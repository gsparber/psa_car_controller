import unittest
from unittest.mock import MagicMock, patch

from psa_car_controller.common.utils import RateLimitException
from psa_car_controller.psa import RemoteClient as remote_client_module
from psa_car_controller.psa.RemoteClient import KEEPALIVE_PERIOD, KEEPALIVE_RETRY_PERIOD, WAKEUP_LIMIT, \
    WAKEUP_PERIOD, WakeupRateLimiter

from tests.test_refresh_remote_token import get_remote_client

VIN = "VR3UHZKX"
OTHER_VIN = "VXXXXX"


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class TestWakeupRateLimit(unittest.TestCase):

    def setUp(self):
        self.clock = FakeClock()
        limiter = WakeupRateLimiter(clock=self.clock)
        patcher = patch.object(remote_client_module, "wakeup_limiter", limiter)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.rc = get_remote_client()
        self.rc.publish = MagicMock(return_value=True)
        self.rc.mqtt_request = MagicMock(return_value="request")

    def test_wakeup_is_limited_per_car(self):
        # GIVEN a car woken up WAKEUP_LIMIT times
        for _ in range(WAKEUP_LIMIT):
            self.assertTrue(self.rc.wakeup(VIN))

        # WHEN it is woken up once more within WAKEUP_PERIOD
        # THEN it is refused without sending anything
        with self.assertRaises(RateLimitException):
            self.rc.wakeup(VIN)
        self.assertEqual(self.rc.publish.call_count, WAKEUP_LIMIT)

        # AND another car still has its own budget (the old limit was shared by all cars)
        self.assertTrue(self.rc.wakeup(OTHER_VIN))

    def test_wakeup_budget_comes_back_after_period(self):
        for _ in range(WAKEUP_LIMIT):
            self.rc.wakeup(VIN)
        self.clock.now += WAKEUP_PERIOD
        self.assertTrue(self.rc.wakeup(VIN))

    def test_failed_publish_does_not_count(self):
        # GIVEN the remote token can't be refreshed, so nothing reaches the car
        self.rc.publish.return_value = False
        for _ in range(WAKEUP_LIMIT + 1):
            self.assertFalse(self.rc.wakeup(VIN))

        # WHEN publishing works again THEN the car can still be woken up
        self.rc.publish.return_value = True
        self.assertTrue(self.rc.wakeup(VIN))

    def test_limit_survives_a_new_client(self):
        # a new login replaces the RemoteClient: the car's budget must not be reset
        for _ in range(WAKEUP_LIMIT):
            self.rc.wakeup(VIN)
        new_rc = get_remote_client()
        new_rc.publish = MagicMock(return_value=True)
        new_rc.mqtt_request = MagicMock(return_value="request")
        with self.assertRaises(RateLimitException):
            new_rc.wakeup(VIN)


class TestKeepaliveWithoutWakeup(unittest.TestCase):

    def run_keepalive(self, refreshed):
        rc = get_remote_client()
        rc.vehicles_list = [MagicMock(vin=VIN)]
        rc.wakeup = MagicMock()
        rc.publish = MagicMock()
        rc._refresh_remote_token = MagicMock(return_value=refreshed)
        rc._RemoteClient__keep_mqtt()
        self.addCleanup(rc.update_thread.cancel)
        return rc

    def test_keepalive_only_refreshes_the_remote_token(self):
        # WHEN the keepalive runs
        rc = self.run_keepalive(True)

        # THEN the token is refreshed but no command is sent to the car: each wakeup draws
        # on its 12V battery
        rc._refresh_remote_token.assert_called_once_with()
        rc.wakeup.assert_not_called()
        rc.publish.assert_not_called()
        self.assertEqual(rc.update_thread.interval, KEEPALIVE_PERIOD)

    def test_failed_keepalive_is_retried_sooner(self):
        rc = self.run_keepalive(False)
        rc.wakeup.assert_not_called()
        self.assertEqual(rc.update_thread.interval, KEEPALIVE_RETRY_PERIOD)

    def test_keepalive_not_rescheduled_after_stop(self):
        rc = get_remote_client()
        rc._refresh_remote_token = MagicMock(return_value=True)
        rc.stop()
        rc._RemoteClient__keep_mqtt()
        self.assertIsNone(rc.update_thread)


if __name__ == '__main__':
    unittest.main()
