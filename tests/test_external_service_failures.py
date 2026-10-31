import io
import json
import logging
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import requests

from psa_car_controller.common.mylogger import formatter
from psa_car_controller.common.sensitive_filter import SensitiveDataFilter
from psa_car_controller.psa.setup.app_decoder import InitialSetup
from psa_car_controller.psacc.application.abrp import Abrp, RETRY_DELAY_IN_S
from psa_car_controller.psacc.application.ecomix import Ecomix
from psa_car_controller.psacc.application.psa_client import PSAClient
from psa_car_controller.psacc.utils.utils import get_temp

TOKEN = "fake-abrp-token-1234567890"
OWM_KEY = "fake-owm-key-1234567890"
LATITUDE = 48.137123
LONGITUDE = 11.575456
EMAIL = "someone@example.org"
PASSWORD = "fake-pass-w0rd"
ABRP_REQUEST = "psa_car_controller.psacc.application.abrp.requests.request"
LEAKS = (TOKEN, OWM_KEY, Abrp.api_key, "48.137", "11.575", "fake-pass", "someone%40", "/1/tlm/send", "onecall?")


def no_network():
    """The real requests and urllib3 run, with the real text of their exceptions, and no packet leaves."""
    return patch("urllib3.util.connection.create_connection", side_effect=OSError("no route"))


def fake_car(vin="VIN1"):
    energy = SimpleNamespace(level=50, updated_at=datetime.now(timezone.utc), consumption=1,
                             charging=SimpleNamespace(status="Stopped"))
    status = SimpleNamespace(get_energy=lambda _: energy, kinetic=SimpleNamespace(speed=0),
                             battery=SimpleNamespace(current=1),
                             last_position=SimpleNamespace(
                                 geometry=SimpleNamespace(coordinates=[LONGITUDE, LATITUDE, 500])))
    return SimpleNamespace(vin=vin, status=status, get_abrp_name=lambda: "peugeot:e208")


class LogCapture(unittest.TestCase):
    def setUp(self):
        self.log = io.StringIO()
        handler = logging.StreamHandler(self.log)
        handler.setFormatter(formatter)
        handler.addFilter(SensitiveDataFilter())
        root = logging.getLogger()
        level = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        self.addCleanup(root.setLevel, level)
        self.addCleanup(root.removeHandler, handler)

    def assert_no_leak(self):
        text = self.log.getvalue()
        for leak in LEAKS:
            self.assertNotIn(leak, text)


class TestAbrpDown(LogCapture):
    def setUp(self):
        super().setUp()
        self.abrp = Abrp(TOKEN, ["VIN1", "VIN2"])

    def test_unreachable_is_reported_without_url_token_or_position(self):
        with no_network():
            self.assertFalse(self.abrp.call(fake_car(), 20))
        self.assertIn("ABRP is unavailable (ConnectionError)", self.log.getvalue())
        self.assert_no_leak()

    def test_every_error_of_requests_is_caught(self):
        for error in (requests.Timeout, requests.exceptions.SSLError, requests.exceptions.ProxyError,
                      requests.exceptions.ChunkedEncodingError, requests.TooManyRedirects):
            with self.subTest(error=error.__name__), patch(ABRP_REQUEST, side_effect=error("url?token=" + TOKEN)):
                self.assertFalse(Abrp(TOKEN, ["VIN1"]).call(fake_car()))
        self.assert_no_leak()

    def test_timeout_is_set(self):
        with patch(ABRP_REQUEST, return_value=MagicMock(text='{"status": "ok"}')) as request:
            self.assertTrue(self.abrp.call(fake_car()))
        self.assertGreater(request.call_args.kwargs["timeout"], 0)

    def test_is_not_asked_again_right_away_even_for_another_car(self):
        now = 1000.0
        with patch("psa_car_controller.psacc.application.abrp.time.monotonic", side_effect=lambda: now), \
                patch(ABRP_REQUEST, side_effect=requests.ConnectionError("down")) as request:
            self.abrp.call(fake_car())
            self.abrp.call(fake_car())
            self.abrp.call(fake_car("VIN2"))
            self.assertEqual(1, request.call_count)
            now += RETRY_DELAY_IN_S + 1
            self.abrp.call(fake_car())
            self.assertEqual(2, request.call_count)
        self.assertEqual(1, self.log.getvalue().count("ABRP is unavailable ("))

    def test_comes_back(self):
        now = 1000.0
        with patch("psa_car_controller.psacc.application.abrp.time.monotonic", side_effect=lambda: now), \
                patch(ABRP_REQUEST, side_effect=[requests.ConnectionError("down"),
                                                 MagicMock(text='{"status": "ok"}')]):
            self.assertFalse(self.abrp.call(fake_car()))
            now += RETRY_DELAY_IN_S + 1
            self.assertTrue(self.abrp.call(fake_car()))
            self.assertTrue(self.abrp._retry_at is None)  # pylint: disable=protected-access
        self.assertIn("ABRP is available again", self.log.getvalue())

    def test_bad_answer_is_not_copied_to_the_log_as_an_error(self):
        # a proxy can answer with an error page quoting the request
        page = f"<html>{self.abrp.url}?lat={LATITUDE}&token={TOKEN}</html>"
        with patch(ABRP_REQUEST, return_value=MagicMock(text=page, status_code=502)) as request:
            self.assertFalse(self.abrp.call(fake_car()))
            self.abrp.call(fake_car())
            self.assertEqual(1, request.call_count)
        self.assertIn("HTTP 502", self.log.getvalue())

    def test_data_of_the_car_that_cant_be_sent_stops_nothing(self):
        car = fake_car()
        car.status.get_energy("Electric").updated_at = None  # TypeError
        self.assertFalse(self.abrp.call(car))


class TestChainContinues(LogCapture):
    """The services are down or fail: the status, the recording and the other cars don't depend on them."""

    def setUp(self):
        super().setUp()
        self.client = PSAClient.__new__(PSAClient)
        self.client.abrp = Abrp(TOKEN, ["VIN1"])
        self.client.weather_api = OWM_KEY
        self.client.country_code = "FR"
        self.client.remote_client = MagicMock()
        self.client._record_enabled = True  # pylint: disable=protected-access

    @staticmethod
    def status():
        now = datetime.now(timezone.utc)
        energy = SimpleNamespace(level=50, updated_at=now, consumption=1, autonomy=100,
                                 charging=SimpleNamespace(status="InProgress", charging_mode="Slow",
                                                          charging_rate=3),
                                 battery=SimpleNamespace(health=SimpleNamespace(resistance=99)))
        return SimpleNamespace(
            get_energy=lambda _: energy, energy=[energy], kinetic=SimpleNamespace(speed=0, moving=False),
            timed_odometer=SimpleNamespace(mileage=1000), battery=SimpleNamespace(current=1),
            last_position=SimpleNamespace(geometry=SimpleNamespace(coordinates=[LONGITUDE, LATITUDE, 500]),
                                          properties=SimpleNamespace(updated_at=now)))

    def car(self):
        car = MagicMock(vin="VIN1", status=self.status())
        car.is_thermal.return_value = False
        car.has_battery.return_value = True
        car.get_abrp_name.return_value = "peugeot:e208"
        return car

    def test_charge_is_recorded_when_abrp_is_down(self):
        with no_network(), patch("psa_car_controller.psacc.application.psa_client.Database") as database, \
                patch("psa_car_controller.psacc.application.psa_client.Charging") as charging:
            database.get_last_temp.return_value = 20
            self.client.record_info(self.car())
        charging.record_charging.assert_called_once()
        database.record_battery_soh.assert_called_once()
        self.assert_no_leak()

    def test_failure_of_the_recording_keeps_the_status(self):
        car = self.car()
        status = car.status
        car.status = None
        self.client.record_info = MagicMock(side_effect=requests.ConnectionError("down"))
        self.client._set_status(car, status)  # pylint: disable=protected-access
        self.assertIs(status, car.status)

    def refresh(self, get_vehicle_info, callbacks=()):
        self.client.info_refresh_rate = 60
        self.client._rate_limited = False  # pylint: disable=protected-access
        self.client.vehicles_list = [SimpleNamespace(vin="VIN1"), SimpleNamespace(vin="VIN2")]
        self.client.info_callback = list(callbacks)
        self.client.get_vehicle_info = get_vehicle_info
        self.client._wait_before_next_car = lambda: True  # pylint: disable=protected-access
        self.client._refresh_vehicle_services = MagicMock()  # pylint: disable=protected-access
        with patch.object(PSAClient, "_PSAClient__schedule_refresh"):
            self.client._PSAClient__refresh_vehicle_info()  # pylint: disable=protected-access

    def test_second_car_is_refreshed_when_the_first_one_fails(self):
        info = MagicMock(side_effect=[requests.ConnectionError("down"), None])
        self.refresh(info)
        self.assertEqual(["VIN1", "VIN2"], [call.args[0] for call in info.call_args_list])
        self.client._refresh_vehicle_services.assert_called_once()  # pylint: disable=protected-access

    def test_a_failing_callback_doesnt_stop_the_next_one(self):
        second = MagicMock()
        self.refresh(MagicMock(), [MagicMock(side_effect=requests.ConnectionError("down")), second])
        second.assert_called_once()


class TestOtherServices(LogCapture):
    def setUp(self):
        super().setUp()
        Ecomix._failed_at.clear()  # pylint: disable=protected-access
        Ecomix._cache.clear()  # pylint: disable=protected-access
        self.addCleanup(setattr, Ecomix, "co2_signal_key", Ecomix.co2_signal_key)
        Ecomix.co2_signal_key = TOKEN

    def test_weather_unreachable(self):
        with no_network():
            self.assertIsNone(get_temp(str(LATITUDE), str(LONGITUDE), OWM_KEY))
        self.assertIn("Can't connect to openweathermap (ConnectionError)", self.log.getvalue())
        self.assert_no_leak()

    def test_weather_answer_that_isnt_json(self):
        answer = MagicMock()
        answer.json.side_effect = ValueError("no json")
        with patch("psa_car_controller.psacc.utils.utils.requests.get", return_value=answer):
            self.assertIsNone(get_temp(str(LATITUDE), str(LONGITUDE), OWM_KEY))

    def test_co2_signal_unreachable_isnt_asked_again_right_away(self):
        with patch("psa_car_controller.psacc.application.ecomix.requests.get",
                   side_effect=requests.ConnectionError("down")) as get:
            self.assertFalse(Ecomix.get_data_from_co2_signal(LATITUDE, LONGITUDE, "DE"))
            self.assertFalse(Ecomix.get_data_from_co2_signal(LATITUDE, LONGITUDE, "DE"))
        self.assertEqual(1, get.call_count)
        self.assert_no_leak()

    def test_co2_signal_answer_that_isnt_json(self):
        answer = MagicMock(status_code=200)
        answer.json.side_effect = ValueError("no json")
        with patch("psa_car_controller.psacc.application.ecomix.requests.get", return_value=answer):
            self.assertFalse(Ecomix.get_data_from_co2_signal(LATITUDE, LONGITUDE, "DE"))

    def test_france_answer_that_isnt_xml(self):
        now = datetime.now(timezone.utc)
        with patch("psa_car_controller.psacc.application.ecomix.requests.get",
                   return_value=MagicMock(text="<html>Bad gateway", status_code=502)):
            self.assertIsNone(Ecomix.get_data_france(now - timedelta(hours=1), now))

    def test_position_isnt_logged_when_the_country_is_unknown(self):
        with patch("psa_car_controller.psacc.application.ecomix.reverse_geocode.search", side_effect=IndexError):
            self.assertEqual("FR", Ecomix.get_country(LATITUDE, LONGITUDE, "FR"))
        self.assert_no_leak()

    def test_brand_login_unreachable_doesnt_log_the_password(self):
        apk = SimpleNamespace(host_brandid_prod="https://brand.example.org", site_code="AP_FR_ESP")
        fetch = getattr(InitialSetup, "_InitialSetup__fetch_brand_token")
        with no_network(), self.assertRaises(ConnectionError) as error:
            fetch(apk, EMAIL, PASSWORD)
        self.assertIsNone(error.exception.__cause__)
        self.assertIn("did not answer correctly", str(error.exception))
        self.assert_no_leak()


class TestFilterMasksQueries(unittest.TestCase):
    """For what gets into a traceback anyway, e.g. an error raised in the requests themselves."""

    def test_abrp_url(self):
        tlm = json.dumps({"lat": LATITUDE, "lon": LONGITUDE})
        prepared = requests.Request("POST", Abrp.url, params={"tlm": tlm, "token": TOKEN,
                                                               "api_key": Abrp.api_key}).prepare()
        text = SensitiveDataFilter().mask(f"Max retries exceeded with url: {prepared.url} (Caused by X('y'))")
        for leak in (TOKEN, Abrp.api_key, "48.137", "11.575"):
            self.assertNotIn(leak, text)
        self.assertIn("(Caused by X('y'))", text)

    def test_weather_url(self):
        prepared = requests.Request("GET", "https://api.example.org/onecall",
                                    params={"lat": LATITUDE, "lon": LONGITUDE, "units": "metric",
                                            "appid": OWM_KEY}).prepare()
        text = SensitiveDataFilter().mask(prepared.url)
        for leak in (OWM_KEY, "48.137", "11.575"):
            self.assertNotIn(leak, text)
        self.assertIn("units=metric", text)

    def test_login_and_otp_urls(self):
        for params in ({"jsonRequest": json.dumps({"USR_EMAIL": EMAIL, "USR_PASSWORD": PASSWORD})},
                       {"sid": "fake-session-id", "code": "fake-sms-code", "macid": "fake-mac-id"}):
            prepared = requests.Request("GET", "https://api.example.org/x", params=params).prepare()
            text = SensitiveDataFilter().mask(prepared.url)
            for leak in ("fake", EMAIL.split("@", maxsplit=1)[0]):
                self.assertNotIn(leak, text)

    def test_keys_in_other_forms(self):
        text = SensitiveDataFilter().mask(f"""{{'api_key': '{OWM_KEY}', "appid": "{OWM_KEY}"}}""")
        self.assertNotIn(OWM_KEY, text)

    def test_plain_text_stays(self):
        for text in ("latitude 48.1", "lat=48.1", "error code=5", "?page=2&units=metric"):
            self.assertEqual(text, SensitiveDataFilter().mask(text))


if __name__ == "__main__":
    unittest.main()
