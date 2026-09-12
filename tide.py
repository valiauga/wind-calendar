"""Astronomical (weather-independent) tide predictions, used only to gate a
spot's qualifying windows to when tide conditions actually suit it -- e.g.
Zandmotor's sheltered lagoon, which only fills properly around high tide
("de poel is met vloed goed vol").

Uses Rijkswaterstaat's public Waterinfo chart-widget endpoint rather than the
documented Waterwebservices API: OphalenWaarnemingen (the documented service)
only ever returns past measurements -- verified against a live request, it
silently truncates a future period down to "now" -- and RWS's forward-looking
astronomical predictions are only exposed through this chart-widget backend.
That endpoint only accepts a small whitelist of forward windows (verified
empirically against the live site, not documented anywhere); this asks for
the widest one confirmed to work, a week out. A block further out than that,
or any fetch failure, ships ungated -- same as knmi_correction.py without a
mapped station: this is a best-effort refinement, never a hard dependency.
"""
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = 'https://waterinfo.rws.nl/api/chart/get'
FORWARD_RANGE = '0,168'  # hours; the widest forward window confirmed to return data
DEFAULT_TOLERANCE_HOURS = 1.5


def fetch_astronomical_series(station, timeout=20):
    """(UTC datetime, height_cm) pairs for roughly the next week, oldest first,
    or None on any failure (network, unexpected schema, or too little data)."""
    params = urllib.parse.urlencode({
        'mapType': 'astronomische-getij', 'locationCodes': station,
        'getijReference': 'NAP', 'values': FORWARD_RANGE,
    })
    req = urllib.request.Request(BASE + '?' + params, headers={'Accept': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            data = json.load(response)
        points = data['series'][0]['data']
        series = [(datetime.fromisoformat(p['dateTime'].replace('Z', '+00:00')), p['value'])
                  for p in points if p['value'] is not None]
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
            KeyError, ValueError, IndexError):
        return None
    return series if len(series) >= 3 else None


def high_tide_windows(series, tolerance_hours):
    """(start, end) UTC ranges around each local high-tide peak in series.
    Strict on one side only, so a multi-point plateau at a peak (equal
    rounded values from the source) counts once, at its first point.

    Requires the peak to sit above NAP (0cm, roughly mean sea level): Hoek van
    Holland's tide curve is shallow-water-distorted, with a genuine secondary
    wiggle partway through the low-water trough (a real local maximum, just
    nowhere near high tide) that a plain peak-shape check alone would
    mistake for one."""
    return [
        (peak_time - timedelta(hours=tolerance_hours), peak_time + timedelta(hours=tolerance_hours))
        for i, (peak_time, value) in enumerate(series[1:-1], start=1)
        if value > series[i - 1][1] and value >= series[i + 1][1] and value > 0
    ]


def block_qualifies(series, block, tolerance_hours=DEFAULT_TOLERANCE_HOURS):
    """True if there's no tide data (fetch failed), the block falls outside the
    predicted horizon (don't guess beyond what was fetched), or the block's
    span overlaps a high-tide window. False only when data covers the block
    and says it genuinely misses every high tide."""
    if series is None:
        return True
    from wind import block_span
    begin, end = (t.astimezone(timezone.utc) for t in block_span(block))
    series_start, series_end = series[0][0], series[-1][0]
    if end <= series_start or begin >= series_end:
        return True
    windows = high_tide_windows(series, tolerance_hours)
    if not windows:
        return True
    return any(begin < w_end and end > w_start for w_start, w_end in windows)
