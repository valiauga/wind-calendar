#!/usr/bin/env python3
"""Publish per-spot shared forecast opportunities. No attendees, invitations, or busy time."""
import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, time, timedelta

import knmi_correction
from wind import SPOTS, ZONE, forecast, windows

API = 'https://www.googleapis.com/calendar/v3'
APP_URL = 'https://wind-calendar.onrender.com/'
MARKER = 'wind-calendar-v1'


class GoogleCalendar:
    def __init__(self, token):
        self.token = token

    @classmethod
    def from_config(cls, config):
        data = urllib.parse.urlencode({
            'client_id': config['client_id'], 'client_secret': config['client_secret'],
            'refresh_token': config['refresh_token'], 'grant_type': 'refresh_token',
        }).encode()
        req = urllib.request.Request('https://oauth2.googleapis.com/token', data=data)
        with urllib.request.urlopen(req, timeout=30) as response:
            return cls(json.load(response)['access_token'])

    def request(self, method, path, body=None, params=None):
        url = API + path
        if params:
            url += '?' + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json',
        })
        with urllib.request.urlopen(req, timeout=30) as response:
            payload = response.read()
            return json.loads(payload) if payload else {}

    def events(self, calendar_id, start, end):
        path = '/calendars/' + urllib.parse.quote(calendar_id, safe='') + '/events'
        params = {'timeMin': start.isoformat(), 'timeMax': end.isoformat(),
                  'privateExtendedProperty': 'publisher=' + MARKER, 'maxResults': 2500}
        result = []
        while True:
            page = self.request('GET', path, params=params)
            result.extend(page.get('items', []))
            if not page.get('nextPageToken'):
                return result
            params['pageToken'] = page['nextPageToken']


def event_for(spot, block, app_url):
    day = datetime.combine(datetime.fromisoformat(block['day']).date(), time(), ZONE)
    begin = day + timedelta(minutes=round(block['start'] * 60))
    end = day + timedelta(minutes=round(block['end'] * 60))
    speed = int(max(p['speed'] for p in block['parts']) + .5)
    direction = Counter(p['direction'] for p in block['parts']).most_common(1)[0][0]
    return {
        'summary': f"{spot['name']} · {speed} kt · {direction}",
        'location': f"{spot['name']}, Netherlands ({spot['lat']}, {spot['lon']})",
        'description': ('Forecast opportunity · peak sustained wind (not gusts).\n'
                        f"View spots and subscribe: {app_url}\n"
                        f"Map: https://www.google.com/maps?q={spot['lat']},{spot['lon']}\n"
                        'Automatically updated from GFS forecast data. Check local conditions before going.'),
        'source': {'title': 'Wind Calendar', 'url': app_url},
        'start': {'dateTime': begin.isoformat(), 'timeZone': 'Europe/Amsterdam'},
        'end': {'dateTime': end.isoformat(), 'timeZone': 'Europe/Amsterdam'},
        'transparency': 'transparent', 'reminders': {'useDefault': False},
        'extendedProperties': {'private': {'publisher': MARKER, 'spot': spot['id']}},
    }


def event_time(event, key):
    return datetime.fromisoformat(event[key]['dateTime'].replace('Z', '+00:00'))


def reconcile(api, calendar_id, desired, start, end):
    """Reuse IDs when a window moves; only remove this app's future events."""
    path = '/calendars/' + urllib.parse.quote(calendar_id, safe='') + '/events'
    existing = api.events(calendar_id, start, end)
    available = {e['id']: e for e in existing if e.get('status') != 'cancelled'}
    counts = {'created': 0, 'updated': 0, 'deleted': 0}
    for body in desired:
        spot = body['extendedProperties']['private']['spot']
        begin = event_time(body, 'start')
        candidates = [e for e in available.values()
                      if e.get('extendedProperties', {}).get('private', {}).get('spot') == spot
                      and 'dateTime' in e.get('start', {})
                      and event_time(e, 'start').astimezone(ZONE).date() == begin.date()]
        match = min(candidates, key=lambda e: abs((event_time(e, 'start') - begin).total_seconds()), default=None)
        if match:
            available.pop(match['id'])
            # Compare timestamps as instants; Google may normalize RFC3339 strings.
            same = all(match.get(k) == v for k, v in body.items() if k not in ('start', 'end'))
            same = same and all(event_time(match, k) == event_time(body, k) for k in ('start', 'end'))
            if not same:
                api.request('PATCH', path + '/' + urllib.parse.quote(match['id'], safe=''), body,
                            {'sendUpdates': 'none'})
                counts['updated'] += 1
        else:
            # Stable IDs make an insertion retry safe after an uncertain network response.
            key = spot + begin.isoformat()
            new = dict(body, id=hashlib.sha256(key.encode()).hexdigest())
            try:
                api.request('POST', path, new, {'sendUpdates': 'none'})
            except urllib.error.HTTPError as error:
                if error.code != 409:
                    raise
                # A deleted Google event ID cannot reliably be reused. Read conflicts,
                # then use a new ID only for a confirmed tombstone.
                try:
                    old = api.request('GET', path + '/' + new['id'])
                except urllib.error.HTTPError as lookup_error:
                    if lookup_error.code != 410:
                        raise
                    old = {'status': 'cancelled'}
                if old.get('status') == 'cancelled':
                    import uuid
                    new['id'] = uuid.uuid4().hex
                    api.request('POST', path, new, {'sendUpdates': 'none'})
                elif old.get('extendedProperties', {}).get('private', {}).get('publisher') == MARKER:
                    api.request('PATCH', path + '/' + new['id'], body, {'sendUpdates': 'none'})
                else:
                    raise RuntimeError('Unexpected calendar event ID conflict') from None
            counts['created'] += 1
    # Writes finish before cleanup, so a failed insertion won't wipe existing windows.
    for event in available.values():
        api.request('DELETE', path + '/' + urllib.parse.quote(event['id'], safe=''), params={'sendUpdates': 'none'})
        counts['deleted'] += 1
    return counts


def calendar_keys():
    """Every calendar this app publishes to: one per group, plus one per spot,
    so a subscriber can pick a whole region or an individual spot."""
    return ['coast', 'inland'] + [s['id'] for s in SPOTS]


def build_events(now, app_url, api_key=None, forecasts=None):
    """One event list per group ('coast'/'inland') AND per individual spot id --
    each qualifying event is published to both its region calendar and its own
    spot calendar. Applies the reactive KNMI correction (a no-op for spots with
    no mapped station) before qualifying windows."""
    result = {key: [] for key in calendar_keys()}
    for spot in SPOTS:
        data = forecast(spot)  # No stale fallback for calendar mutations.
        data = knmi_correction.apply(data, spot, now, api_key)
        if forecasts is not None:
            forecasts[spot['id']] = data
        required = {(now.date() + timedelta(days=i)).isoformat() for i in range(10)}
        if not required.issubset(data['daily']['time']):
            raise ValueError('Forecast does not cover the ten-day horizon')
        # Refuse partial days, rather than deleting events based on truncated data.
        for day in required:
            if sum(t.startswith(day) for t in data['hourly']['time']) < 23:
                raise ValueError('Incomplete forecast day')
        for block in windows(spot, data, now.date()):
            event = event_for(spot, block, app_url)
            if event_time(event, 'end') > now:
                result[spot['group']].append(event)
                result[spot['id']].append(event)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true', help='Preview events without Google credentials or writes')
    args = parser.parse_args()
    now = datetime.now(ZONE)
    url = os.environ.get('WIND_APP_URL', APP_URL)
    if not url.startswith('https://'):
        raise ValueError('WIND_APP_URL must be HTTPS')
    api_key = os.environ.get('KNMI_EDR_API_KEY')
    forecasts = {}
    desired = build_events(now, url, api_key, forecasts)  # Fetch ALL spots before Google writes.
    if args.dry_run:
        print(json.dumps(desired, indent=2, ensure_ascii=False))
        return
    config = json.loads(os.environ['GOOGLE_CALENDAR_CONFIG'])
    ids = config['calendars']
    missing = [key for key in calendar_keys() if not ids.get(key)]
    if missing:
        raise ValueError('Missing calendar IDs for: ' + ', '.join(missing))
    api = GoogleCalendar.from_config(config)
    end = datetime.combine(now.date() + timedelta(days=10), time(), ZONE)
    for key in calendar_keys():
        print(key, reconcile(api, ids[key], desired[key], now, end))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Never log credential payloads or OAuth response bodies.
        print('Calendar sync failed: ' + type(error).__name__ + '. Check credentials, forecast availability, and retry.', file=sys.stderr)
        sys.exit(1)
