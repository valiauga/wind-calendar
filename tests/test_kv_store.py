import unittest
from unittest.mock import patch

import kv_store


class FakeSocket:
    """Buffers RESP requests and answers with pre-scripted replies."""

    def __init__(self, replies):
        self._replies = list(replies)
        self.sent = b''
        self.closed = False

    def sendall(self, data):
        self.sent += data

    def recv(self, n):
        if not self._replies:
            return b''
        chunk = self._replies[0][:n]
        self._replies[0] = self._replies[0][n:]
        if not self._replies[0]:
            self._replies.pop(0)
        return chunk

    def close(self):
        self.closed = True


class KvStoreTests(unittest.TestCase):
    def test_get_returns_none_without_redis_url(self):
        with patch.dict('os.environ', {}, clear=True):
            self.assertIsNone(kv_store.get('mix:abc'))

    def test_set_with_ttl_returns_false_without_redis_url(self):
        with patch.dict('os.environ', {}, clear=True):
            self.assertFalse(kv_store.set_with_ttl('mix:abc', '["ijmuiden"]'))

    def test_get_parses_bulk_string_reply(self):
        sock = FakeSocket([b'$13\r\n["ijmuiden"]\r\n'])
        with patch.object(kv_store, '_connect', return_value=sock):
            result = kv_store.get('mix:abc')
        self.assertEqual(result, '["ijmuiden"]')
        self.assertTrue(sock.closed)

    def test_get_parses_nil_reply(self):
        sock = FakeSocket([b'$-1\r\n'])
        with patch.object(kv_store, '_connect', return_value=sock):
            self.assertIsNone(kv_store.get('mix:missing'))

    def test_set_with_ttl_parses_simple_string_reply(self):
        sock = FakeSocket([b'+OK\r\n'])
        with patch.object(kv_store, '_connect', return_value=sock):
            self.assertTrue(kv_store.set_with_ttl('mix:abc', '["ijmuiden"]', ttl_seconds=60))
        self.assertIn(b'SET', sock.sent)
        self.assertIn(b'EX', sock.sent)

    def test_error_reply_raises(self):
        sock = FakeSocket([b'-ERR bad thing\r\n'])
        with patch.object(kv_store, '_connect', return_value=sock):
            with self.assertRaises(RuntimeError):
                kv_store.get('mix:abc')

    def test_rpush_returns_false_without_redis_url(self):
        with patch.dict('os.environ', {}, clear=True):
            self.assertFalse(kv_store.rpush('feedback:all', '{}'))

    def test_rpush_parses_integer_reply(self):
        sock = FakeSocket([b':3\r\n'])
        with patch.object(kv_store, '_connect', return_value=sock):
            self.assertTrue(kv_store.rpush('feedback:all', '{"message":"hi"}'))
        self.assertIn(b'RPUSH', sock.sent)

    def test_lrange_returns_empty_without_redis_url(self):
        with patch.dict('os.environ', {}, clear=True):
            self.assertEqual(kv_store.lrange('feedback:all', 0, -1), [])

    def test_lrange_parses_array_reply(self):
        sock = FakeSocket([b'*2\r\n$7\r\n{"a":1}\r\n$7\r\n{"b":2}\r\n'])
        with patch.object(kv_store, '_connect', return_value=sock):
            result = kv_store.lrange('feedback:all', 0, -1)
        self.assertEqual(result, ['{"a":1}', '{"b":2}'])

    def test_lrange_parses_empty_array_reply(self):
        sock = FakeSocket([b'*0\r\n'])
        with patch.object(kv_store, '_connect', return_value=sock):
            self.assertEqual(kv_store.lrange('feedback:all', 0, -1), [])


if __name__ == '__main__':
    unittest.main()
