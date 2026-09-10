#!/usr/bin/env python3
"""One-time owner authorization and creation of the per-spot shared calendars."""
import argparse
import base64
import hashlib
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from calendar_sync import GoogleCalendar

SCOPE = 'https://www.googleapis.com/auth/calendar.app.created'
DEFAULT_OUTPUT = Path.home() / '.config' / 'wind-calendar' / 'google-calendar.json'


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    # Credentials never live in the public app directory.
    tmp = path.with_suffix('.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(data, stream, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def authorize(client):
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    result = {}

    class Callback(BaseHTTPRequestHandler):
        def do_GET(self):
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            if not secrets.compare_digest(query.get('state', [''])[0], state):
                self.send_error(400, 'Invalid authorization state')
                return
            if query.get('error'):
                result['error'] = True
            elif query.get('code'):
                result['code'] = query['code'][0]
            else:
                self.send_error(400, 'Missing authorization code')
                return
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.end_headers()
            self.wfile.write(b'You can close this tab and return to the terminal.')

        def log_message(self, *args):
            pass  # OAuth codes must not appear in request logs.

    with HTTPServer(('127.0.0.1', 0), Callback) as server:
        redirect = 'http://127.0.0.1:' + str(server.server_port)
        url = 'https://accounts.google.com/o/oauth2/v2/auth?' + urllib.parse.urlencode({
            'client_id': client['client_id'], 'redirect_uri': redirect,
            'response_type': 'code', 'scope': SCOPE, 'state': state,
            'code_challenge': challenge, 'code_challenge_method': 'S256',
            'access_type': 'offline', 'prompt': 'consent',
        })
        print('Opening Google authorization. If needed, open this URL:\n' + url, flush=True)
        webbrowser.open(url)
        deadline = time.monotonic() + 300
        server.timeout = 1
        while not result and time.monotonic() < deadline:
            server.handle_request()
    if 'code' not in result:
        raise RuntimeError('Authorization was cancelled or timed out')
    body = urllib.parse.urlencode({
        'client_id': client['client_id'], 'client_secret': client['client_secret'],
        'code': result['code'], 'code_verifier': verifier, 'redirect_uri': redirect,
        'grant_type': 'authorization_code',
    }).encode()
    with urllib.request.urlopen('https://oauth2.googleapis.com/token', body, timeout=30) as response:
        token = json.load(response)
    if SCOPE not in token.get('scope', '').split():
        raise RuntimeError('Calendar permission was not granted')
    if not token.get('refresh_token'):
        raise RuntimeError('No refresh token returned; reconnect with consent')
    return token['refresh_token']


def make_public(api, calendar_id):
    """Grant read access to anyone with the link, via a 'default' scope ACL rule.
    This is what lets subscribers add the calendar without ever granting this app
    access to their own Google account."""
    try:
        api.request('POST', '/calendars/' + urllib.parse.quote(calendar_id, safe='') + '/acl',
                    {'role': 'reader', 'scope': {'type': 'default'}})
    except urllib.error.HTTPError as error:
        if error.code == 403:
            raise RuntimeError(
                'Could not make calendar public (403). The calendar.app.created scope may not '
                'cover ACL writes on this account; share it manually instead: open the calendar '
                'in Google Calendar > Settings and sharing > Access permissions > '
                '"Make available to public".'
            ) from error
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('credentials', type=Path, help='Google OAuth Desktop app client JSON')
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument('--reauthorize', action='store_true', help='Renew owner authorization, preserving calendar IDs')
    args = parser.parse_args()
    from wind import ROOT, SPOTS
    if args.output.resolve().is_relative_to(ROOT):
        raise ValueError('Save credentials outside the app repository')
    client = json.loads(args.credentials.read_text()).get('installed')
    if not client:
        raise ValueError('Create an OAuth client of type Desktop app, then download its JSON')
    config = json.loads(args.output.read_text()) if args.output.exists() else {'calendars': {}}
    if config.get('client_id') and config['client_id'] != client['client_id']:
        raise ValueError('Use the same OAuth client that created these calendars')
    config.update({k: client[k] for k in ('client_id', 'client_secret')})
    if args.reauthorize or not config.get('refresh_token'):
        config['refresh_token'] = authorize(client)
        save(args.output, config)
    api = GoogleCalendar.from_config(config)
    needs_manual_sharing = []
    labels = {'coast': 'Coast', 'inland': 'Inland'}
    members = {'coast': [], 'inland': []}
    for spot in SPOTS:
        members[spot['group']].append(spot['name'])
    for group in ('coast', 'inland'):
        if not config['calendars'].get(group):
            calendar = api.request('POST', '/calendars', {
                'summary': 'Wind Calendar — ' + labels[group],
                'description': 'Shared kitesurfing forecast opportunities for '
                               + ', '.join(members[group]) + '. Events are free time.',
                'timeZone': 'Europe/Amsterdam',
            })
            config['calendars'][group] = calendar['id']
            save(args.output, config)
        try:
            make_public(api, config['calendars'][group])
        except RuntimeError as error:
            print(str(error))
            needs_manual_sharing.append(labels[group])
        print(labels[group] + ' (' + ', '.join(members[group]) + '): '
              'https://calendar.google.com/calendar/render?cid=' +
              urllib.parse.quote(config['calendars'][group], safe=''))
    print('Saved private configuration to ' + str(args.output))
    print('Copy the "calendars" mapping above into public/calendars.json for the frontend, '
          'and set GOOGLE_CALENDAR_CONFIG in Render from the full file at ' + str(args.output) + '.')
    if needs_manual_sharing:
        print('Share these manually (Settings and sharing > Access permissions > '
              '"Make available to public"): ' + ', '.join(needs_manual_sharing))


if __name__ == '__main__':
    main()
