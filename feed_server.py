#!/usr/bin/env python3
"""Merges the already-public per-spot Google Calendar ICS feeds into one combined
feed for whatever subset of spots a visitor picks within a region. Exists only
because Google's native "Add to Google Calendar" button can't point at a custom,
visitor-defined combination -- that mechanism only works for a calendar that
already exists with fixed content. No events are generated here; this only
re-fetches and concatenates the VEVENT blocks each spot's own public calendar
already serves.

Also mints and updates persistent "mix" tokens (/mix, /mix/<token>) so a
visitor's subscribe link stays the same URL even after they change which
spots it covers -- calendar apps refresh the same subscription with new
content instead of needing a whole new subscription. Requires REDIS_URL
(kv_store.py); without it, /mix responds 503 and the site falls back to
plain ?spots= links that do need a resubscribe on change.

Also collects visitor feedback (POST /feedback), appended to a Redis list,
and lets the owner read it back (GET /feedback?token=<FEEDBACK_ADMIN_TOKEN>)
without needing direct access to the KV store itself.
"""
import datetime
import json
import os
import re
import secrets
import time
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import kv_store

ROOT = Path(__file__).resolve().parent
SPOTS = json.loads((ROOT / 'spots.json').read_text())
CALENDARS = json.loads((ROOT / 'public' / 'calendars.json').read_text())
SPOT_IDS = {s['id'] for s in SPOTS}
CACHE_SECONDS = 900
MIX_KEY_PREFIX = 'mix:'
FEEDBACK_KEY = 'feedback:all'
FEEDBACK_MESSAGE_MAX = 2000
FEEDBACK_CONTACT_MAX = 200
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


def spots_for_token(token):
    """Returns a spot id list, or None if the token is unknown/expired/unconfigured."""
    raw = kv_store.get(MIX_KEY_PREFIX + token)
    if raw is None:
        return None
    try:
        spots = json.loads(raw)
    except ValueError:
        return None
    return [s for s in spots if s in SPOT_IDS]


def store_mix(token, spot_ids):
    return kv_store.set_with_ttl(MIX_KEY_PREFIX + token, json.dumps(sorted(spot_ids)))


def store_feedback(message, contact):
    entry = {
        'message': message, 'contact': contact,
        'receivedAt': datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    return kv_store.rpush(FEEDBACK_KEY, json.dumps(entry))


def read_feedback():
    """Newest first; skips any entry that fails to parse rather than 500ing."""
    entries = []
    for raw in kv_store.lrange(FEEDBACK_KEY, 0, -1):
        try:
            entries.append(json.loads(raw))
        except ValueError:
            continue
    entries.reverse()
    return entries


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # Avoid logging query strings; they're not sensitive here but keep it minimal.

    def _cors_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, PUT, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')

    def _json(self, status, payload):
        body = json.dumps(payload).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self._cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self):
        length = int(self.headers.get('Content-Length', 0) or 0)
        if not length or length > 8192:
            return None
        try:
            return json.loads(self.rfile.read(length).decode('utf-8'))
        except ValueError:
            return None

    def _valid_spot_list(self, payload):
        if not isinstance(payload, dict):
            return None
        spots = payload.get('spots')
        if not isinstance(spots, list) or not spots:
            return None
        if any(s not in SPOT_IDS for s in spots):
            return None
        return spots

    def _valid_feedback(self, payload):
        if not isinstance(payload, dict):
            return None
        message = (payload.get('message') or '').strip()
        contact = (payload.get('contact') or '').strip()
        if not message or len(message) > FEEDBACK_MESSAGE_MAX or len(contact) > FEEDBACK_CONTACT_MAX:
            return None
        return message, contact

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors_headers()
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == '/feedback':
            self._handle_read_feedback(urllib.parse.parse_qs(parsed.query))
            return
        if parsed.path != '/feed.ics':
            self.send_response(404)
            self.end_headers()
            return
        query = urllib.parse.parse_qs(parsed.query)
        token = query.get('token', [''])[0]
        if token:
            requested = spots_for_token(token)
            if not requested:
                self.send_response(404)
                self.send_header('Content-Type', 'text/plain; charset=utf-8')
                self.end_headers()
                self.wfile.write(b'Unknown or expired link. Recreate it from the site.')
                return
        else:
            requested = [s for s in query.get('spots', [''])[0].split(',') if s]
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

    def do_POST(self):
        if self.path == '/feedback':
            self._handle_submit_feedback()
            return
        if self.path != '/mix':
            self.send_response(404)
            self.end_headers()
            return
        spots = self._valid_spot_list(self._read_json_body())
        if spots is None:
            self._json(400, {'error': 'Body must be {"spots": [known spot ids]}'})
            return
        token = secrets.token_urlsafe(16)
        if not store_mix(token, spots):
            self._json(503, {'error': 'Persistent links are not configured yet.'})
            return
        self._json(200, {'token': token})

    def _handle_submit_feedback(self):
        parsed = self._valid_feedback(self._read_json_body())
        if parsed is None:
            self._json(400, {'error': 'Body must be {"message": "...", "contact": "(optional)"}'})
            return
        message, contact = parsed
        if not store_feedback(message, contact):
            self._json(503, {'error': 'Feedback storage is not configured yet.'})
            return
        self._json(200, {'ok': True})

    def _handle_read_feedback(self, query):
        expected = os.environ.get('FEEDBACK_ADMIN_TOKEN')
        given = query.get('token', [''])[0]
        if not expected or not secrets.compare_digest(given, expected):
            self._json(403, {'error': 'Missing or incorrect admin token.'})
            return
        self._json(200, {'feedback': read_feedback()})

    def do_PUT(self):
        parsed = urllib.parse.urlsplit(self.path)
        match = re.fullmatch(r'/mix/([A-Za-z0-9_-]{8,64})', parsed.path)
        if not match:
            self.send_response(404)
            self.end_headers()
            return
        spots = self._valid_spot_list(self._read_json_body())
        if spots is None:
            self._json(400, {'error': 'Body must be {"spots": [known spot ids]}'})
            return
        token = match.group(1)
        if not store_mix(token, spots):
            self._json(503, {'error': 'Persistent links are not configured yet.'})
            return
        self._json(200, {'token': token})


def main():
    port = int(os.environ.get('PORT', 8000))
    server = ThreadingHTTPServer(('0.0.0.0', port), Handler)
    print('Serving custom feed merges on port', port)
    server.serve_forever()


if __name__ == '__main__':
    main()
