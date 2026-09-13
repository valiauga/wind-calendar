"""Minimal RESP (Redis wire protocol) client -- just enough of it for SET/GET
with an expiry against Render's Key Value store, so this project stays free of
third-party dependencies like every other module here. No-ops when REDIS_URL
isn't set, same graceful-degradation pattern as knmi_correction.py without a
station or API key: callers get None/False back rather than an exception.
"""
import os
import socket
import ssl
import urllib.parse

TOKEN_TTL_SECONDS = 60 * 60 * 24 * 180  # unused tokens expire after ~6 months


def _connect():
    url = os.environ.get('REDIS_URL')
    if not url:
        return None
    parsed = urllib.parse.urlsplit(url)
    sock = socket.create_connection((parsed.hostname, parsed.port or 6379), timeout=5)
    if parsed.scheme == 'rediss':
        sock = ssl.create_default_context().wrap_socket(sock, server_hostname=parsed.hostname)
    if parsed.password:
        _command(sock, 'AUTH', parsed.password)
    return sock


def _command(sock, *parts):
    payload = ('*%d\r\n' % len(parts)).encode()
    for part in parts:
        raw = str(part).encode('utf-8')
        payload += ('$%d\r\n' % len(raw)).encode() + raw + b'\r\n'
    sock.sendall(payload)
    return _read_reply(sock)


def _read_exact(sock, length):
    data = b''
    while len(data) < length:
        chunk = sock.recv(length - len(data))
        if not chunk:
            break
        data += chunk
    return data


def _readline(sock):
    line = b''
    while not line.endswith(b'\r\n'):
        chunk = sock.recv(1)
        if not chunk:
            break
        line += chunk
    return line[:-2]


def _read_reply(sock):
    kind = sock.recv(1)
    rest = _readline(sock)
    if kind == b'+':
        return rest.decode()
    if kind == b'-':
        raise RuntimeError(rest.decode())
    if kind == b':':
        return int(rest)
    if kind == b'$':
        length = int(rest)
        if length == -1:
            return None
        data = _read_exact(sock, length + 2)
        return data[:-2].decode('utf-8')
    if kind == b'*':
        count = int(rest)
        if count == -1:
            return None
        return [_read_reply(sock) for _ in range(count)]
    raise RuntimeError('Unexpected RESP reply: ' + kind.decode(errors='replace') + rest.decode(errors='replace'))


def get(key):
    sock = _connect()
    if sock is None:
        return None
    try:
        return _command(sock, 'GET', key)
    finally:
        sock.close()


def set_with_ttl(key, value, ttl_seconds=TOKEN_TTL_SECONDS):
    """Returns False (no-op) when REDIS_URL isn't configured, True once stored."""
    sock = _connect()
    if sock is None:
        return False
    try:
        _command(sock, 'SET', key, value, 'EX', ttl_seconds)
        return True
    finally:
        sock.close()


def rpush(key, value):
    """Appends to a list, creating it if needed. Returns False (no-op) when
    REDIS_URL isn't configured, True once stored."""
    sock = _connect()
    if sock is None:
        return False
    try:
        _command(sock, 'RPUSH', key, value)
        return True
    finally:
        sock.close()


def lrange(key, start, stop):
    """Returns a list of stored values (empty if the key is missing or unset)."""
    sock = _connect()
    if sock is None:
        return []
    try:
        return _command(sock, 'LRANGE', key, start, stop) or []
    finally:
        sock.close()
