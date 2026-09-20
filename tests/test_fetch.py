import unittest
from datetime import datetime
from pathlib import Path

from fetch import FetchError, parse_station_html

FIXTURE = Path(__file__).parent / "fixtures" / "station_309.html"
FIXTURE_EXCEEDED = Path(__file__).parent / "fixtures" / "station_309_exceeded.html"


class TestParseStationHtml(unittest.TestCase):
    def setUp(self):
        self.html = FIXTURE.read_text(encoding="utf-8")

    def test_generated_at(self):
        snap = parse_station_html(self.html)
        self.assertEqual(snap.generated_at, datetime(2026, 9, 20, 22, 25))

    def test_readings_count_and_order(self):
        snap = parse_station_html(self.html)
        self.assertEqual(len(snap.readings), 24)
        times = [r.observed_at for r in snap.readings]
        self.assertEqual(times, sorted(times))

    def test_first_and_last_reading(self):
        snap = parse_station_html(self.html)
        first, last = snap.readings[0], snap.readings[-1]
        self.assertEqual(first.observed_at, datetime(2026, 9, 20, 18, 30))
        self.assertEqual(first.value, 2.56)
        self.assertEqual(last.observed_at, datetime(2026, 9, 20, 22, 20))
        self.assertEqual(last.value, 3.80)

    def test_thresholds(self):
        snap = parse_station_html(self.html)
        self.assertEqual(
            snap.thresholds,
            {
                "suiboudan_taiki": 4.00,
                "hanran_chuui": 4.50,
                "hinan_handan": 5.20,
                "hanran_kiken": 5.65,
            },
        )

    def test_broken_html_raises(self):
        with self.assertRaises(FetchError):
            parse_station_html("<html><body>no table here</body></html>")


class TestParseStationHtmlWhenExceeded(unittest.TestCase):
    """水位が警戒水位を超えると<td>に class="stageLv1" 等が付与され、
    それ以降の行が解析から落ちる回帰バグの再発防止。"""

    def setUp(self):
        self.html = FIXTURE_EXCEEDED.read_text(encoding="utf-8")

    def test_all_24_rows_parsed_even_after_threshold_class_appears(self):
        snap = parse_station_html(self.html)
        self.assertEqual(len(snap.readings), 24)

    def test_last_reading_after_exceeding_threshold(self):
        snap = parse_station_html(self.html)
        last = snap.readings[-1]
        self.assertEqual(last.observed_at, datetime(2026, 9, 21, 0, 0))
        self.assertEqual(last.value, 4.20)


if __name__ == "__main__":
    unittest.main()
