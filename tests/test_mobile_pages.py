import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from psa_car_controller.psacc.application.vehicle_picture import VehiclePicture
from psa_car_controller.psa import RemoteClient, precond_programs
from psa_car_controller.web.view import insights_data
from tests.utils import get_web_view

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
        log = get_web_view("log")
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
        log = get_web_view("log")
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


class TestSettingsRemoteState(unittest.TestCase):
    def remote_state(self, token_error, connected=True):
        settings = get_web_view("settings")
        controller = MagicMock(remote_control=True)
        controller.myp.remote_client.token_error = token_error
        controller.myp.remote_client.mqtt_client.is_connected.return_value = connected
        with patch.object(settings, "app", return_value=controller):
            return settings.remote_state()

    def test_connected(self):
        settings = get_web_view("settings")
        self.assertEqual(settings.REMOTE_OK, self.remote_state(None))

    def test_connected_socket_but_token_failed(self):
        # the mqtt socket stays open while every publish fails on the token
        settings = get_web_view("settings")
        self.assertEqual(settings.REMOTE_ERROR, self.remote_state(RemoteClient.TOKEN_REMOTE_FAILED))

    def test_rate_limited_is_paused(self):
        settings = get_web_view("settings")
        self.assertEqual(settings.REMOTE_PAUSED, self.remote_state(RemoteClient.TOKEN_RATE_LIMITED))

    def test_rejected_login_shows_up_as_the_login_problem_only(self):
        # is_good stays True when PSA rejects the login after the start: the account row said "Connected"
        # in green next to a red remote row, and the login form was nowhere
        settings = get_web_view("settings")
        controller = MagicMock(remote_control=True, is_good=True)
        controller.myp.manager.needs_login = True
        with patch.object(settings, "app", return_value=controller):
            self.assertEqual([settings.LOGIN_FAILED, None, None], settings.current_state())
        controller.myp.manager.needs_login = False
        controller.myp.remote_client.token_error = None
        with patch.object(settings, "app", return_value=controller):
            self.assertEqual(settings.LOGGED_IN, settings.account_state())

    def render_remote(self, state, error=None):
        settings = get_web_view("settings")
        return str(settings._remote(state, error))  # pylint: disable=protected-access

    def test_account_failure_offers_no_new_otp(self):
        settings = get_web_view("settings")
        shown = self.render_remote(settings.REMOTE_ERROR, RemoteClient.TOKEN_ACCOUNT_FAILED)
        self.assertIn("account token", shown)
        self.assertIn(settings.RECONNECT, shown)
        self.assertNotIn("SMS", shown)
        self.assertNotIn("finish-otp", shown)

    def test_remote_failure_offers_reconnect_before_a_new_otp(self):
        settings = get_web_view("settings")
        shown = self.render_remote(settings.REMOTE_ERROR, RemoteClient.TOKEN_REMOTE_FAILED)
        self.assertIn("If reconnecting doesn't help", shown)
        self.assertIn(settings.RECONNECT, shown)
        self.assertIn("SMS", shown)

    def test_paused_offers_nothing_to_click(self):
        settings = get_web_view("settings")
        shown = self.render_remote(settings.REMOTE_PAUSED)
        self.assertIn("Paused", shown)
        self.assertNotIn(settings.RECONNECT, shown)
        self.assertNotIn("SMS", shown)

    def test_refused_by_the_broker_keeps_the_generic_text(self):
        # the broker refusing the credentials isn't a failed refresh: mqtt_auth_failures counts it, not token_error
        settings = get_web_view("settings")
        self.assertEqual(settings.REMOTE_ERROR, self.remote_state(None, connected=False))
        self.assertIn("Can't reach the PSA remote server", self.render_remote(settings.REMOTE_ERROR, None))
