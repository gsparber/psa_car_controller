"""The home page and /get_vehicleinfo say how old the status is.

Before, the home page only showed the time of the status, without saying it's outdated when
the API stopped answering, and the API had no age at all.
"""
import copy
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from psa_car_controller.psa.connected_car_api import ApiClient
from psa_car_controller.psacc.model.car import Car, EMPTY_STATUS_LIMIT
from psa_car_controller.psacc.model.car_status import status_age_seconds
from psa_car_controller.web.view import status_text
from tests.data.car_status import ELECTRIC_CAR_STATUS
from tests.test_remote_command_api import get_api_view
from tests.test_remote_command_ui import get_car_access, text

VIN = "VR3UHZKXZLT000000"
NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)


def without_dates(data):
    if isinstance(data, dict):
        return {key: without_dates(value) for key, value in data.items() if key not in ("updatedAt", "createdAt")}
    if isinstance(data, list):
        return [without_dates(value) for value in data]
    return data


def get_status(updated_at=None):
    status = ApiClient()._ApiClient__deserialize(without_dates(copy.deepcopy(ELECTRIC_CAR_STATUS)), "Status")
    status.updated_at = updated_at
    return status


def get_car(updated_at=None, failures=0):
    car = Car(VIN, "vehicle-id", "Peugeot")
    car.status = get_status(updated_at)
    for _ in range(failures):
        car.status_request_failed()
    return car


class TestStatusAge(unittest.TestCase):

    def test_aware_date(self):
        status = get_status(NOW - timedelta(minutes=5))
        self.assertEqual(300, status_age_seconds(status, NOW))

    def test_other_timezone(self):
        status = get_status(datetime(2026, 10, 30, 13, 55, tzinfo=timezone(timedelta(hours=2))))
        self.assertEqual(300, status_age_seconds(status, NOW))

    def test_naive_date_is_utc(self):
        status = get_status(datetime(2026, 10, 30, 11, 55))
        self.assertEqual(300, status_age_seconds(status, NOW))

    def test_no_date(self):
        self.assertIsNone(status_age_seconds(get_status(), NOW))

    def test_naive_and_aware_parts_without_top_level_date(self):
        status = get_status()
        status.timed_odometer.updated_at = datetime(2026, 10, 30, 11, 55)
        status.last_position.properties.updated_at = NOW
        self.assertIsNone(status_age_seconds(status, NOW))

    def test_date_in_the_future(self):
        self.assertEqual(0, status_age_seconds(get_status(NOW + timedelta(minutes=5)), NOW))


class TestAgeText(unittest.TestCase):

    def test_format_age(self):
        for seconds, expected in ((0, "just now"), (59, "just now"), (60, "1 min ago"), (3599, "59 min ago"),
                                  (3600, "1 h ago"), (47 * 3600, "47 h ago"), (48 * 3600, "2 days ago")):
            self.assertEqual(expected, status_text.format_age(seconds))

    def test_format_age_unknown(self):
        self.assertIsNone(status_text.format_age(None))

    def test_outdated_text(self):
        self.assertIn("3 h ago", status_text.outdated_text(3 * 3600))
        self.assertEqual("No new data from the API", status_text.outdated_text(None))


class TestHomeShowsAge(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        get_car_access()
        from psa_car_controller.web.view import home  # pylint: disable=import-outside-toplevel
        cls.home = home

    def hero(self, car):
        with patch.object(self.home, "_picture", return_value=None), \
                patch.object(self.home, "command_supported", return_value=False), \
                patch.object(self.home.units, "current", return_value=self.home.units.KM):
            return self.home._hero(car, True)

    def has_warning(self, component):
        if getattr(component, "className", None) == "status-outdated small":
            return True
        children = getattr(component, "children", None)
        if not isinstance(children, (list, tuple)):
            children = [children]
        return any(self.has_warning(child) for child in children if child is not None and not isinstance(child, str))

    def test_age_is_shown(self):
        hero = self.hero(get_car(datetime.now(timezone.utc) - timedelta(minutes=5)))
        self.assertIn("min ago", text(hero))
        self.assertFalse(self.has_warning(hero))

    def test_outdated_status_is_pointed_out(self):
        hero = self.hero(get_car(NOW, failures=EMPTY_STATUS_LIMIT))
        self.assertTrue(self.has_warning(hero))
        self.assertIn("No new data from the API, last status from", text(hero))

    def test_few_failures_are_no_warning(self):
        hero = self.hero(get_car(NOW, failures=EMPTY_STATUS_LIMIT - 1))
        self.assertFalse(self.has_warning(hero))

    def test_status_without_date(self):
        hero = self.hero(get_car())
        self.assertNotIn("updated", text(hero))
        self.assertNotIn("ago", text(hero))


class TestApiShowsAge(unittest.TestCase):

    @staticmethod
    def get_answer(car):
        api_view = get_api_view()
        client = MagicMock()
        client.get_vehicle_info.return_value = car.status
        client.vehicles_list.get_car_by_vin.return_value = car
        with patch.object(api_view, "APP", MagicMock(myp=client)), api_view.app.test_request_context():
            return api_view.get_vehicle_info(VIN)

    def test_age_is_added(self):
        car = get_car(datetime.now(timezone.utc) - timedelta(hours=1))
        answer = json.loads(self.get_answer(car).get_data())
        self.assertAlmostEqual(3600, answer["data_age_seconds"], delta=60)

    def test_other_fields_stay(self):
        car = get_car(NOW)
        answer = json.loads(self.get_answer(car).get_data())
        answer.pop("data_age_seconds")
        self.assertEqual(json.loads(json.dumps(car.status.to_dict(), default=str)), answer)

    def test_no_date_is_null(self):
        answer = json.loads(self.get_answer(get_car()).get_data())
        self.assertIn("data_age_seconds", answer)
        self.assertIsNone(answer["data_age_seconds"])


if __name__ == "__main__":
    unittest.main()
