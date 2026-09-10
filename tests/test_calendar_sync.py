import copy
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import calendar_sync as sync
from wind import SPOTS, ZONE, windows


def data_for(day='2026-09-07', speeds=None, sunset='19:30'):
    speeds = speeds or [20] * 24
    return {'hourly': {'time': [f'{day}T{h:02}:00' for h in range(24)],
                       'wind_speed_10m': speeds, 'wind_gusts_10m': [24] * 24,
                       'wind_direction_10m': [285] * 24},
            'daily': {'time': [day], 'sunrise': [day + 'T07:00'], 'sunset': [day + 'T' + sunset]}}


def ten_day_data_for(start='2026-09-07', days=11):
    from datetime import date, timedelta
    base = date.fromisoformat(start)
    times, speeds, gusts, dirs, daily_days, sunrises, sunsets = [], [], [], [], [], [], []
    for d in range(days):
        day = (base + timedelta(days=d)).isoformat()
        daily_days.append(day)
        sunrises.append(day + 'T07:00')
        sunsets.append(day + 'T19:30')
        for h in range(24):
            times.append(f'{day}T{h:02}:00')
            speeds.append(20)
            gusts.append(24)
            dirs.append(285)
    return {'hourly': {'time': times, 'wind_speed_10m': speeds,
                       'wind_gusts_10m': gusts, 'wind_direction_10m': dirs},
            'daily': {'time': daily_days, 'sunrise': sunrises, 'sunset': sunsets}}


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 9, 7, tzinfo=ZONE)
        self.end = self.start + timedelta(days=10)
        block = windows(SPOTS[0], data_for(), self.start.date())[0]
        self.event = sync.event_for(SPOTS[0], block, sync.APP_URL)

    def run_sync(self, api, desired):
        return sync.reconcile(api, 'test@calendar', desired, self.start, self.end)

    def test_repeat_sync_is_noop(self):
        api = FakeAPI()
        self.assertEqual(self.run_sync(api, [self.event])['created'], 1)
        self.assertEqual(self.run_sync(api, [self.event]), {'created': 0, 'updated': 0, 'deleted': 0})
        self.assertEqual(len(api.current), 1)

    def test_shifted_window_keeps_id(self):
        old = dict(self.event, id='old')
        api = FakeAPI([old])
        changed = copy.deepcopy(self.event)
        changed['start']['dateTime'] = '2026-09-07T08:00:00+02:00'
        result = self.run_sync(api, [changed])
        self.assertEqual(result['updated'], 1)
        self.assertEqual(api.current[0]['id'], 'old')
        self.assertEqual(api.calls[0][3], {'sendUpdates': 'none'})

    def test_obsolete_windows_removed(self):
        api = FakeAPI([dict(self.event, id='old')])
        self.assertEqual(self.run_sync(api, [])['deleted'], 1)

    def test_failed_insert_does_not_delete_existing(self):
        old = copy.deepcopy(self.event)
        old['id'] = 'old'
        old['extendedProperties']['private']['spot'] = 'other'
        api = FakeAPI([old], fail=True)
        with self.assertRaises(RuntimeError):
            self.run_sync(api, [self.event])
        self.assertFalse(any(c[0] == 'DELETE' for c in api.calls))

    def test_partial_forecast_aborts_before_google(self):
        with patch.object(sync, 'forecast', return_value=data_for()), patch.object(sync.GoogleCalendar, 'from_config') as google:
            with self.assertRaises(ValueError):
                sync.build_events(self.start, sync.APP_URL)
            google.assert_not_called()

    def test_build_events_covers_every_spot(self):
        with patch.object(sync, 'forecast', return_value=ten_day_data_for()):
            desired = sync.build_events(self.start, sync.APP_URL)
        self.assertEqual(set(desired), {s['id'] for s in SPOTS})

    def test_pagination_and_ownership_filter(self):
        api = sync.GoogleCalendar('test')
        with patch.object(api, 'request', side_effect=[{'items': [{'id': 'one'}], 'nextPageToken': 'next'}, {'items': [{'id': 'two'}]}]) as req:
            self.assertEqual(len(api.events('calendar', self.start, self.end)), 2)
            self.assertEqual(req.call_args.kwargs['params']['privateExtendedProperty'], 'publisher=' + sync.MARKER)
            self.assertEqual(req.call_args.kwargs['params']['pageToken'], 'next')


class FakeAPI:
    def __init__(self, events=(), fail=False):
        self.current = copy.deepcopy(list(events))
        self.calls = []
        self.fail = fail

    def events(self, *args):
        return copy.deepcopy(self.current)

    def request(self, method, path, body=None, params=None):
        self.calls.append((method, path, body, params))
        if self.fail and method == 'POST':
            raise RuntimeError('Network failed')
        if method == 'POST':
            self.current.append(copy.deepcopy(body))
        elif method == 'PATCH':
            target = next(e for e in self.current if e['id'] == path.split('/')[-1])
            target.update(copy.deepcopy(body))
        elif method == 'DELETE':
            self.current = [e for e in self.current if e['id'] != path.split('/')[-1]]
        return body or {}


if __name__ == '__main__':
    unittest.main()
