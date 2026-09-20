import unittest
from datetime import datetime, timedelta

from forecast import Judgement, EtaEstimate
from main import approaching_to_notify, levels_to_notify

THRESHOLDS = {
    "suiboudan_taiki": 4.00,
    "hanran_chuui": 4.50,
    "hinan_handan": 5.20,
    "hanran_kiken": 5.65,
}

CFG = {
    "forecast": {
        "hysteresis_margin": 0.2,
        "cooldown_minutes": 60,
    }
}


class TestExceedHysteresis(unittest.TestCase):
    def test_first_exceed_is_notified(self):
        now = datetime(2026, 9, 20, 22, 0)
        j = Judgement(current_value=4.05, current_level="suiboudan_taiki", exceeded_levels=["suiboudan_taiki"])
        state = {}
        result = levels_to_notify(j, THRESHOLDS, state, now, CFG)
        self.assertEqual(result, ["suiboudan_taiki"])

    def test_repeated_exceed_within_cooldown_not_renotified(self):
        now = datetime(2026, 9, 20, 22, 0)
        j = Judgement(current_value=4.05, current_level="suiboudan_taiki", exceeded_levels=["suiboudan_taiki"])
        state = {"notified": {"suiboudan_taiki": {"at": (now - timedelta(minutes=10)).isoformat()}}}
        result = levels_to_notify(j, THRESHOLDS, state, now, CFG)
        self.assertEqual(result, [])

    def test_renotified_after_cooldown(self):
        now = datetime(2026, 9, 20, 22, 0)
        j = Judgement(current_value=4.05, current_level="suiboudan_taiki", exceeded_levels=["suiboudan_taiki"])
        state = {"notified": {"suiboudan_taiki": {"at": (now - timedelta(minutes=61)).isoformat()}}}
        result = levels_to_notify(j, THRESHOLDS, state, now, CFG)
        self.assertEqual(result, ["suiboudan_taiki"])

    def test_drop_below_margin_clears_state_for_renotify(self):
        now = datetime(2026, 9, 20, 22, 0)
        # 4.00 - 0.2 = 3.80 を下回ったので記録が消える
        j_drop = Judgement(current_value=3.70, current_level=None, exceeded_levels=[])
        state = {"notified": {"suiboudan_taiki": {"at": (now - timedelta(minutes=5)).isoformat()}}}
        levels_to_notify(j_drop, THRESHOLDS, state, now, CFG)
        self.assertNotIn("suiboudan_taiki", state["notified"])

        # 再度上回れば、クールダウン中でも(記録が消えているため)通知される
        j_up = Judgement(current_value=4.05, current_level="suiboudan_taiki", exceeded_levels=["suiboudan_taiki"])
        result = levels_to_notify(j_up, THRESHOLDS, state, now, CFG)
        self.assertEqual(result, ["suiboudan_taiki"])


class TestApproachingCooldown(unittest.TestCase):
    def test_first_approach_notified(self):
        now = datetime(2026, 9, 20, 22, 0)
        j = Judgement(
            current_value=4.3,
            current_level="suiboudan_taiki",
            approaching=[EtaEstimate(level="hanran_chuui", eta_minutes_low=20, eta_minutes_high=40)],
        )
        state = {}
        result = approaching_to_notify(j, state, now, CFG)
        self.assertEqual(result, ["hanran_chuui"])

    def test_no_longer_approaching_clears_state(self):
        now = datetime(2026, 9, 20, 22, 0)
        j = Judgement(current_value=4.3, current_level="suiboudan_taiki", approaching=[])
        state = {"notified_approaching": {"hanran_chuui": {"at": now.isoformat()}}}
        approaching_to_notify(j, state, now, CFG)
        self.assertNotIn("hanran_chuui", state["notified_approaching"])


if __name__ == "__main__":
    unittest.main()
