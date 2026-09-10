"""Reactive forecast correction from live KNMI observations. No database: KNMI's
real-time observations endpoint retains trailing data, so each run pulls a fresh
short window and computes the correction from timestamps already in the response.

Mechanism: take the current observed/forecast wind-speed ratio at the spot's mapped
KNMI station, clamp it to a plausible range, then blend it into the forecast with
exponential decay (tau ~= 5h) so near-term hours move toward observed reality while
hours far in the future converge back to the raw model forecast.

A spot with no mapped KNMI station (knmiStationId is None) gets no correction --
ship the raw forecast rather than guess at an unverified station.
"""
import json
import math
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = 'https://api.dataplatform.knmi.nl/edr/v1/collections/10-minute-in-situ-meteorological-observations'
TAU_HOURS = 5
RATIO_CLAMP = (0.75, 1.35)
MS_TO_KNOTS = 1.9438444924
MIN_REFERENCE_SPEED = 3  # knots; ratios near zero wind are unstable, not meaningful.


def fetch_observed_speed(station_id, api_key, now_utc, window_minutes=60, timeout=30):
    """Mean observed wind speed in knots over the trailing window, or None."""
    start = (now_utc - timedelta(minutes=window_minutes)).strftime('%Y-%m-%dT%H:%M:%SZ')
    end = now_utc.strftime('%Y-%m-%dT%H:%M:%SZ')
    url = (BASE + '/locations/' + urllib.parse.quote(station_id, safe='') + '?' +
           urllib.parse.urlencode({'datetime': start + '/' + end, 'parameter-name': 'ff'}))
    req = urllib.request.Request(url, headers={'Authorization': api_key, 'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        data = json.load(response)
    coverages = data.get('coverages') or []
    if not coverages:
        return None
    values = [v for v in coverages[0]['ranges']['ff']['values'] if v is not None]
    if not values:
        return None
    return sum(values) / len(values) * MS_TO_KNOTS


def current_ratio(spot, forecast_data, now_local, api_key):
    """Clamped observed/forecast wind-speed ratio right now, or None if unavailable."""
    station_id = spot.get('knmiStationId')
    if not station_id or not api_key:
        return None
    now_utc = now_local.astimezone(timezone.utc)
    try:
        observed = fetch_observed_speed(station_id, api_key, now_utc)
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, KeyError, TimeoutError):
        return None
    if observed is None:
        return None
    hourly = forecast_data['hourly']
    stamp = now_local.replace(minute=0, second=0, microsecond=0).isoformat()[:13]
    matches = [i for i, t in enumerate(hourly['time']) if t[:13] == stamp]
    if not matches:
        return None
    reference_speed = hourly['wind_speed_10m'][matches[0]]
    if reference_speed < MIN_REFERENCE_SPEED:
        return None
    ratio = observed / reference_speed
    return min(max(ratio, RATIO_CLAMP[0]), RATIO_CLAMP[1])


def apply(forecast_data, spot, now_local, api_key):
    """Return a copy of forecast_data with hourly wind nudged toward the live ratio,
    decaying to the untouched forecast further into the horizon. A no-op (returns
    forecast_data unchanged) whenever no station is mapped or observations aren't
    available -- correction is a best-effort improvement, never a hard dependency."""
    ratio = current_ratio(spot, forecast_data, now_local, api_key)
    if ratio is None:
        return forecast_data
    hourly = dict(forecast_data['hourly'])
    speeds, gusts = list(hourly['wind_speed_10m']), list(hourly['wind_gusts_10m'])
    for i, stamp in enumerate(hourly['time']):
        dt = datetime.fromisoformat(stamp)
        hours_ahead = (dt - now_local.replace(tzinfo=None)).total_seconds() / 3600
        if hours_ahead < 0:
            continue
        multiplier = 1 + (ratio - 1) * math.exp(-hours_ahead / TAU_HOURS)
        speeds[i] *= multiplier
        gusts[i] *= multiplier
    hourly['wind_speed_10m'], hourly['wind_gusts_10m'] = speeds, gusts
    return dict(forecast_data, hourly=hourly)
