import unittest
from datetime import datetime, timedelta

from fetch import Reading
from forecast import clean_readings, fit_trend, judge, should_run_full_check, valid_points

THRESHOLDS = {
    "suiboudan_taiki": 4.00,
    "hanran_chuui": 4.50,
    "hinan_handan": 5.20,
    "hanran_kiken": 5.65,
}

CFG = {
    "forecast": {
        "trend_window_minutes": 60,
        "trend_window_minutes_min": 30,
        "min_valid_points": 5,
        "surge_10min": 0.15,
        "surge_30min": 0.3,
        "eta_warn_minutes_chuui": 180,
        "eta_warn_minutes_handan": 120,
        "eta_warn_minutes_kiken": 90,
        "hysteresis_margin": 0.2,
        "cooldown_minutes": 60,
    }
}


def series(now, start_offset_min, values, step=10):
    base = now - timedelta(minutes=start_offset_min)
    return [Reading(observed_at=base + timedelta(minutes=step * i), value=v) for i, v in enumerate(values)]


class TestCleanReadings(unittest.TestCase):
    def test_out_of_range_is_suspect(self):
        now = datetime(2026, 9, 20, 22, 0)
        readings = series(now, 20, [3.0, -1.0, 3.1])
        cleaned = clean_readings(readings, max_jump_per_10min=0.5)
        self.assertFalse(cleaned[0].suspect)
        self.assertTrue(cleaned[1].suspect)
        self.assertFalse(cleaned[2].suspect)

    def test_sudden_jump_is_suspect(self):
        now = datetime(2026, 9, 20, 22, 0)
        readings = series(now, 20, [3.0, 8.0, 3.1])
        cleaned = clean_readings(readings, max_jump_per_10min=0.5)
        self.assertTrue(cleaned[1].suspect)

    def test_valid_points_excludes_suspect(self):
        now = datetime(2026, 9, 20, 22, 0)
        readings = series(now, 20, [3.0, 8.0, 3.1])
        cleaned = clean_readings(readings, max_jump_per_10min=0.5)
        self.assertEqual(len(valid_points(cleaned)), 2)


class TestFitTrend(unittest.TestCase):
    def test_rising_trend_positive_slope(self):
        now = datetime(2026, 9, 20, 22, 0)
        values = [4.00 + 0.05 * i for i in range(6)]
        readings = series(now, 50, values)
        cleaned = clean_readings(readings, max_jump_per_10min=0.5)
        trend = fit_trend(cleaned, now, window_minutes=60, min_points=5)
        self.assertIsNotNone(trend)
        self.assertGreater(trend.slope_per_min, 0)

    def test_insufficient_points_returns_none(self):
        now = datetime(2026, 9, 20, 22, 0)
        readings = series(now, 20, [4.0, 4.1])
        cleaned = clean_readings(readings, max_jump_per_10min=0.5)
        trend = fit_trend(cleaned, now, window_minutes=60, min_points=5)
        self.assertIsNone(trend)


class TestJudge(unittest.TestCase):
    def test_below_base_level_no_eta_evaluated(self):
        now = datetime(2026, 9, 20, 22, 0)
        values = [2.5 + 0.05 * i for i in range(6)]
        readings = series(now, 50, values)
        cleaned = clean_readings(readings, max_jump_per_10min=0.5)
        j = judge(cleaned, now, THRESHOLDS, CFG)
        self.assertEqual(j.exceeded_levels, [])
        self.assertEqual(j.approaching, [])

    def test_exceeds_suiboudan_and_approaches_chuui(self):
        now = datetime(2026, 9, 20, 22, 0)
        values = [4.10 + 0.05 * i for i in range(6)]  # 30分でchuui(4.5)に届く上昇
        readings = series(now, 50, values)
        cleaned = clean_readings(readings, max_jump_per_10min=0.5)
        j = judge(cleaned, now, THRESHOLDS, CFG)
        self.assertIn("suiboudan_taiki", j.exceeded_levels)
        levels = [e.level for e in j.approaching]
        self.assertIn("hanran_chuui", levels)

    def test_falling_trend_no_eta(self):
        now = datetime(2026, 9, 20, 22, 0)
        values = [4.50 - 0.02 * i for i in range(6)]
        readings = series(now, 50, values)
        cleaned = clean_readings(readings, max_jump_per_10min=0.5)
        j = judge(cleaned, now, THRESHOLDS, CFG)
        self.assertEqual(j.approaching, [])

    def test_surge_detection(self):
        now = datetime(2026, 9, 20, 22, 0)
        values = [4.10, 4.11, 4.12, 4.13, 4.14, 4.35]  # 直近10分で+0.21m
        readings = series(now, 50, values)
        cleaned = clean_readings(readings, max_jump_per_10min=1.0)
        j = judge(cleaned, now, THRESHOLDS, CFG)
        self.assertTrue(j.surge_10min)

    def test_no_data_flagged(self):
        now = datetime(2026, 9, 20, 22, 0)
        j = judge([], now, THRESHOLDS, CFG)
        self.assertTrue(j.no_data)


class TestShouldRunFullCheck(unittest.TestCase):
    def setUp(self):
        self.cfg = {
            "thresholds": THRESHOLDS,
            "schedule": {"normal_interval_minutes": 30, "escalate_margin": 0.5},
        }

    def test_first_run_always_full(self):
        now = datetime(2026, 9, 20, 22, 0)
        self.assertTrue(should_run_full_check(None, None, now, self.cfg))

    def test_normal_level_skipped_within_interval(self):
        now = datetime(2026, 9, 20, 22, 20)
        last_checked = datetime(2026, 9, 20, 22, 10)  # 10分前
        self.assertFalse(should_run_full_check(3.0, last_checked, now, self.cfg))

    def test_normal_level_runs_after_interval(self):
        now = datetime(2026, 9, 20, 22, 45)
        last_checked = datetime(2026, 9, 20, 22, 10)  # 35分前
        self.assertTrue(should_run_full_check(3.0, last_checked, now, self.cfg))

    def test_elevated_level_always_runs(self):
        # suiboudan_taiki(4.00) - margin(0.5) = 3.50 以上なら毎回実行
        now = datetime(2026, 9, 20, 22, 20)
        last_checked = datetime(2026, 9, 20, 22, 10)  # 10分前でも
        self.assertTrue(should_run_full_check(3.60, last_checked, now, self.cfg))


if __name__ == "__main__":
    unittest.main()
