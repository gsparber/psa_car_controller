import unittest
from datetime import datetime, timedelta, timezone

from psa_car_controller.web.view import status_text


class TestStatusText(unittest.TestCase):
    def test_format_int(self):
        self.assertEqual("12 345 km", status_text.format_int(12345.4, "km"))
        self.assertEqual("–", status_text.format_int(None, "km"))
        self.assertEqual("–", status_text.format_int(True, "km"))

    def test_format_duration(self):
        self.assertEqual("2 h 10 min", status_text.format_duration("PT2H10M"))
        self.assertEqual("45 min", status_text.format_duration("PT45M"))
        self.assertIsNone(status_text.format_duration("PT0S"))
        self.assertIsNone(status_text.format_duration(None))

    def test_charging_text(self):
        self.assertEqual("Charging · full in 1 h", status_text.charging_text(True, "InProgress", "PT1H"))
        self.assertEqual("Charging", status_text.charging_text(True, "InProgress", None))
        self.assertEqual("Plugged in, not charging", status_text.charging_text(True, "Disconnected", None))
        self.assertEqual("Unplugged", status_text.charging_text(False, "Disconnected", None))
        self.assertIsNone(status_text.charging_text(None, None, None))

    def test_charge_power_kw(self):
        # 50 kWh at 50 % for 125 km -> 0.2 kWh/km, 35 km/h gained -> 7 kW
        self.assertAlmostEqual(7.0, status_text.charge_power_kw(35, 50, 50, 125))
        self.assertIsNone(status_text.charge_power_kw(0, 50, 50, 125))

    def test_car_state_text(self):
        self.assertEqual("Driving · 50 km/h", status_text.car_state_text("Start", True, 50.2))
        self.assertEqual("On", status_text.car_state_text("StartUp", False, 0))
        self.assertEqual("Parked", status_text.car_state_text("Stop", False, None))
        self.assertIsNone(status_text.car_state_text(None, None, None))

    def test_format_updated(self):
        now = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
        today = now - timedelta(hours=1)
        self.assertEqual(f"{today.astimezone():%H:%M}", status_text.format_updated(today, now))
        self.assertIn("Oct", status_text.format_updated(now - timedelta(days=3), now))
        self.assertIsNone(status_text.format_updated(None))

    def test_position(self):
        self.assertEqual((49.9, 9.6), status_text.position([9.6, 49.9, 21]))
        self.assertIsNone(status_text.position([None, None, None]))
        self.assertIsNone(status_text.position(None))


if __name__ == "__main__":
    unittest.main()
