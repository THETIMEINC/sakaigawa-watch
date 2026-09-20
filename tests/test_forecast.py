import unittest
from datetime import datetime, timedelta

from fetch import Reading
from forecast import (
    RainContext,
    SchedulePoint,
    build_rain_ratio_schedule,
    clean_readings,
    etas_from_schedule,
    fit_trend,
    judge,
    should_run_full_check,
    valid_points,
)

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
        "stale_after_minutes": 90,
        "rain_floor_mm": 1.0,
        "rain_ratio_max": 3.0,
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

    def test_delayed_feed_still_computes_eta(self):
        # 県ページの配信が25分遅れていても、最新観測時刻を基準にETAが出ること
        data_latest = datetime(2026, 9, 20, 21, 35)
        wall_now = data_latest + timedelta(minutes=25)
        values = [4.10 + 0.05 * i for i in range(6)]
        readings = series(data_latest, 50, values)
        cleaned = clean_readings(readings, max_jump_per_10min=0.5)
        j = judge(cleaned, wall_now, THRESHOLDS, CFG)
        self.assertIn("suiboudan_taiki", j.exceeded_levels)
        levels = [e.level for e in j.approaching]
        self.assertIn("hanran_chuui", levels)

    def test_very_stale_feed_suppresses_forecast(self):
        # 配信が90分以上止まっている場合は、古いデータから予測を作らない
        data_latest = datetime(2026, 9, 20, 20, 0)
        wall_now = data_latest + timedelta(minutes=120)
        values = [4.10 + 0.05 * i for i in range(6)]
        readings = series(data_latest, 50, values)
        cleaned = clean_readings(readings, max_jump_per_10min=0.5)
        j = judge(cleaned, wall_now, THRESHOLDS, CFG)
        self.assertEqual(j.approaching, [])
        self.assertFalse(j.surge_10min)
        self.assertFalse(j.surge_30min)


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


def rising_trend(now):
    """0.005m/分（10分で0.05m）で上昇するトレンド。現在値4.35m。"""
    values = [4.10 + 0.05 * i for i in range(6)]
    readings = series(now, 50, values)
    cleaned = clean_readings(readings, max_jump_per_10min=0.5)
    return fit_trend(cleaned, now, window_minutes=60, min_points=5)


class TestRainRatioSchedule(unittest.TestCase):
    def test_tapering_rain_flattens_the_forecast(self):
        now = datetime(2026, 9, 20, 22, 0)
        trend = rising_trend(now)
        # 直近60分で5mm（15分あたり1.25mm）に対し、最初の45分は比率2.0相当の
        # 強い雨、その後は無降雨という予報
        rain = RainContext(
            recent_mm=5.0,
            future_buckets=[(0, 2.5), (15, 2.5), (30, 2.5), (45, 2.5)] + [(m, 0.0) for m in range(60, 180, 15)],
        )
        schedule = build_rain_ratio_schedule(trend, current_value=4.35, rain=rain, cfg=CFG)
        self.assertTrue(schedule)
        # 雨が止んだ後（60分以降）は水位が増え続けない（平坦化）
        value_at_60 = next(p.value_mid for p in schedule if abs((p.time - now).total_seconds() / 60 - 60) < 1)
        value_at_final = schedule[-1].value_mid
        self.assertAlmostEqual(value_at_60, value_at_final, places=6)
        # 雨が強かった前半では上昇している
        self.assertGreater(value_at_60, 4.35)

    def test_sustained_rain_close_to_ratio_one_matches_linear(self):
        now = datetime(2026, 9, 20, 22, 0)
        trend = rising_trend(now)
        # 直近60分で5mm、将来も同じ15分あたり1.25mmが続く予報＝比率ほぼ1
        rain = RainContext(recent_mm=5.0, future_buckets=[(m, 1.25) for m in range(0, 180, 15)])
        schedule = build_rain_ratio_schedule(trend, current_value=4.35, rain=rain, cfg=CFG)
        expected_final = 4.35 + trend.slope_per_min * 180
        self.assertAlmostEqual(schedule[-1].value_mid, expected_final, places=6)

    def test_non_rain_driven_rise_falls_back_to_linear(self):
        now = datetime(2026, 9, 20, 22, 0)
        trend = rising_trend(now)
        # 直近降雨がrain_floor_mm未満＝上昇は降雨由来と言えない。
        # 将来降雨が0でも比率モデルは適用せず線形のまま
        rain = RainContext(recent_mm=0.2, future_buckets=[(m, 0.0) for m in range(0, 180, 15)])
        schedule = build_rain_ratio_schedule(trend, current_value=4.35, rain=rain, cfg=CFG)
        expected_final = 4.35 + trend.slope_per_min * 180
        self.assertAlmostEqual(schedule[-1].value_mid, expected_final, places=6)

    def test_rain_none_falls_back_to_linear(self):
        now = datetime(2026, 9, 20, 22, 0)
        trend = rising_trend(now)
        schedule = build_rain_ratio_schedule(trend, current_value=4.35, rain=None, cfg=CFG)
        expected_final = 4.35 + trend.slope_per_min * 180
        self.assertAlmostEqual(schedule[-1].value_mid, expected_final, places=6)

    def test_falling_trend_produces_no_schedule(self):
        now = datetime(2026, 9, 20, 22, 0)
        values = [4.50 - 0.02 * i for i in range(6)]
        readings = series(now, 50, values)
        cleaned = clean_readings(readings, max_jump_per_10min=0.5)
        trend = fit_trend(cleaned, now, window_minutes=60, min_points=5)
        schedule = build_rain_ratio_schedule(trend, current_value=4.40, rain=None, cfg=CFG)
        self.assertEqual(schedule, [])


class TestEtasFromSchedule(unittest.TestCase):
    def test_crossing_detected(self):
        now = datetime(2026, 9, 20, 22, 0)
        schedule = [
            SchedulePoint(time=now, value_low=4.35, value_mid=4.35, value_high=4.35),
            SchedulePoint(time=now + timedelta(minutes=15), value_low=4.40, value_mid=4.45, value_high=4.50),
            SchedulePoint(time=now + timedelta(minutes=30), value_low=4.45, value_mid=4.55, value_high=4.65),
        ]
        etas = etas_from_schedule(schedule, current_value=4.35, thresholds=THRESHOLDS)
        levels = {e.level: e for e in etas}
        self.assertIn("hanran_chuui", levels)  # 4.50はhighが15分時点で到達
        self.assertEqual(levels["hanran_chuui"].eta_minutes_low, 15)

    def test_no_crossing_within_horizon_is_suppressed(self):
        now = datetime(2026, 9, 20, 22, 0)
        # 平坦化して4.40mで頭打ち＝氾濫危険水位(5.65)には届かない
        schedule = [SchedulePoint(time=now + timedelta(minutes=15 * i), value_low=4.40, value_mid=4.40, value_high=4.40) for i in range(5)]
        etas = etas_from_schedule(schedule, current_value=4.35, thresholds=THRESHOLDS)
        levels = {e.level for e in etas}
        self.assertNotIn("hanran_kiken", levels)


if __name__ == "__main__":
    unittest.main()
