import unittest
from io import BytesIO
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


if __name__ == '__main__':
    unittest.main()
