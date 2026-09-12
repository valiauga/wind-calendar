"""Shared forecast fetch and wind-window qualification, forked from wind-window-frame.

No travel-time gating here: the source project skips marginal-wind sessions at spots
far from the owner's home, which doesn't generalize to subscribers at unknown
locations. This qualifies purely on wind, direction and season thresholds.
"""
import json
import math
import os
import time
import threading
import urllib.error
from email.utils import parsedate_to_datetime
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from datetime import time as day_start
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
SPOTS = json.loads((ROOT / 'spots.json').read_text())
ZONE = ZoneInfo('Europe/Amsterdam')
CACHE = Path(os.environ.get('FORECAST_CACHE_DIR', ROOT / '.forecast-cache'))
CACHE_SECONDS = 3600
_forecast_lock = threading.Lock()
_retry_at = 0
_rate_failures = 0


def retry_delay(value):
    """Honor Retry-After seconds or HTTP date, with a fifteen-minute minimum."""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try:
            seconds = parsedate_to_datetime(value).timestamp() - time.time()
        except (TypeError, ValueError, AttributeError, OverflowError):
            seconds = 0
    return max(900, seconds) if math.isfinite(seconds) else 900


def forecast(spot, allow_stale=False):
    # One upstream request at a time; queued readers recheck the shared cache.
    with _forecast_lock:
        return _forecast(spot, allow_stale)


def _forecast(spot, allow_stale):
    global _retry_at, _rate_failures
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / (spot['id'] + '-v1.json')
    cached = None
    if path.exists():
        try:
            cached = json.loads(path.read_text())
            validate(cached)
            cached['_fetched_at'] = path.stat().st_mtime
        except (ValueError, KeyError, TypeError, OSError):
            cached = None
    if cached and time.time() - cached['_fetched_at'] < CACHE_SECONDS:
        return cached
    if time.monotonic() < _retry_at:
        if allow_stale and cached:
            return cached
        raise RuntimeError('Forecast provider cooling down')
    params = urllib.parse.urlencode({
        'latitude': spot['lat'], 'longitude': spot['lon'],
        'hourly': 'wind_speed_10m,wind_gusts_10m,wind_direction_10m',
        'daily': 'sunrise,sunset', 'wind_speed_unit': 'kn',
        'timezone': 'Europe/Amsterdam', 'forecast_days': 10,
        'models': 'gfs_seamless',
        'cell_selection': 'sea' if spot['group'] == 'coast' else 'nearest',
    })
    try:
        req = urllib.request.Request('https://api.open-meteo.com/v1/forecast?' + params,
                                     headers={'User-Agent': 'WindCalendar/1.0'})
        with urllib.request.urlopen(req, timeout=30) as response:
            data = json.load(response)
        validate(data)
        # Atomic replacement: the cron job and any local run can have concurrent readers.
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', dir=CACHE, delete=False) as tmp:
            json.dump(data, tmp)
            temp_path = tmp.name
        os.replace(temp_path, path)
        data['_fetched_at'] = path.stat().st_mtime
        _rate_failures = 0
        return data
    except Exception as exc:
        if isinstance(exc, urllib.error.HTTPError) and exc.code == 429:
            _rate_failures = min(_rate_failures + 1, 6)
            delay = max(min(900 * 2 ** (_rate_failures - 1), 21600),
                        retry_delay(exc.headers.get('Retry-After')))
            _retry_at = time.monotonic() + delay
        else:
            _retry_at = time.monotonic() + 60
        if allow_stale and cached:
            return cached
        raise


def validate(data):
    hourly, daily = data['hourly'], data['daily']
    times = hourly['time']
    if not times or times != sorted(set(times)):
        raise ValueError('Missing or unordered forecast hours')
    for key in ('wind_speed_10m', 'wind_gusts_10m', 'wind_direction_10m'):
        values = hourly[key]
        if len(values) != len(times) or any(
            not isinstance(v, (int, float)) or not math.isfinite(v) for v in values
        ):
            raise ValueError('Incomplete forecast: ' + key)
    for key in ('sunrise', 'sunset'):
        if len(daily[key]) != len(daily['time']) or not all(daily[key]):
            raise ValueError('Incomplete daylight data')
    if not set(t[:10] for t in times).issubset(daily['time']):
        raise ValueError('Missing forecast days')


def block_span(block):
    """Local (Europe/Amsterdam) start/end datetimes for a qualifying window block."""
    day = datetime.combine(datetime.fromisoformat(block['day']).date(), day_start(), ZONE)
    return day + timedelta(minutes=round(block['start'] * 60)), day + timedelta(minutes=round(block['end'] * 60))


def wind_class(direction, normal):
    delta = abs((direction - normal + 540) % 360 - 180)
    for limit, name, angle in ((22.5, 'onshore', 90), (67.5, 'side-on', 45),
                               (112.5, 'cross', 0), (157.5, 'side-off', 135)):
        if delta <= limit:
            return name, angle
    return 'offshore', -1


def windows(spot, data, start=None):
    validate(data)
    start = start or datetime.now(ZONE).date()
    end = start + timedelta(days=10)
    hourly, daily = data['hourly'], data['daily']
    daylight = dict(zip(daily['time'], zip(daily['sunrise'], daily['sunset'])))
    blocks, current = [], None
    for i, stamp in enumerate(hourly['time']):
        dt = datetime.fromisoformat(stamp)
        day, md = stamp[:10], stamp[5:10]
        speed, gust = hourly['wind_speed_10m'][i], hourly['wind_gusts_10m'][i]
        name, angle = wind_class(hourly['wind_direction_10m'][i], spot['shoreNormal'])
        season = any(w['from'] <= md <= w['to'] if w['from'] <= w['to'] else
                     md >= w['from'] or md <= w['to'] for w in spot['openWindows'])
        sunrise, sunset = map(datetime.fromisoformat, daylight[day])
        if not (start <= dt.date() < end and season and sunrise <= dt <= sunset):
            continue
        if name == 'offshore' or (name == 'side-off' and not spot['allowSideOffshore']):
            continue
        if not spot['minWind'] <= speed <= spot['maxWind'] or gust / max(speed, .1) > spot['maxGustFactor']:
            continue
        h = dt.hour + dt.minute / 60
        # Do not extend a qualifying hourly sample past sunset.
        finish = min(h + 1, sunset.hour + sunset.minute / 60)
        part = {'start': h, 'end': finish, 'angle': angle,
                'strong': speed >= spot['strongThreshold'], 'speed': speed, 'direction': name}
        if current is None or current['day'] != day or h > current['end'] + .01:
            current = {'day': day, 'start': h, 'end': finish, 'parts': []}
            blocks.append(current)
        current['end'] = finish
        current['parts'].append(part)
    return [b for b in blocks if b['end'] - b['start'] >= 1.5]
