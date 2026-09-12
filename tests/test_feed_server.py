import http.client
import json
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from io import BytesIO
from threading import Thread
from unittest.mock import patch

import feed_server


def ics_response(uids):
    events = ''.join(
        'BEGIN:VEVENT\r\nUID:{}@google.com\r\nSUMMARY:Test\r\nEND:VEVENT\r\n'.format(uid)
        for uid in uids
    )
    return ('BEGIN:VCALENDAR\r\nX-WR-CALNAME:Test\r\n' + events + 'END:VCALENDAR\r\n').encode()


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return BytesIO(self._payload)

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._payload


class FeedServerTests(unittest.TestCase):
    def test_fetch_events_extracts_vevent_blocks_only(self):
        with patch('urllib.request.urlopen', return_value=FakeResponse(ics_response(['a', 'b']))):
            events = feed_server.fetch_events('ijmuiden')
        self.assertEqual(len(events), 2)
        self.assertTrue(all(e.startswith('BEGIN:VEVENT') and e.rstrip().endswith('END:VEVENT') for e in events))

    def test_merged_feed_combines_multiple_spots(self):
        responses = [FakeResponse(ics_response(['a'])), FakeResponse(ics_response(['b']))]
        with patch('urllib.request.urlopen', side_effect=responses):
            body = feed_server.merged_feed(['ijmuiden', 'muiderberg'])
        self.assertTrue(body.startswith('BEGIN:VCALENDAR'))
        self.assertTrue(body.endswith('END:VCALENDAR\r\n'))
        self.assertEqual(body.count('BEGIN:VEVENT'), 2)
        self.assertIn('UID:a@google.com', body)
        self.assertIn('UID:b@google.com', body)

    def test_merged_feed_is_cached(self):
        with patch('urllib.request.urlopen', return_value=FakeResponse(ics_response(['a']))) as mocked:
            feed_server.merged_feed(['ijmuiden'])
            feed_server.merged_feed(['ijmuiden'])
        self.assertEqual(mocked.call_count, 1)

    def test_spots_for_token_returns_none_when_unset(self):
        with patch('feed_server.kv_store.get', return_value=None):
            self.assertIsNone(feed_server.spots_for_token('missing'))

    def test_spots_for_token_filters_unknown_ids(self):
        with patch('feed_server.kv_store.get', return_value=json.dumps(['ijmuiden', 'not-a-spot'])):
            self.assertEqual(feed_server.spots_for_token('abc'), ['ijmuiden'])

    def test_store_mix_sorts_and_delegates_to_kv_store(self):
        with patch('feed_server.kv_store.set_with_ttl', return_value=True) as mocked:
            self.assertTrue(feed_server.store_mix('abc', ['muiderberg', 'ijmuiden']))
        mocked.assert_called_once_with('mix:abc', json.dumps(['ijmuiden', 'muiderberg']))


class FakeKvStore:
    """In-memory stand-in for kv_store, keyed exactly like the real one."""

    def __init__(self):
        self._data = {}

    def get(self, key):
        return self._data.get(key)

    def set_with_ttl(self, key, value, ttl_seconds=None):
        self._data[key] = value
        return True


class MixEndpointTests(unittest.TestCase):
    """End-to-end against a real HTTP server on an ephemeral port, with the
    KV store faked and upstream per-spot ICS fetches mocked."""

    @classmethod
    def setUpClass(cls):
        cls.fake_kv = FakeKvStore()
        cls._kv_patch = patch.multiple(
            'feed_server.kv_store', get=cls.fake_kv.get, set_with_ttl=cls.fake_kv.set_with_ttl,
        )
        cls._kv_patch.start()
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), feed_server.Handler)
        cls.base_url = 'http://127.0.0.1:%d' % cls.server.server_address[1]
        cls.thread = Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls._kv_patch.stop()

    def _post_json(self, path, payload, method='POST'):
        req = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(payload).encode(),
            headers={'Content-Type': 'application/json'},
            method=method,
        )
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())

    def test_post_mix_mints_a_token(self):
        status, body = self._post_json('/mix', {'spots': ['ijmuiden', 'muiderberg']})
        self.assertEqual(status, 200)
        self.assertTrue(body['token'])

    def test_post_mix_rejects_unknown_spot(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self._post_json('/mix', {'spots': ['not-a-real-spot']})
        self.assertEqual(cm.exception.code, 400)

    def test_put_mix_updates_same_token_in_place(self):
        _, body = self._post_json('/mix', {'spots': ['ijmuiden']})
        token = body['token']
        status, body2 = self._post_json('/mix/' + token, {'spots': ['muiderberg']}, method='PUT')
        self.assertEqual(status, 200)
        self.assertEqual(body2['token'], token)
        self.assertEqual(feed_server.spots_for_token(token), ['muiderberg'])

    def test_feed_ics_by_token_merges_the_stored_spots(self):
        _, body = self._post_json('/mix', {'spots': ['ijmuiden', 'muiderberg']})
        token = body['token']
        # Patching urllib.request.urlopen would also intercept this test's own
        # outer request to the local server (same process, same symbol), so
        # that request goes over http.client instead -- only the server's
        # internal per-spot fetch (feed_server.fetch_events) is mocked.
        responses = [FakeResponse(ics_response(['a'])), FakeResponse(ics_response(['b']))]
        with patch('urllib.request.urlopen', side_effect=responses):
            conn = http.client.HTTPConnection('127.0.0.1', self.server.server_address[1])
            conn.request('GET', '/feed.ics?token=' + token)
            resp = conn.getresponse()
            text = resp.read().decode()
            conn.close()
        self.assertTrue(text.startswith('BEGIN:VCALENDAR'))
        self.assertEqual(text.count('BEGIN:VEVENT'), 2)

    def test_feed_ics_by_unknown_token_is_404(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(self.base_url + '/feed.ics?token=does-not-exist')
        self.assertEqual(cm.exception.code, 404)


if __name__ == '__main__':
    unittest.main()
