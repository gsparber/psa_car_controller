"""Maintenance, subscription rights, Stellantis trips and private mode (T38).

The answers are mocked after the format the Home Assistant integration reads, no real account was at hand.
"""
import re
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from psa_car_controller.common.utils import RateLimitException
from psa_car_controller.psa.connected_car_api.rest import ApiException
from psa_car_controller.psa.extra_api import API_ROOT, ExtraApi, page_token
from psa_car_controller.psacc.application import psa_client as psa_client_module
from psa_car_controller.psacc.application.psa_client import PSAClient
from psa_car_controller.psacc.application.vehicle_services import REFRESH_PERIOD, RETRY_DELAY, VehicleServices, \
    parse_rights, supported_commands
from psa_car_controller.psacc.model.car import Car, Cars
from psa_car_controller.web.view import status_text, ui
from tests.test_units import service_text
from tests.test_status_refresh import get_client, get_status, VIN
from tests.utils import get_web_view

DATE = "2022-03-26T11:02:54Z"
MAINTENANCE_HREF = API_ROOT + "/connectedcar/v4/user/vehicles/vehicle-id/maintenance?locale=fr-FR"
MAINTENANCE = {"mileageBeforeMaintenance": 12000, "daysBeforeMaintenance": 150, "updatedAt": DATE}
RIGHTS = {"services": {
    "BSRF": [{"code": "RCC", "name": "Remote charge", "mqtt_services": ["VehCharge"]},
             {"code": "RTP", "name": "Remote climate", "mqtt_services": ["/ThermalPrecond"]}],
    "VSM": [{"code": "RCC", "name": "Remote charge", "mqtt_services": ["VehCharge"]}],
}}


def api_error(status):
    return ApiException(status=status, reason="error")


class FakeClock:
    now = 1000.0

    def __call__(self):
        return self.now


def trips_page(ids, next_token=None):
    page = {"_embedded": {"trips": [{"id": trip_id} for trip_id in ids]}, "_links": {}}
    if next_token:
        page["_links"]["next"] = {"href": API_ROOT + f"/connectedcar/v4/user/vehicles/x/trips?pageToken={next_token}"}
    return page


class TestExtraApi(unittest.TestCase):

    def setUp(self):
        self.client = MagicMock()
        self.api = ExtraApi(self.client)

    def test_link_is_requested_with_its_query_and_the_credentials(self):
        self.client.call_api.return_value = MAINTENANCE
        self.assertEqual(MAINTENANCE, self.api.get_maintenance(MAINTENANCE_HREF))
        args, kwargs = self.client.call_api.call_args
        self.assertEqual(("/connectedcar/v4/user/vehicles/vehicle-id/maintenance", "GET"), args)
        self.assertEqual([("locale", "fr-FR")], kwargs["query_params"])
        self.assertEqual(["Vehicle_auth", "client_id", "realm"], kwargs["auth_settings"])

    def test_the_token_is_never_sent_to_another_host(self):
        with self.assertRaises(ValueError):
            self.api.get_maintenance("https://example.com/maintenance")
        self.client.call_api.assert_not_called()

    def test_rights_ask_for_plain_json(self):
        self.client.call_api.return_value = RIGHTS
        self.api.get_rights(VIN)
        args, kwargs = self.client.call_api.call_args
        self.assertEqual(f"/applications/cvs/v4/rights/vehicle/{VIN}", args[0])
        self.assertEqual("application/json", kwargs["header_params"]["Accept"])

    def test_not_a_json_object(self):
        self.client.call_api.return_value = "oops"
        self.assertEqual({}, self.api.get_rights(VIN))

    def test_trips_follow_the_page_tokens(self):
        self.client.call_api.side_effect = [trips_page([1, 2], "abc"), trips_page([3])]
        since = datetime(2026, 10, 1, tzinfo=timezone.utc)
        trips, more = self.api.get_trips("vehicle-id", since)
        self.assertEqual([1, 2, 3], [trip["id"] for trip in trips])
        self.assertFalse(more)
        first, second = [call.kwargs["query_params"] for call in self.client.call_api.call_args_list]
        self.assertEqual([("distance", "0.1-"), ("timestamps", "2026-10-01T00:00:00+00:00/")], first)
        self.assertEqual(("pageToken", "abc"), second[-1])
        self.assertEqual("/connectedcar/v4/user/vehicles/vehicle-id/trips",
                         self.client.call_api.call_args.args[0])

    def test_trips_stop_at_max_pages_or_a_repeated_token(self):
        self.client.call_api.side_effect = [trips_page([1], "a"), trips_page([2], "b")]
        self.assertEqual(([{"id": 1}, {"id": 2}], True), self.api.get_trips("vehicle-id", max_pages=2))
        self.client.call_api.side_effect = [trips_page([1], "a"), trips_page([2], "a")]
        self.assertEqual(([{"id": 1}, {"id": 2}], False), self.api.get_trips("vehicle-id", max_pages=5))

    def test_page_token(self):
        self.assertEqual("x y", page_token("https://h/p?distance=0.1-&pageToken=x%20y"))
        self.assertIsNone(page_token(None))
        self.assertIsNone(page_token("https://h/p?distance=0.1-"))


class TestRights(unittest.TestCase):

    def test_services_of_every_unit_once(self):
        features = parse_rights(RIGHTS)
        self.assertEqual(["RCC", "RTP"], sorted(features))
        self.assertEqual(["VehCharge"], features["RCC"]["mqtt_services"])

    def test_malformed_answers(self):
        self.assertEqual({}, parse_rights(None))
        self.assertEqual({}, parse_rights({"services": []}))
        self.assertEqual({"A": {"name": None, "mqtt_services": []}},
                         parse_rights({"services": {"u": [None, "x", {"name": "no code"}, {"code": "A",
                                                                                         "mqtt_services": "x"}]}}))

    def test_commands_covered(self):
        commands = supported_commands(parse_rights(RIGHTS))
        self.assertTrue(commands["/VehCharge"])
        self.assertTrue(commands["/VehCharge/state"])  # the wakeup is a charge state request
        self.assertTrue(commands["/ThermalPrecond"])
        self.assertFalse(commands["/Horn"])
        self.assertFalse(commands["/Doors"])

    def test_unknown_format_hides_nothing(self):
        self.assertIsNone(supported_commands(parse_rights(
            {"services": {"u": [{"code": "X", "mqtt_services": ["remote:door"]}]}})))
        self.assertIsNone(supported_commands({}))


class TestVehicleServices(unittest.TestCase):

    def setUp(self):
        self.extra_api = MagicMock()
        self.extra_api.get_maintenance.return_value = MAINTENANCE
        self.extra_api.get_rights.return_value = RIGHTS
        self.clock = FakeClock()
        self.services = VehicleServices(lambda: self.extra_api, self.clock)
        self.car = Car(VIN, "vehicle-id", "Peugeot")
        self.services.set_maintenance_href(VIN, MAINTENANCE_HREF)

    def test_asked_for_once_a_day(self):
        self.assertTrue(self.services.refresh(self.car))
        self.assertEqual({"mileage": 12000, "days": 150, "updated_at": DATE}, self.services.maintenance(VIN))
        self.clock.now += REFRESH_PERIOD - 1
        self.services.refresh(self.car)
        self.assertEqual(1, self.extra_api.get_maintenance.call_count)
        self.assertEqual(1, self.extra_api.get_rights.call_count)
        self.clock.now += 1
        self.services.refresh(self.car)
        self.assertEqual(2, self.extra_api.get_maintenance.call_count)
        self.assertEqual(2, self.extra_api.get_rights.call_count)

    def test_empty_maintenance_is_not_asked_for_again(self):
        self.extra_api.get_maintenance.return_value = {}
        self.services.refresh(self.car)
        self.clock.now += 10 * REFRESH_PERIOD
        self.services.refresh(self.car)
        self.assertEqual(1, self.extra_api.get_maintenance.call_count)
        self.assertIsNone(self.services.maintenance(VIN))

    def test_no_maintenance_link_no_request(self):
        services = VehicleServices(lambda: self.extra_api, self.clock)
        services.set_maintenance_href(VIN, None)
        services.refresh(self.car)
        self.extra_api.get_maintenance.assert_not_called()

    def test_missing_endpoint_turns_the_feature_off(self):
        self.extra_api.get_rights.side_effect = api_error(404)
        self.services.refresh(self.car)
        self.clock.now += 10 * REFRESH_PERIOD
        self.services.refresh(self.car)
        self.assertEqual(1, self.extra_api.get_rights.call_count)
        self.assertTrue(self.services.is_command_supported(VIN, "/Horn"))

    def test_server_error_is_retried_later(self):
        self.extra_api.get_maintenance.side_effect = [api_error(500), MAINTENANCE]
        self.services.refresh(self.car)
        self.services.refresh(self.car)
        self.assertEqual(1, self.extra_api.get_maintenance.call_count)
        self.clock.now += RETRY_DELAY
        self.services.refresh(self.car)
        self.assertEqual(150, self.services.maintenance(VIN)["days"])

    def test_rate_limit_stops_asking(self):
        self.extra_api.get_maintenance.side_effect = api_error(429)
        self.assertFalse(self.services.refresh(self.car))
        self.extra_api.get_rights.assert_not_called()
        self.extra_api.get_maintenance.side_effect = RateLimitException("token")
        self.clock.now += RETRY_DELAY
        self.assertFalse(self.services.refresh(self.car))

    def test_network_error_is_swallowed(self):
        self.extra_api.get_maintenance.side_effect = ValueError("unexpected API host")
        self.assertTrue(self.services.refresh(self.car))
        self.assertEqual(1, self.extra_api.get_rights.call_count)

    def test_commands_hidden_only_when_the_rights_are_known(self):
        self.assertTrue(self.services.is_command_supported(VIN, "/Horn"))
        self.services.refresh(self.car)
        self.assertFalse(self.services.is_command_supported(VIN, "/Horn"))
        self.assertTrue(self.services.is_command_supported(VIN, "/VehCharge"))
        self.assertTrue(self.services.is_command_supported(VIN, "/Unknown"))
        self.assertEqual(["RCC", "RTP"], sorted(self.services.to_dict(VIN)["rights"]))

    def test_trips_are_cached(self):
        self.extra_api.get_trips.return_value = ([{"id": 1}], False)
        self.assertEqual({"trips": [{"id": 1}], "more": False}, self.services.trips("vehicle-id"))
        self.services.trips("vehicle-id")
        self.assertEqual(1, self.extra_api.get_trips.call_count)
        self.clock.now += 600
        self.services.trips("vehicle-id")
        self.assertEqual(2, self.extra_api.get_trips.call_count)


def services_client(*statuses):
    client = get_client(*statuses)
    client.refresh_thread = None
    client.info_callback = []
    client.extra_api = MagicMock()
    client.extra_api.get_maintenance.return_value = MAINTENANCE
    client.extra_api.get_rights.return_value = RIGHTS
    client.vehicle_services = VehicleServices(lambda: client.extra_api)
    client.vehicle_services.set_maintenance_href(VIN, MAINTENANCE_HREF)
    return client


@patch.object(psa_client_module.threading, "Timer", MagicMock())
class TestPSAClientServices(unittest.TestCase):

    def test_status_refresh_asks_once(self):
        client = services_client(get_status(DATE), get_status(DATE))
        client._PSAClient__refresh_vehicle_info()
        client._PSAClient__refresh_vehicle_info()
        self.assertEqual(1, client.extra_api.get_maintenance.call_count)
        self.assertEqual(1, client.extra_api.get_rights.call_count)
        self.assertEqual(2, client.api().get_vehicle_status.call_count)

    def test_nothing_asked_while_rate_limited(self):
        client = services_client(api_error(429))
        client._PSAClient__refresh_vehicle_info()
        client.extra_api.get_maintenance.assert_not_called()

    def test_rate_limit_slows_the_status_refresh(self):
        client = services_client(get_status(DATE))
        client.extra_api.get_maintenance.side_effect = api_error(429)
        client._PSAClient__refresh_vehicle_info()
        self.assertEqual(1, client._rate_limit_backoff)

    def test_failure_never_breaks_the_refresh(self):
        client = services_client(get_status(DATE))
        client.vehicle_services.refresh = MagicMock(side_effect=KeyError("boom"))
        client._PSAClient__refresh_vehicle_info()
        self.assertIsNotNone(client.vehicles_list[0].status)
        self.assertIsNotNone(client.refresh_thread)

    def test_client_without_services(self):
        client = get_client(get_status(DATE))
        client.refresh_thread = None
        client.info_callback = []
        client._PSAClient__refresh_vehicle_info()

    def test_maintenance_link_from_the_vehicle_list(self):
        client = services_client()
        client.vehicles_list = Cars()
        client.vehicles_list.save_cars = MagicMock()
        client.vehicle_services = VehicleServices(lambda: client.extra_api)
        vehicle = MagicMock(vin=VIN, id="vehicle-id", brand="Peugeot", label=None)
        vehicle.links.maintenance.href = MAINTENANCE_HREF
        client.api().get_vehicles_by_device.return_value.embedded.vehicles = [vehicle]
        client.get_vehicles()
        client.vehicle_services.refresh(client.vehicles_list[0])
        client.extra_api.get_maintenance.assert_called_once_with(MAINTENANCE_HREF)

    def test_trips_rate_limit(self):
        client = services_client()
        client.extra_api.get_trips.side_effect = api_error(429)
        car = client.vehicles_list[0]
        with self.assertRaises(RateLimitException):
            client.get_stellantis_trips(car)
        self.assertTrue(client._is_rate_limited())
        with self.assertRaises(RateLimitException):  # not even asked for while limited
            client.get_stellantis_trips(car)
        self.assertEqual(1, client.extra_api.get_trips.call_count)

    def test_root_client_shares_the_credentials(self):
        client = PSAClient.__new__(PSAClient)
        client.api_config = psa_client_module.Oauth2PSACCApiConfig()
        client.manager = MagicMock(access_token="token-1", needs_login=False)
        client.manager.rejected_grant_retry_due.return_value = False
        client.api_config.api_key["client_id"] = "id"
        root = client.root_api_client()
        self.assertEqual(API_ROOT, root.configuration.host)
        self.assertEqual("https://api.groupe-psa.com/connectedcar/v4", client.api_config.host)
        self.assertEqual("token-1", root.configuration.access_token)

        def refresh(_rejected):
            client.manager.access_token = "token-2"
            return True
        client.manager.refresh_token_now.side_effect = refresh
        self.assertTrue(root.configuration.refresh_callback("token-1"))
        self.assertEqual("token-2", root.configuration.access_token)


class TestStatusText(unittest.TestCase):

    def test_maintenance(self):
        self.assertEqual("12\u202f000 km · 150 days", service_text({"mileage": 12000, "days": 150}))
        self.assertEqual("1 day", service_text({"mileage": None, "days": 1}))
        self.assertEqual("500 km overdue · 3 days overdue",
                         service_text({"mileage": -500, "days": -3}))
        self.assertIsNone(ui.next_service(None))
        self.assertIsNone(ui.next_service({"mileage": "x"}))

    def test_privacy(self):
        self.assertIn("Private mode", status_text.privacy_text("Full"))
        self.assertIsNone(status_text.privacy_text("None"))
        self.assertIsNone(status_text.privacy_text(None))


def get_view(name):
    return get_web_view(name)


def text(component):
    if isinstance(component, (list, tuple)):
        return " ".join(text(child) for child in component if child is not None)
    if isinstance(component, str):
        return component
    children = getattr(component, "children", None)
    return text(children) if children is not None else ""


def ids(component):
    found = []
    if isinstance(component, (list, tuple)):
        for child in component:
            found.extend(ids(child))
        return found
    component_id = getattr(component, "id", None)
    if isinstance(component_id, dict):
        found.append(component_id.get("action"))
    children = getattr(component, "children", None)
    return found + ids(children) if children is not None else found


class TestApi(unittest.TestCase):

    def setUp(self):
        api_view = get_view("api")
        self.myp = services_client(get_status(DATE))
        self.myp.get_vehicle_info(VIN)
        self.patch = patch.object(api_view, "APP", MagicMock(myp=self.myp))
        self.patch.start()
        self.client = api_view.app.test_client()

    def tearDown(self):
        self.patch.stop()

    def test_vehicle_services(self):
        self.myp.vehicles_list[0].status.privacy.state = "Full"
        body = self.client.get(f"/vehicle_services/{VIN}").get_json()
        self.assertEqual({"maintenance": None, "commands": None, "rights": None, "privacy": "Full"}, body)
        self.myp.vehicle_services.refresh(self.myp.vehicles_list[0])
        body = self.client.get(f"/vehicle_services/{VIN}").get_json()
        self.assertEqual(12000, body["maintenance"]["mileage"])
        self.assertFalse(body["commands"]["/Horn"])
        self.assertEqual(404, self.client.get("/vehicle_services/UNKNOWN").status_code)

    def test_stellantis_trips(self):
        self.myp.extra_api.get_trips.return_value = ([{"id": 1}], True)
        response = self.client.get(f"/vehicles/{VIN}/stellantis_trips?since=2026-10-01T00:00:00%2B00:00&max_pages=99")
        self.assertEqual({"trips": [{"id": 1}], "more": True}, response.get_json())
        self.myp.extra_api.get_trips.assert_called_once_with(
            "vehicle-id", datetime(2026, 10, 1, tzinfo=timezone.utc), 10)

    def test_stellantis_trips_errors(self):
        self.assertEqual(400, self.client.get(f"/vehicles/{VIN}/stellantis_trips?since=yesterday").status_code)
        self.myp.extra_api.get_trips.side_effect = api_error(404)
        self.assertEqual(404, self.client.get(f"/vehicles/{VIN}/stellantis_trips").status_code)
        self.myp.extra_api.get_trips.side_effect = api_error(500)
        self.assertEqual(502, self.client.get(f"/vehicles/{VIN}/stellantis_trips?max_pages=2").status_code)
        self.myp.extra_api.get_trips.side_effect = api_error(429)
        self.assertEqual(429, self.client.get(f"/vehicles/{VIN}/stellantis_trips?max_pages=3").status_code)


class TestHomePage(unittest.TestCase):

    def setUp(self):
        self.home = get_view("home")
        self.myp = services_client(get_status(DATE))
        self.myp.get_vehicle_info(VIN)
        self.car = self.myp.vehicles_list[0]
        controller = MagicMock(myp=self.myp, remote_control=True, offline=True)
        controller.chc.get.return_value = None
        from psa_car_controller.web.view import car_access  # pylint: disable=import-outside-toplevel
        for module in (self.home, car_access):
            patcher = patch.object(module, "app", return_value=controller)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(self.home, "_picture", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_every_command_while_the_rights_are_unknown(self):
        page = self.home.render_status(self.car)
        self.assertEqual(["refresh", "charge", "precond"], ids(page))
        self.assertNotIn("Next service", text(page))
        self.assertNotIn("Private mode", text(page))

    def test_only_commands_of_the_subscription(self):
        self.myp.extra_api.get_rights.return_value = {"services": {"u": [
            {"code": "RTP", "mqtt_services": ["ThermalPrecond"]}]}}
        self.myp.vehicle_services.refresh(self.car)
        self.assertEqual(["precond"], ids(self.home.render_status(self.car)))

    def test_maintenance_and_private_mode(self):
        self.myp.vehicle_services.refresh(self.car)
        self.car.status.privacy.state = "Full"
        page = re.sub(" +", " ", text(self.home.render_status(self.car)))  # text() puts spaces between the children
        self.assertIn("Next service", page)
        self.assertIn("12\u202f000 km · 150 days", page)
        self.assertIn("Private mode is on", page)
