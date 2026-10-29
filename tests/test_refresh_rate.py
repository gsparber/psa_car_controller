"""PSA blocks accounts that request the status too often (#1130, #888, #967, #1061).

Before, the status was requested every 120 s whatever the car did, and an answer
"429 Too Many Requests" was retried right away and again 120 s later.
"""
import unittest
from unittest.mock import MagicMock, patch

from psa_car_controller.common.utils import RateLimitException
from psa_car_controller.psacc.application import psa_client as psa_client_module
from psa_car_controller.psacc.application.psa_client import PSAClient, MAX_REFRESH_DELAY
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


if __name__ == '__main__':
    unittest.main()
