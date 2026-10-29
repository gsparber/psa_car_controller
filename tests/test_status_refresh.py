"""The status the API returns isn't always the newest, and requesting it fails now and then.

Before, every answer replaced the status, so a cached older one made the values flip
between old and new (#1242) and recorded the same data again. After two failed requests
the status was set to None: the UI, the API and charge control had no data until the
next successful request.
"""
import copy
import json
import unittest
from unittest.mock import MagicMock, patch

from psa_car_controller.psa.connected_car_api import ApiClient
from psa_car_controller.psa.connected_car_api.rest import ApiException
from psa_car_controller.psacc.application import psa_client as psa_client_module
from psa_car_controller.psacc.application.psa_client import PSAClient
from psa_car_controller.psacc.model.car import Car, Cars, EMPTY_STATUS_LIMIT
from tests.data.car_status import ELECTRIC_CAR_STATUS

VIN = "VR3UHZKXZLT000000"


def get_status(updated_at, level=59, top_level_date=True):
    data = copy.deepcopy(ELECTRIC_CAR_STATUS)
    data["energy"][1]["level"] = level
    if top_level_date:
        data["updatedAt"] = updated_at
    else:
        del data["updatedAt"]
        data["energy"][1]["updatedAt"] = updated_at
    return ApiClient()._ApiClient__deserialize(data, "Status")


def api_error(status, code=None):
    ex = ApiException(status=status, reason="error")
    if code is not None:
        ex.body = json.dumps({"code": code, "message": "error"}).encode()
    return ex


def get_client(*responses) -> PSAClient:
    client = PSAClient.__new__(PSAClient)  # bypass __init__, it does I/O
    client.vehicles_list = Cars([Car(VIN, "vehicle-id", "Peugeot")])
    client.remote_client = MagicMock()
    client._record_enabled = True
    client.record_info = MagicMock()
    vehicles_api = MagicMock()
    vehicles_api.get_vehicle_status.side_effect = list(responses)
    client.api = MagicMock(return_value=vehicles_api)
    return client


def get_car(client: PSAClient) -> Car:
    return client.vehicles_list.get_car_by_vin(VIN)


class TestOnlyNewerStatus(unittest.TestCase):

    def test_an_older_status_is_ignored(self):
        client = get_client(get_status("2022-03-26T11:02:54Z", level=60),
                            get_status("2022-03-26T10:00:00Z", level=40))
        client.get_vehicle_info(VIN)
        res = client.get_vehicle_info(VIN)
        self.assertEqual(60, res.get_energy("Electric").level)
        self.assertEqual(60, get_car(client).status.get_energy("Electric").level)
        client.record_info.assert_called_once()

    def test_an_older_status_without_updated_at_is_ignored(self):
        client = get_client(get_status("2022-03-26T11:02:54Z", level=60, top_level_date=False),
                            get_status("2022-03-26T10:00:00Z", level=40, top_level_date=False))
        client.get_vehicle_info(VIN)
        client.get_vehicle_info(VIN)
        self.assertEqual(60, get_car(client).status.get_energy("Electric").level)

    def test_a_newer_status_is_taken_and_recorded(self):
        client = get_client(get_status("2022-03-26T11:02:54Z", level=60),
                            get_status("2022-03-26T11:10:00Z", level=61))
        client.get_vehicle_info(VIN)
        client.get_vehicle_info(VIN)
        self.assertEqual(61, get_car(client).status.get_energy("Electric").level)
        self.assertEqual(2, client.record_info.call_count)

    def test_the_same_status_is_not_recorded_again(self):
        client = get_client(get_status("2022-03-26T11:02:54Z"), get_status("2022-03-26T11:02:54Z"))
        client.get_vehicle_info(VIN)
        client.get_vehicle_info(VIN)
        client.record_info.assert_called_once()

    def test_the_status_date_is_kept(self):
        status = get_status("2022-03-26T11:02:54Z")
        self.assertEqual("2022-03-26T11:02:54+00:00", status.updated_at.isoformat())


class TestKeepStatusOnErrors(unittest.TestCase):

    def test_the_status_is_kept_when_the_api_fails(self):
        client = get_client(get_status("2022-03-26T11:02:54Z", level=60), api_error(503), api_error(503))
        client.get_vehicle_info(VIN)
        res = client.get_vehicle_info(VIN)
        self.assertEqual(60, res.get_energy("Electric").level)
        self.assertIsNotNone(get_car(client).status)
        self.assertEqual(1, get_car(client).status_failures)
        self.assertFalse(get_car(client).status_outdated)

    def test_the_status_is_outdated_after_failures_in_a_row(self):
        client = get_client(get_status("2022-03-26T11:02:54Z"), *[api_error(503)] * (2 * EMPTY_STATUS_LIMIT),
                            get_status("2022-03-26T11:10:00Z"))
        client.get_vehicle_info(VIN)
        with self.assertLogs(psa_client_module.logger.name, "ERROR") as logs:
            for _ in range(EMPTY_STATUS_LIMIT):
                self.assertIsNotNone(client.get_vehicle_info(VIN))
        self.assertTrue(get_car(client).status_outdated)
        self.assertTrue(any("outdated" in line for line in logs.output))
        client.get_vehicle_info(VIN)
        self.assertEqual(0, get_car(client).status_failures)
        self.assertFalse(get_car(client).status_outdated)

    def test_no_status_yet_stays_none(self):
        client = get_client(api_error(503), api_error(503))
        self.assertIsNone(client.get_vehicle_info(VIN))

    def test_transient_errors_are_not_retried(self):
        for status, code in ((404, 40400), (500, 50038)):
            with self.subTest(code=code):
                client = get_client(api_error(status, code), get_status("2022-03-26T11:02:54Z"))
                with self.assertLogs(psa_client_module.logger.name, "WARNING") as logs:
                    self.assertIsNone(client.get_vehicle_info(VIN))
                self.assertEqual(1, client.api().get_vehicle_status.call_count)
                self.assertFalse(any("ERROR" in line for line in logs.output))

    def test_a_replaced_connection_module_is_explained(self):
        client = get_client(api_error(500, 50000), api_error(500, 50000))
        with self.assertLogs(psa_client_module.logger.name, "ERROR") as logs:
            client.get_vehicle_info(VIN)
        self.assertTrue(any("connection module" in line for line in logs.output))

    def test_other_errors_are_retried(self):
        client = get_client(api_error(500, 12345), get_status("2022-03-26T11:02:54Z"))
        self.assertIsNotNone(client.get_vehicle_info(VIN))
        self.assertEqual(0, get_car(client).status_failures)

    def test_the_failure_count_is_not_saved(self):
        car = Car(VIN, "vehicle-id", "Peugeot")
        car.status_request_failed()
        self.assertNotIn("_status_failures", car.to_dict())
        Car.from_json(car.to_dict())


class TestOutdatedStatusHeader(unittest.TestCase):

    def test_the_api_says_the_status_is_outdated(self):
        from psa_car_controller.web.view import api as api_view  # pylint: disable=import-outside-toplevel
        client = get_client(get_status("2022-03-26T11:02:54Z"), *[api_error(503)] * (2 * EMPTY_STATUS_LIMIT))
        client.get_vehicle_info(VIN)
        for _ in range(EMPTY_STATUS_LIMIT - 1):
            client.get_vehicle_info(VIN)
        with patch.object(api_view, "APP", MagicMock(myp=client)), api_view.app.test_request_context():
            response = api_view.get_vehicle_info(VIN)
        self.assertEqual(200, response.status_code)
        self.assertEqual(str(EMPTY_STATUS_LIMIT), response.headers["X-Status-Outdated"])
