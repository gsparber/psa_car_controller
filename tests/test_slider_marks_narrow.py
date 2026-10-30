"""The 11 date marks of the slider on /v1/stats overlap on a phone. The browser decides, so this only checks the rules
which were measured: first, middle and last mark on a phone, every second one up to a laptop."""
import re
import unittest
from datetime import datetime
from pathlib import Path

from psa_car_controller.web.tools.utils import get_marks_from_start_end

CSS = Path(__file__).parent.parent / "psa_car_controller" / "web" / "assets" / "10_mobile.css"


def rule(media):
    match = re.search(re.escape(media) + r"\s*\{\s*#date-slider \.dash-slider-mark([^{]*)\{\s*display: none;",
                      CSS.read_text())
    return match.group(1).strip() if match else None


def shown_on_phone(count):
    return [i for i in range(1, count + 1) if (i - 1) % 5 == 0]


class TestSliderMarks(unittest.TestCase):

    def test_phone(self):
        self.assertEqual(":not(:nth-of-type(5n+1))", rule("@media (max-width: 575.98px)"))

    def test_tablet(self):
        self.assertEqual(":nth-of-type(even)", rule("@media (min-width: 576px) and (max-width: 991.98px)"))

    def test_rules_keep_the_first_and_last_mark(self):
        """They count on 11 marks: with another number the last date would go."""
        for start, end in ((datetime(2026, 1, 1), datetime(2026, 10, 30, 8, 17)),
                           (datetime(2026, 10, 1, 8), datetime(2026, 10, 2, 22))):
            with self.subTest(start=start):
                count = len(get_marks_from_start_end(start, end))
                self.assertEqual(11, count)
                self.assertEqual(count, shown_on_phone(count)[-1])
                self.assertEqual(1, count % 2)
