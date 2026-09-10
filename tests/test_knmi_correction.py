import copy
import json
import unittest
from datetime import datetime
from io import BytesIO
from unittest.mock import patch
from zoneinfo import ZoneInfo

import knmi_correction as correction
from wind import SPOTS

ZONE = ZoneInfo('Europe/Amsterdam')


def forecast_for(now, speed=10.0, hours=24):
    from datetime import timedelta
    base = now.replace(minute=0, second=0, microsecond=0, tzinfo=None)
    times, speeds, gusts, dirs = [], [], [], []
    for i in range(hours):
        times.append((base + timedelta(hours=i)).isoformat())
        speeds.append(speed)
        gusts.append(speed * 1.2)
        dirs.append(285)
    return {'hourly': {'time': times, 'wind_speed_10m': speeds,
                       'wind_gusts_10m': gusts, 'wind_direction_10m': dirs},
            'daily': {'time': [], 'sunrise': [], 'sunset': []}}


class FakeResponse:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode()

    def __enter__(self):
        return BytesIO(self._payload)

    def __exit__(self, *exc):
        return False


def knmi_payload(values):
    return {'coverages': [{'ranges': {'ff': {'values': values}}}]}


class CorrectionTests(unittest.TestCase):
    def setUp(self):
        self.spot = copy.deepcopy(SPOTS[0])
        self.assertIsNotNone(self.spot['knmiStationId'])
        self.now = datetime(2026, 9, 7, 12, 0, tzinfo=ZONE)

    def test_no_station_is_a_noop(self):
        spot = copy.deepcopy(SPOTS[3])  # muiderberg has no KNMI station.
        self.assertIsNone(spot['knmiStationId'])
        data = forecast_for(self.now)
        result = correction.apply(data, spot, self.now, api_key='key')
        self.assertIs(result, data)

    def test_no_api_key_is_a_noop(self):
        data = forecast_for(self.now)
        result = correction.apply(data, self.spot, self.now, api_key=None)
        self.assertIs(result, data)

    def test_ratio_is_clamped(self):
        # 20 kt observed / 10 kt forecast = 2.0x, clamped to 1.35x.
        with patch('urllib.request.urlopen', return_value=FakeResponse(knmi_payload([20 / correction.MS_TO_KNOTS]))):
            ratio = correction.current_ratio(self.spot, forecast_for(self.now, speed=10), self.now, 'key')
        self.assertAlmostEqual(ratio, 1.35)

    def test_ratio_decays_with_horizon(self):
        with patch('urllib.request.urlopen', return_value=FakeResponse(knmi_payload([13 / correction.MS_TO_KNOTS]))):
            data = forecast_for(self.now, speed=10)
            corrected = correction.apply(data, self.spot, self.now, api_key='key')
        # Ratio ~1.3 at hour 0 should apply almost fully; five hours out it should be
        # meaningfully closer to the raw forecast.
        near_term = corrected['hourly']['wind_speed_10m'][0]
        far_term = corrected['hourly']['wind_speed_10m'][5]
        self.assertGreater(near_term, far_term)
        self.assertAlmostEqual(near_term, 13, delta=0.5)

    def test_missing_observation_is_a_noop(self):
        with patch('urllib.request.urlopen', return_value=FakeResponse(knmi_payload([]))):
            data = forecast_for(self.now)
            result = correction.apply(data, self.spot, self.now, api_key='key')
        self.assertIs(result, data)

    def test_network_failure_is_a_noop(self):
        import urllib.error
        with patch('urllib.request.urlopen', side_effect=urllib.error.URLError('down')):
            data = forecast_for(self.now)
            result = correction.apply(data, self.spot, self.now, api_key='key')
        self.assertIs(result, data)


if __name__ == '__main__':
    unittest.main()
