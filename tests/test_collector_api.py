"""What a collector polling the REST API relies on: the routes, their answers on errors, and that a cached read
or a read during a PSA rate limit never calls PSA.

Before, an unknown VIN or from_cache=abc answered HTTP 500, and a direct /get_vehicleinfo asked PSA again while the
refresh loop waited after a 429.
"""
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from psa_car_controller.common.utils import AuthenticationRequiredException
from psa_car_controller.psa.RemoteClient import RemoteException
from psa_car_controller.psacc.application import psa_client as psa_client_module
from psa_car_controller.psacc.application.health import get_data_supply
from psa_car_controller.psacc.application.psa_client import MAX_REFRESH_DELAY, PSAClient
from psa_car_controller.psacc.model.car import Car, Cars, EMPTY_STATUS_LIMIT
from tests.test_status_refresh import api_error, get_status
from tests.utils import get_web_view

VIN = "VR3UHZKXZLT000000"
OTHER_VIN = "VR3UHZKXZLT999999"
NOW = "2026-11-06T10:00:00Z"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now


def get_client(*responses) -> PSAClient:
    """A client whose PSA answers with responses in turn: an exception is raised, anything else is the status."""
    client = PSAClient.__new__(PSAClient)  # bypass __init__, it does I/O
    client.vehicles_list = Cars([Car(VIN, "vehicle-id", "Peugeot")])
    client.remote_client = MagicMock()
    client._record_enabled = False
    vehicles_api = MagicMock()
    vehicles_api.get_vehicle_status.side_effect = list(responses)
    client.api = MagicMock(return_value=vehicles_api)
    return client


def psa_calls(client: PSAClient) -> int:
    return client.api.return_value.get_vehicle_status.call_count


class ApiTestCase(unittest.TestCase):

    def setUp(self):
        self.api_view = get_web_view("api")
        self.clock = Clock()
        clock = patch.object(psa_client_module, "time", self.clock)
        clock.start()
        self.addCleanup(clock.stop)

    def get(self, client, path, **app):
        with patch.object(self.api_view, "APP", MagicMock(myp=client, **app)):
            response = self.api_view.app.test_client().get(path)
        return response, response.get_json()


class TestVehicleInfo(ApiTestCase):

    def test_status_from_psa(self):
        client = get_client(get_status(NOW, level=70))
        response, body = self.get(client, f"/get_vehicleinfo/{VIN}")
        self.assertEqual(200, response.status_code)
        self.assertEqual(70, [e for e in body["energy"] if e["type"] == "Electric"][0]["level"])
        self.assertIn("data_age_seconds", body)
        self.assertNotIn("X-Status-Outdated", response.headers)
        self.assertEqual(1, psa_calls(client))

    def test_cached_status_doesnt_call_psa(self):
        client = get_client(get_status(NOW))
        self.get(client, f"/get_vehicleinfo/{VIN}")
        response, body = self.get(client, f"/get_vehicleinfo/{VIN}?from_cache=1")
        self.assertEqual(200, response.status_code)
        self.assertIn("data_age_seconds", body)
        self.assertEqual(1, psa_calls(client))

    def test_empty_cache_answers_no_data_without_psa(self):
        client = get_client()
        client.api.side_effect = AssertionError("PSA must not be asked")
        response, body = self.get(client, f"/get_vehicleinfo/{VIN}?from_cache=1")
        self.assertEqual(502, response.status_code)
        self.assertEqual("no_data", body["error"])
        self.assertIn("cached", body["message"])
        self.assertNotIn("API", body["message"])

    def test_cached_read_needs_no_login(self):
        client = get_client(get_status(NOW))
        self.get(client, f"/get_vehicleinfo/{VIN}")
        client.api.side_effect = AuthenticationRequiredException()
        response, _ = self.get(client, f"/get_vehicleinfo/{VIN}?from_cache=1")
        self.assertEqual(200, response.status_code)

    def test_old_cached_status_is_returned_with_its_age(self):
        client = get_client(get_status("2026-01-01T00:00:00Z"))
        self.get(client, f"/get_vehicleinfo/{VIN}")
        _, body = self.get(client, f"/get_vehicleinfo/{VIN}?from_cache=1")
        self.assertGreater(body["data_age_seconds"], 3600)

    def test_outdated_status_has_the_header(self):
        client = get_client(get_status(NOW), *[api_error(500)] * (2 * EMPTY_STATUS_LIMIT))
        self.get(client, f"/get_vehicleinfo/{VIN}")
        for _ in range(EMPTY_STATUS_LIMIT):
            response, _ = self.get(client, f"/get_vehicleinfo/{VIN}")
        self.assertEqual(200, response.status_code)
        self.assertEqual(str(EMPTY_STATUS_LIMIT), response.headers["X-Status-Outdated"])

    def test_psa_failure_without_status(self):
        client = get_client(api_error(500), api_error(500))
        response, body = self.get(client, f"/get_vehicleinfo/{VIN}")
        self.assertEqual(502, response.status_code)
        self.assertEqual("no_data", body["error"])

    def test_not_logged_in(self):
        client = get_client()
        client.api.side_effect = AuthenticationRequiredException()
        response, body = self.get(client, f"/get_vehicleinfo/{VIN}")
        self.assertEqual(503, response.status_code)
        self.assertEqual("authentication_required", body["error"])

    def test_unknown_vin(self):
        client = get_client()
        client.api.side_effect = AssertionError("PSA must not be asked")
        for query in ("", "?from_cache=1"):
            with self.subTest(query):
                response, body = self.get(client, f"/get_vehicleinfo/{OTHER_VIN}{query}")
                self.assertEqual(404, response.status_code)
                self.assertEqual("unknown_vin", body["error"])

    def test_invalid_from_cache(self):
        client = get_client()
        client.api.side_effect = AssertionError("PSA must not be asked")
        for value in ("abc", "", "1.0"):
            with self.subTest(value):
                response, body = self.get(client, f"/get_vehicleinfo/{VIN}?from_cache={value}")
                self.assertEqual(400, response.status_code)
                self.assertEqual("invalid_argument", body["error"])

    def test_other_numbers_ask_psa(self):
        client = get_client(get_status(NOW), get_status(NOW))
        for value in ("0", "2"):
            self.get(client, f"/get_vehicleinfo/{VIN}?from_cache={value}")
        self.assertEqual(2, psa_calls(client))


class TestRateLimit(ApiTestCase):

    def rate_limited_client(self):
        client = get_client(get_status(NOW, level=60), api_error(429), get_status(NOW, level=65))
        self.get(client, f"/get_vehicleinfo/{VIN}")
        self.get(client, f"/get_vehicleinfo/{VIN}")
        self.assertEqual(2, psa_calls(client))
        return client

    def test_requests_during_the_limit_get_the_stored_status(self):
        client = self.rate_limited_client()
        for _ in range(5):
            response, body = self.get(client, f"/get_vehicleinfo/{VIN}")
            self.assertEqual(200, response.status_code)
            self.assertEqual(60, [e for e in body["energy"] if e["type"] == "Electric"][0]["level"])
        self.assertEqual(2, psa_calls(client))

    def test_waiting_doesnt_count_as_failure(self):
        client = self.rate_limited_client()
        failures = client.vehicles_list[0].status_failures
        for _ in range(EMPTY_STATUS_LIMIT + 1):
            response, _ = self.get(client, f"/get_vehicleinfo/{VIN}")
        self.assertEqual(failures, client.vehicles_list[0].status_failures)
        self.assertNotIn("X-Status-Outdated", response.headers)

    def test_psa_is_asked_again_after_the_wait(self):
        client = self.rate_limited_client()
        self.clock.now += client.rest_retry_in() + 1
        _, body = self.get(client, f"/get_vehicleinfo/{VIN}")
        self.assertEqual(3, psa_calls(client))
        self.assertEqual(65, [e for e in body["energy"] if e["type"] == "Electric"][0]["level"])
        self.assertEqual(0, client.rest_retry_in())

    def test_wait_doubles_and_is_bounded(self):
        client = get_client(*[api_error(429)] * 10)
        client.info_refresh_rate = 300
        waits = []
        for _ in range(4):
            client.get_vehicle_info(VIN)
            waits.append(client.rest_retry_in())
            self.clock.now += waits[-1] + 1
        self.assertEqual([600, 1200, 2400, MAX_REFRESH_DELAY], waits)

    def test_success_resets_the_wait(self):
        client = get_client(api_error(429), get_status(NOW), api_error(429))
        client.info_refresh_rate = 300
        client.get_vehicle_info(VIN)
        self.clock.now += client.rest_retry_in() + 1
        client.get_vehicle_info(VIN)
        client.get_vehicle_info(VIN)
        self.assertEqual(600, client.rest_retry_in())

    def test_retry_after_is_used(self):
        error = api_error(429)
        error.headers = {"Retry-After": "120"}
        client = get_client(error)
        client.get_vehicle_info(VIN)
        self.assertEqual(120, client.rest_retry_in())

    def test_invalid_retry_after_is_ignored(self):
        for value in ("soon", "-5", "Wed, 21 Oct 2026 07:28:00 GMT"):
            with self.subTest(value):
                error = api_error(429)
                error.headers = {"Retry-After": value}
                client = get_client(error)
                client.info_refresh_rate = 300
                client.get_vehicle_info(VIN)
                self.assertEqual(600, client.rest_retry_in())

    def test_refresh_loop_still_asks(self):
        client = self.rate_limited_client()
        client.get_vehicle_info(VIN, scheduled=True)
        self.assertEqual(3, psa_calls(client))

    def test_cached_read_is_unaffected(self):
        client = self.rate_limited_client()
        response, _ = self.get(client, f"/get_vehicleinfo/{VIN}?from_cache=1")
        self.assertEqual(200, response.status_code)
        self.assertEqual(2, psa_calls(client))

    def test_stellantis_trips_wait_too(self):
        client = self.rate_limited_client()
        client._rate_limited = False
        client.vehicle_services = MagicMock()
        response, body = self.get(client, f"/vehicles/{VIN}/stellantis_trips")
        self.assertEqual(429, response.status_code)
        self.assertEqual("rate_limited", body["error"])
        client.vehicle_services.trips.assert_not_called()


class TestHealthDataSupply(ApiTestCase):

    def supply(self, client, max_status_age=900):
        return get_data_supply(client, max_status_age)

    def test_nothing_asked_yet(self):
        supply = self.supply(get_client())
        self.assertEqual("ok", supply["data_supply"])
        vehicle = supply["vehicles"][0]
        self.assertIsNone(vehicle["last_status_attempt"])
        self.assertIsNone(vehicle["last_status_success"])
        self.assertIsNone(vehicle["data_age_seconds"])
        self.assertNotIn(VIN, json.dumps(supply))

    def test_old_data_fetched_now(self):
        # PSA keeps sending the same old status: fetched a moment ago, data from long ago
        client = get_client(get_status("2026-01-01T00:00:00Z"))
        client.get_vehicle_info(VIN)
        vehicle = self.supply(client)["vehicles"][0]
        self.assertLess(vehicle["status_success_age_seconds"], 60)
        self.assertGreater(vehicle["data_age_seconds"], 3600)
        self.assertIsNone(vehicle["last_error"])

    def test_failing_requests(self):
        client = get_client(get_status(NOW), *[api_error(500)] * (2 * EMPTY_STATUS_LIMIT))
        for _ in range(EMPTY_STATUS_LIMIT + 1):
            client.get_vehicle_info(VIN)
        supply = self.supply(client)
        self.assertEqual("degraded", supply["data_supply"])
        self.assertIn("status_requests_failing", supply["data_supply_problems"])
        self.assertEqual("http_500", supply["vehicles"][0]["last_error"])
        self.assertEqual(EMPTY_STATUS_LIMIT, supply["vehicles"][0]["consecutive_failures"])

    def test_recovery(self):
        client = get_client(*[api_error(500)] * (2 * EMPTY_STATUS_LIMIT), get_status(NOW))
        for _ in range(EMPTY_STATUS_LIMIT + 1):
            client.get_vehicle_info(VIN)
        supply = self.supply(client)
        self.assertEqual("ok", supply["data_supply"])
        self.assertIsNone(supply["vehicles"][0]["last_error"])

    def test_rate_limit(self):
        client = get_client(api_error(429))
        client.info_refresh_rate = 300
        client.get_vehicle_info(VIN)
        supply = self.supply(client)
        self.assertEqual("degraded", supply["data_supply"])
        self.assertTrue(supply["rate_limit"]["active"])
        self.assertEqual(600, supply["rate_limit"]["retry_in_seconds"])
        self.assertEqual("http_429", supply["vehicles"][0]["last_error"])

    def test_no_recent_status_only_while_the_refresh_loop_runs(self):
        client = get_client(get_status(NOW))
        with patch.object(psa_client_module, "datetime") as fake_datetime:
            fake_datetime.now.return_value = datetime.now(timezone.utc) - timedelta(hours=1)
            client.get_vehicle_info(VIN)
        self.assertEqual("ok", self.supply(client)["data_supply"])
        client.info_refresh_rate = 300
        client._next_refresh_at = self.clock.now + 60
        supply = self.supply(client)
        self.assertEqual(["no_recent_status"], supply["data_supply_problems"])
        self.assertEqual(60, supply["rate_limit"]["next_refresh_in_seconds"])

    def test_health_route(self):
        client = get_client(get_status(NOW))
        client.get_vehicle_info(VIN)
        client.is_authenticated = MagicMock(return_value=True)
        client.auth_problem = MagicMock(return_value=None)
        response, body = self.get(client, "/health", remote_control=False)
        self.assertEqual(200, response.status_code)
        self.assertTrue(body["api_authenticated"])
        self.assertEqual("ok", body["data_supply"])
        self.assertEqual(1, len(body["vehicles"]))

    def test_degraded_is_still_healthy(self):
        client = get_client(api_error(429))
        client.get_vehicle_info(VIN)
        client.is_authenticated = MagicMock(return_value=True)
        client.auth_problem = MagicMock(return_value=None)
        response, body = self.get(client, "/health", remote_control=False)
        self.assertEqual(200, response.status_code)
        self.assertEqual("degraded", body["data_supply"])

    def test_cached_read_doesnt_count_as_request(self):
        client = get_client(get_status(NOW))
        client.get_vehicle_info(VIN)
        before = self.supply(client)["vehicles"][0]["last_status_success"]
        client.get_vehicle_info(VIN, cache=True)
        self.assertEqual(before, self.supply(client)["vehicles"][0]["last_status_success"])


class TestOtherCollectorRoutes(ApiTestCase):

    def test_trips_without_cars(self):
        response, body = self.get(MagicMock(vehicles_list=Cars()), "/vehicles/trips")
        self.assertEqual(200, response.status_code)
        # an object here, a list on errors: kept as it was, clients may rely on it
        self.assertEqual({}, body)

    def test_trips_without_enough_data(self):
        with patch.object(self.api_view.Trips, "get_trips", side_effect=KeyError(VIN)):
            response, body = self.get(MagicMock(vehicles_list=Cars([Car(VIN, "id", "Peugeot")])), "/vehicles/trips")
        self.assertEqual(200, response.status_code)
        self.assertEqual([], body)

    def test_trips_of_the_first_car(self):
        trips = MagicMock()
        trips.get_trips_as_dict.return_value = [{"id": 1, "distance": 12.5}]
        with patch.object(self.api_view.Trips, "get_trips", return_value={VIN: trips}):
            _, body = self.get(MagicMock(vehicles_list=Cars([Car(VIN, "id", "Peugeot")])), "/vehicles/trips")
        self.assertEqual([{"id": 1, "distance": 12.5}], body)

    def test_soh(self):
        for soh in (97.5, None):
            with self.subTest(soh), patch.object(self.api_view.Database, "get_last_soh_by_vin", return_value=soh):
                response, body = self.get(MagicMock(), f"/battery/soh/{VIN}")
                self.assertEqual(200, response.status_code)
                self.assertEqual({"soh": soh}, body)

    def test_preconditioning(self):
        client = MagicMock()
        client.remote_client.preconditioning.return_value = True
        client.remote_client.track_commands.return_value.__enter__.return_value = []
        response, body = self.get(client, f"/preconditioning/{VIN}/1")
        self.assertEqual(200, response.status_code)
        self.assertTrue(body)
        client.remote_client.preconditioning.assert_called_once_with(VIN, 1)

    def test_preconditioning_refused(self):
        client = MagicMock()
        client.remote_client.preconditioning.side_effect = RemoteException("programs unknown")
        client.remote_client.track_commands.return_value.__enter__.return_value = []
        response, body = self.get(client, f"/preconditioning/{VIN}/0")
        self.assertEqual(409, response.status_code)
        self.assertEqual({"error": "programs unknown"}, body)


if __name__ == "__main__":
    unittest.main()
