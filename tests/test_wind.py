import copy
import unittest
from datetime import date, datetime

from wind import SPOTS, ZONE, block_span, windows


def data_for(day='2026-09-07', speeds=None, sunset='19:30'):
    speeds = speeds or [20] * 24
    return {'hourly': {'time': [f'{day}T{h:02}:00' for h in range(24)],
                       'wind_speed_10m': speeds, 'wind_gusts_10m': [24] * 24,
                       'wind_direction_10m': [285] * 24},
            'daily': {'time': [day], 'sunrise': [day + 'T07:00'], 'sunset': [day + 'T' + sunset]}}


class WindTests(unittest.TestCase):
    def test_daylight_and_minimum_duration(self):
        blocks = windows(SPOTS[0], data_for(), date(2026, 9, 7))
        self.assertEqual([(b['start'], b['end']) for b in blocks], [(7, 19.5)])
        data = data_for(speeds=[20 if h == 12 else 0 for h in range(24)])
        self.assertEqual(windows(SPOTS[0], data, date(2026, 9, 7)), [])

    def test_offshore_and_gust_filter(self):
        data = data_for()
        data['hourly']['wind_direction_10m'] = [105] * 24
        self.assertEqual(windows(SPOTS[0], data, date(2026, 9, 7)), [])
        data = data_for()
        data['hourly']['wind_gusts_10m'] = [40] * 24
        self.assertEqual(windows(SPOTS[0], data, date(2026, 9, 7)), [])

    def test_missing_values_are_not_zero_wind(self):
        data = data_for()
        data['hourly']['wind_speed_10m'][4] = None
        with self.assertRaises(ValueError):
            windows(SPOTS[0], data, date(2026, 9, 7))

    def test_season_closure(self):
        spot = copy.deepcopy(SPOTS[5])
        self.assertEqual(spot['id'], 'medemblik')
        spot['shoreNormal'] = 285
        data = data_for('2026-10-01')
        self.assertEqual(windows(spot, data, date(2026, 10, 1)), [])

    def test_block_span_is_local_aware_datetime(self):
        block = windows(SPOTS[0], data_for(), date(2026, 9, 7))[0]
        begin, end = block_span(block)
        self.assertEqual((begin, end), (datetime(2026, 9, 7, 7, 0, tzinfo=ZONE),
                                         datetime(2026, 9, 7, 19, 30, tzinfo=ZONE)))

    def test_no_travel_gating(self):
        """The source project skips marginal wind at far spots; this fork qualifies
        purely on wind/direction/season, regardless of distance from any one home."""
        spot = copy.deepcopy(SPOTS[0])
        spot['minWind'], spot['strongThreshold'] = 10, 19
        data = data_for(speeds=[15] * 24)  # Below strongThreshold, but above minWind.
        data['hourly']['wind_gusts_10m'] = [18] * 24  # Keep gust factor within limits.
        self.assertTrue(windows(spot, data, date(2026, 9, 7)))


if __name__ == '__main__':
    unittest.main()
