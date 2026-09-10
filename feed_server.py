#!/usr/bin/env python3
"""Merges the already-public per-spot Google Calendar ICS feeds into one combined
feed for whatever subset of spots a visitor picks within a region. Exists only
because Google's native "Add to Google Calendar" button can't point at a custom,
visitor-defined combination -- that mechanism only works for a calendar that
already exists with fixed content. No events are generated here; this only
re-fetches and concatenates the VEVENT blocks each spot's own public calendar
already serves.
"""
import json
import os
import re
import time
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SPOTS = json.loads((ROOT / 'spots.json').read_text())
CALENDARS = json.loads((ROOT / 'public' / 'calendars.json').read_text())
SPOT_IDS = {s['id'] for s in SPOTS}
CACHE_SECONDS = 900
_cache = {}
_cache_lock = threading.Lock()


def spot_ics_url(spot_id):
    cid = CALENDARS[spot_id]
    return 'https://calendar.google.com/calendar/ical/' + urllib.parse.quote(cid, safe='') + '/public/basic.ics'


def fetch_events(spot_id):
    """Raw VEVENT...END:VEVENT blocks (with original folding) for one spot."""
    req = urllib.request.Request(spot_ics_url(spot_id), headers={'User-Agent': 'WindCalendarFeed/1.0'})
    with urllib.request.urlopen(req, timeout=20) as response:
        text = response.read().decode('utf-8', errors='replace')
    return re.findall(r'^BEGIN:VEVENT\r?\n.*?^END:VEVENT\r?\n', text, re.DOTALL | re.MULTILINE)


def merged_feed(spot_ids):
    key = tuple(sorted(spot_ids))
    with _cache_lock:
        cached = _cache.get(key)
    if cached and time.time() - cached[0] < CACHE_SECONDS:
        return cached[1]
    events = []
    for spot_id in key:
        events.extend(fetch_events(spot_id))
    body = (
        'BEGIN:VCALENDAR\r\n'
        'PRODID:-//Wind Calendar//Custom Feed//EN\r\n'
        'VERSION:2.0\r\n'
        'CALSCALE:GREGORIAN\r\n'
        'METHOD:PUBLISH\r\n'
        'X-WR-CALNAME:Wind Calendar (custom)\r\n'
        'X-WR-TIMEZONE:Europe/Amsterdam\r\n'
        + ''.join(events) +
        'END:VCALENDAR\r\n'
    )
    with _cache_lock:
        _cache[key] = (time.time(), body)
    return body


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # Avoid logging query strings; they're not sensitive here but keep it minimal.

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path != '/feed.ics':
            self.send_response(404)
            self.end_headers()
            return
        requested = [s for s in urllib.parse.parse_qs(parsed.query).get('spots', [''])[0].split(',') if s]
        unknown = [s for s in requested if s not in SPOT_IDS]
        if not requested or unknown:
            self.send_response(400)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.end_headers()
            self.wfile.write(('Unknown or missing spot ids: ' + ', '.join(unknown or ['(none given)'])).encode())
            return
        try:
            body = merged_feed(requested).encode('utf-8')
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
            self.send_response(502)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.end_headers()
            self.wfile.write(b'Could not fetch upstream calendar feeds. Try again shortly.')
            return
        self.send_response(200)
        self.send_header('Content-Type', 'text/calendar; charset=utf-8')
        self.send_header('Cache-Control', 'public, max-age=' + str(CACHE_SECONDS))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)


def main():
    port = int(os.environ.get('PORT', 8000))
    server = ThreadingHTTPServer(('0.0.0.0', port), Handler)
    print('Serving custom feed merges on port', port)
    server.serve_forever()


if __name__ == '__main__':
    main()
