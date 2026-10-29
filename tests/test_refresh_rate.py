"""PSA blocks accounts that request the status too often (#1130, #888, #967, #1061).

Before, the status was requested every 120 s whatever the car did, and an answer
"429 Too Many Requests" was retried right away and again 120 s later.
"""
import unittest
from unittest.mock import MagicMock, patch

from psa_car_controller.common.utils import RateLimitException
from psa_car_controller.psacc.application import psa_client as psa_client_module
from psa_car_controller.psacc.application.psa_client import PSAClient, MAX_REFRESH_DELAY, CAR_STAGGER_DELAY
from psa_car_controller.psacc.model.car import Car
from tests.test_status_refresh import get_client, get_status, api_error, get_car

DATE = "2022-03-26T11:02:54Z"


def new_client(*responses) -> PSAClient:
    client = get_client(*responses)
    client.refresh_thread = None
    client.info_callback = []
    return client


def charging_status():
    status = get_status(DATE)
    status.energy[1].charging.status = "InProgress"
    return status


class TestRefreshDelay(unittest.TestCase):

    def setUp(self):
        self.delays = []

        def new_timer(delay, _function):
            self.delays.append(delay)
            return MagicMock()

        patcher = patch.object(psa_client_module.threading, "Timer", side_effect=new_timer)
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def refresh(client: PSAClient):
        client._PSAClient__refresh_vehicle_info()

    def test_default_rate_is_five_minutes(self):
        client = new_client(get_status(DATE))
        client.start_refresh_thread()
        self.assertEqual([300], self.delays)
        self.assertEqual(300, client.refresh_delay)

    def test_a_charging_car_is_refreshed_more_often(self):
        client = new_client(charging_status(), get_status(DATE))
        self.refresh(client)
        self.refresh(client)
        self.assertEqual([120, 300], self.delays)

    def test_chosen_rate_applies_while_charging(self):
        # -R 10
        client = new_client(charging_status())
        client.info_refresh_rate = client.charging_refresh_rate = 600
        self.refresh(client)
        self.assertEqual([600], self.delays)

    def test_too_many_requests_isnt_retried_right_away(self):
        client = new_client(api_error(429), get_status(DATE))
        self.refresh(client)
        self.assertEqual(1, client.api().get_vehicle_status.call_count)
        self.assertEqual(1, get_car(client).status_failures)

    def test_too_many_requests_doubles_the_delay_up_to_an_hour(self):
        client = new_client(*[api_error(429)] * 5, get_status(DATE))
        for _ in range(6):
            self.refresh(client)
        self.assertEqual([600, 1200, 2400, MAX_REFRESH_DELAY, MAX_REFRESH_DELAY, 300], self.delays)

    def test_too_many_requests_from_the_web_api_delays_the_next_refresh(self):
        client = new_client(api_error(429), get_status(DATE))
        client.get_vehicle_info("VR3UHZKXZLT000000")
        self.refresh(client)
        self.assertEqual([600], self.delays)

    def test_a_rate_above_the_maximum_isnt_shortened(self):
        client = new_client(api_error(429))
        client.info_refresh_rate = 2 * MAX_REFRESH_DELAY
        self.refresh(client)
        self.assertEqual([2 * MAX_REFRESH_DELAY], self.delays)

    def test_rate_limit_exception_delays_the_next_refresh(self):
        client = new_client(RateLimitException("refresh_token_now"))
        self.refresh(client)
        self.assertEqual([600], self.delays)

    def test_stopped_while_refreshing_schedules_nothing(self):
        client = new_client(get_status(DATE))
        client.api().get_vehicle_status.side_effect = lambda _id: client.stop() or get_status(DATE)
        self.refresh(client)
        self.assertEqual([], self.delays)

    def test_started_twice_refreshes_once(self):
        client = new_client(get_status(DATE), get_status(DATE))
        client.start_refresh_thread()
        client.start_refresh_thread()
        self.assertEqual(1, client.api().get_vehicle_status.call_count)

    def test_a_single_car_is_asked_without_waiting(self):
        client = new_client(get_status(DATE))
        client._stop_event = MagicMock()
        self.refresh(client)
        client._stop_event.wait.assert_not_called()
        self.assertEqual([300], self.delays)


SECOND_VIN = "VR3UHZKXZLT000001"


class TestStaggeredCars(unittest.TestCase):
    """Several cars: their status requests are spread out, like stagger_first_poll of the HA integration."""

    def setUp(self):
        self.delays = []
        self.events = []

        def new_timer(delay, _function):
            self.delays.append(delay)
            return MagicMock()

        patcher = patch.object(psa_client_module.threading, "Timer", side_effect=new_timer)
        patcher.start()
        self.addCleanup(patcher.stop)

    def new_client(self, *responses, cars=2) -> PSAClient:
        client = new_client(*responses)
        for i in range(1, cars):
            client.vehicles_list.append(Car(SECOND_VIN[:-1] + str(i), "vehicle-id-%d" % i, "Peugeot"))
        client.api().get_vehicle_status.side_effect = self.record(list(responses))
        client._stop_event = MagicMock()
        client._stop_event.wait.side_effect = self.wait
        return client

    def record(self, responses):
        def get_vehicle_status(vehicle_id):
            self.events.append(vehicle_id)
            response = responses.pop(0)
            if isinstance(response, BaseException):
                raise response
            return response
        return get_vehicle_status

    def wait(self, delay):
        self.events.append(delay)
        return False

    @staticmethod
    def refresh(client: PSAClient):
        client._PSAClient__refresh_vehicle_info()

    def test_the_second_car_is_asked_later(self):
        client = self.new_client(get_status(DATE), get_status(DATE))
        self.refresh(client)
        self.assertEqual(["vehicle-id", CAR_STAGGER_DELAY, "vehicle-id-1"], self.events)

    def test_each_car_is_still_asked_once_per_refresh_rate(self):
        client = self.new_client(get_status(DATE), get_status(DATE))
        self.refresh(client)
        self.assertEqual([300 - CAR_STAGGER_DELAY], self.delays)
        self.assertEqual(300 - CAR_STAGGER_DELAY, client.refresh_delay)

    def test_charging_rate_is_kept(self):
        client = self.new_client(charging_status(), get_status(DATE))
        self.refresh(client)
        self.assertEqual([120 - CAR_STAGGER_DELAY], self.delays)

    def test_all_cars_are_asked_within_the_shortest_rate(self):
        # -R 1 with five cars: 12 s between them, not 15
        client = self.new_client(*[get_status(DATE)] * 5, cars=5)
        client.info_refresh_rate = client.charging_refresh_rate = 60
        self.refresh(client)
        self.assertEqual([12, 12, 12, 12], [e for e in self.events if not isinstance(e, str)])
        self.assertEqual([60 - 48], self.delays)

    def test_too_many_requests_stops_asking_the_other_cars(self):
        client = self.new_client(api_error(429), get_status(DATE))
        self.refresh(client)
        self.assertEqual(["vehicle-id"], self.events)
        self.assertEqual([600], self.delays)

    def test_stopped_while_waiting_asks_no_more_cars(self):
        client = self.new_client(get_status(DATE), get_status(DATE))
        client._stop_event.wait.side_effect = None
        client._stop_event.wait.return_value = True
        self.refresh(client)
        self.assertEqual(["vehicle-id"], self.events)
        self.assertEqual([], self.delays)

    def test_stop_ends_the_wait(self):
        client = self.new_client()
        client.manager = MagicMock()
        client.command_refresh = None
        client.stop()
        client._stop_event.set.assert_called_once()

    def test_start_doesnt_wait_for_the_other_cars(self):
        client = self.new_client(get_status(DATE), get_status(DATE))
        with patch.object(psa_client_module.threading, "Thread") as thread:
            client.start_refresh_thread()
        self.assertEqual([], self.events)
        thread.return_value.start.assert_called_once()


if __name__ == '__main__':
    unittest.main()
