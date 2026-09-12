import unittest
import urllib.error
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from unittest.mock import patch

import tide
from wind import SPOTS, block_span, windows


def data_for(day='2026-09-07', speeds=None, sunset='19:30'):
    speeds = speeds or [20] * 24
    return {'hourly': {'time': [f'{day}T{h:02}:00' for h in range(24)],
                       'wind_speed_10m': speeds, 'wind_gusts_10m': [24] * 24,
                       'wind_direction_10m': [268] * 24},
            'daily': {'time': [day], 'sunrise': [day + 'T07:00'], 'sunset': [day + 'T' + sunset]}}


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return BytesIO(self._payload)

    def __exit__(self, *exc):
        return False


def sine_series(start, hours=168, peak_every_hours=12.4, amplitude=100):
    """A synthetic tide-shaped curve: smooth peaks/troughs at a realistic cadence."""
    import math
    points, t = [], start
    while t < start + timedelta(hours=hours):
        minutes_in = (t - start).total_seconds() / 60
        value = amplitude * math.sin(2 * math.pi * minutes_in / (peak_every_hours * 60))
        points.append((t, round(value, 1)))
        t += timedelta(minutes=10)
    return points


class TideFetchTests(unittest.TestCase):
    def test_fetch_parses_series_from_response(self):
        body = ('{"series":[{"data":[' +
                '{"dateTime":"2026-09-12T00:00:00Z","value":10.0},' +
                '{"dateTime":"2026-09-12T00:10:00Z","value":12.0},' +
                '{"dateTime":"2026-09-12T00:20:00Z","value":9.0}' +
                ']}]}').encode()
        with patch('urllib.request.urlopen', return_value=FakeResponse(body)):
            series = tide.fetch_astronomical_series('hoekvanholland')
        self.assertEqual(len(series), 3)
        self.assertEqual(series[0], (datetime(2026, 9, 12, 0, 0, tzinfo=timezone.utc), 10.0))

    def test_fetch_returns_none_on_network_error(self):
        with patch('urllib.request.urlopen', side_effect=urllib.error.URLError('boom')):
            self.assertIsNone(tide.fetch_astronomical_series('hoekvanholland'))

    def test_fetch_returns_none_on_too_few_points(self):
        body = b'{"series":[{"data":[{"dateTime":"2026-09-12T00:00:00Z","value":10.0}]}]}'
        with patch('urllib.request.urlopen', return_value=FakeResponse(body)):
            self.assertIsNone(tide.fetch_astronomical_series('hoekvanholland'))

    def test_fetch_returns_none_on_malformed_response(self):
        with patch('urllib.request.urlopen', return_value=FakeResponse(b'not json')):
            self.assertIsNone(tide.fetch_astronomical_series('hoekvanholland'))


class HighTideWindowsTests(unittest.TestCase):
    def test_ignores_a_secondary_wiggle_in_the_low_water_trough(self):
        # Hoek van Holland's real curve has exactly this shape: a shallow-water
        # distortion creates a genuine local maximum deep in the low-water
        # trough (nowhere near high tide), which a shape-only peak check would
        # wrongly treat as a high-tide peak.
        start = datetime(2026, 9, 12, tzinfo=timezone.utc)
        series = [(start + timedelta(minutes=30 * i), v) for i, v in enumerate([
            -18, -24, -28, -27, -25, -25, -26, -15, 21, 73, 114, 126, 117, 106,
        ])]
        windows = tide.high_tide_windows(series, tolerance_hours=2)
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0][0], start + timedelta(minutes=30 * 11) - timedelta(hours=2))

    def test_finds_peaks_in_a_smooth_curve(self):
        start = datetime(2026, 9, 12, tzinfo=timezone.utc)
        series = sine_series(start)
        tide_windows = tide.high_tide_windows(series, tolerance_hours=1.5)
        # Roughly two high tides per day over a week.
        self.assertGreaterEqual(len(tide_windows), 10)
        self.assertLessEqual(len(tide_windows), 15)
        for w_start, w_end in tide_windows:
            self.assertEqual(w_end - w_start, timedelta(hours=3))


class BlockQualifiesTests(unittest.TestCase):
    def setUp(self):
        self.spot = next(s for s in SPOTS if s['id'] == 'zandmotor')
        self.block = windows(self.spot, data_for(), date(2026, 9, 7))[0]
        self.begin, self.end = block_span(self.block)

    def test_no_series_is_ungated(self):
        self.assertTrue(tide.block_qualifies(None, self.block))

    def test_block_outside_series_horizon_is_ungated(self):
        far_future = [(self.begin.astimezone(timezone.utc) + timedelta(days=30) + timedelta(hours=h), 50.0)
                      for h in range(5)]
        self.assertTrue(tide.block_qualifies(far_future, self.block))

    def test_block_overlapping_a_high_tide_qualifies(self):
        peak = self.begin.astimezone(timezone.utc) + timedelta(hours=1)
        series = [(peak - timedelta(hours=1), 0.0), (peak, 100.0), (peak + timedelta(hours=1), 0.0)]
        self.assertTrue(tide.block_qualifies(series, self.block, tolerance_hours=1.5))

    def test_block_missing_every_high_tide_is_excluded(self):
        peak = self.begin.astimezone(timezone.utc) - timedelta(hours=8)
        series = [(peak - timedelta(hours=1), 0.0), (peak, 100.0), (peak + timedelta(hours=1), 0.0),
                  (self.end.astimezone(timezone.utc) + timedelta(hours=8), 0.0)]
        self.assertFalse(tide.block_qualifies(series, self.block, tolerance_hours=1.5))


if __name__ == '__main__':
    unittest.main()
