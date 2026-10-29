import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from psa_car_controller.psacc.application.vehicle_picture import VehiclePicture
from psa_car_controller.web.view import insights_data, precond_programs

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)


class TestPrecondPrograms(unittest.TestCase):
    def test_from_status(self):
        programs = precond_programs.from_status([
            {"enabled": True, "slot": 2, "recurrence": "Daily", "start": "PT6H30M",
             "occurence": {"day": ["Mon", "Tue", "Wed", "Thu"]}}])
        self.assertEqual({"day": [1, 1, 1, 1, 0, 0, 0], "hour": 6, "minute": 30, "on": 1}, programs["program2"])
        self.assertFalse(precond_programs.is_set(programs["program1"]))
        self.assertFalse(precond_programs.is_default(programs))

    def test_from_status_empty(self):
        self.assertTrue(precond_programs.is_default(precond_programs.from_status(None)))

    def test_days_text(self):
        self.assertEqual("Mon–Thu", precond_programs.days_text([1, 1, 1, 1, 0, 0, 0]))
        self.assertEqual("Weekdays", precond_programs.days_text([1, 1, 1, 1, 1, 0, 0]))
        self.assertEqual("Weekend", precond_programs.days_text([0, 0, 0, 0, 0, 1, 1]))
        self.assertEqual("Every day", precond_programs.days_text([1] * 7))
        self.assertEqual("Mon, Wed", precond_programs.days_text([1, 0, 1, 0, 0, 0, 0]))

    def test_build(self):
        programs = precond_programs.build([True, False, True, False], ["06:00", "", "07:15", None],
                                          [[0, 1, 2, 3], [], [], None])
        self.assertEqual({"day": [1, 1, 1, 1, 0, 0, 0], "hour": 6, "minute": 0, "on": 1}, programs["program1"])
        # no day: stored as unset even if switched on
        self.assertEqual(precond_programs.UNSET_HOUR, programs["program3"]["hour"])
        self.assertEqual("Mon–Thu · 06:00", precond_programs.program_text(programs["program1"]))


class TestInsightsData(unittest.TestCase):
    def setUp(self):
        self.trips = insights_data.trip_rows([
            {"start_at": NOW - timedelta(days=1), "distance": 20, "consumption": 3, "consumption_km": 15},
            {"start_at": NOW - timedelta(days=2), "distance": 30, "consumption": 4.5, "consumption_km": 15},
            {"start_at": NOW - timedelta(days=40), "distance": 100, "consumption": 20, "consumption_km": 20},
        ])
        self.charges = insights_data.charge_rows([
            {"start_at": NOW - timedelta(days=3), "stop_at": NOW - timedelta(days=3) + timedelta(hours=2),
             "kw": 14, "price": 4.2, "VIN": "A"},
            {"start_at": NOW - timedelta(days=5), "stop_at": None, "kw": 10, "price": None, "VIN": "B"},
        ])

    def test_trip_totals(self):
        totals = insights_data.trip_totals(insights_data.in_period(self.trips, "7d", NOW))
        self.assertEqual(50, totals["distance"])
        self.assertEqual(2, totals["count"])
        self.assertAlmostEqual(15.0, totals["consumption"])
        self.assertEqual(3, insights_data.trip_totals(insights_data.in_period(self.trips, "all", NOW))["count"])

    def test_charge_totals(self):
        totals = insights_data.charge_totals(self.charges)
        self.assertEqual(24, totals["energy"])
        self.assertAlmostEqual(4.2, totals["cost"])
        self.assertAlmostEqual(0.3, totals["price_per_kwh"])
        self.assertAlmostEqual(7.0, totals["avg_power"])
        self.assertEqual(1, len(insights_data.charge_rows([{"VIN": "A", "kw": 1, "start_at": NOW},
                                                           {"VIN": "B", "kw": 1, "start_at": NOW}], "A")))

    def test_series_fills_days(self):
        x, y = insights_data.series(self.trips, "distance", "7d", NOW)
        self.assertEqual(8, len(x))
        self.assertEqual(50, sum(y))

    def test_series_months(self):
        x, y = insights_data.series(self.trips, "distance", "all", NOW)
        self.assertEqual(150, sum(y))
        self.assertTrue(all(day.day == 1 for day in x))


class TestVehiclePicture(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        vehicle = MagicMock(vin="VIN1", pictures=["https://example.org/car.png"])
        self.psacc = MagicMock()
        self.psacc.api.return_value.get_vehicles_by_device.return_value.embedded.vehicles = [vehicle]

    @patch("psa_car_controller.psacc.application.vehicle_picture.requests.get")
    def test_download_and_cache(self, get):
        get.return_value.status_code = 200
        get.return_value.headers = {"Content-Type": "image/png"}
        get.return_value.raw.read.return_value = b"png"
        pictures = VehiclePicture(self.psacc, self.dir)
        path = pictures.fetch("VIN1")
        self.assertEqual(os.path.join(self.dir, "vehicle_picture_VIN1.png"), path)
        self.assertEqual(path, pictures.get_cached("VIN1"))
        pictures.fetch("VIN1")
        get.assert_called_once()

    @patch("psa_car_controller.psacc.application.vehicle_picture.requests.get")
    def test_not_an_image(self, get):
        get.return_value.status_code = 200
        get.return_value.headers = {"Content-Type": "text/html"}
        pictures = VehiclePicture(self.psacc, self.dir)
        self.assertIsNone(pictures.fetch("VIN1"))
        self.assertIsNone(pictures.get_cached("VIN1"))


if __name__ == "__main__":
    unittest.main()


class TestLog(unittest.TestCase):
    def test_parse_and_filter(self):
        from psa_car_controller.web.view import log  # pylint: disable=import-outside-toplevel
        entries = log.parse("2026-10-29 17:08:23,614 :: INFO :: database initialized !\n"
                            "2026-10-29 17:08:24,001 :: ERROR :: Failed to record SOH\n"
                            "Traceback (most recent call last):\n"
                            "  File \"x.py\", line 1\n"
                            "2026-10-29 17:08:25,000 :: DEBUG :: Executing callbacks\n")
        self.assertEqual(["INFO", "ERROR", "DEBUG"], [e["level"] for e in entries])
        self.assertIn("Traceback", entries[1]["details"])
        self.assertEqual(2, len(log.filter_level(entries, "info")))
        self.assertEqual(1, len(log.filter_level(entries, "warning")))
        self.assertEqual(3, len(log.filter_level(entries, "debug")))

    def test_read_tail_drops_cut_line(self):
        from psa_car_controller.web.view import log  # pylint: disable=import-outside-toplevel
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".log") as f:
            f.write("first line that gets cut\nsecond\nthird\n")
        try:
            self.assertEqual("second\nthird\n", log.read_tail(f.name, size=16))
        finally:
            os.remove(f.name)


class TestLegacyPagePath(unittest.TestCase):
    def test_paths(self):
        from psa_car_controller.web.app import legacy_page_path  # pylint: disable=import-outside-toplevel
        self.assertEqual("/v1/stats", legacy_page_path("/v1/stats"))
        self.assertEqual("/v1/config/", legacy_page_path("/v1/config/"))
        self.assertEqual("/_dash-layout", legacy_page_path("/v1/_dash-layout"))
        self.assertEqual("/assets/pwa.js", legacy_page_path("/v1/assets/pwa.js"))
        self.assertEqual("/charge", legacy_page_path("/charge"))
