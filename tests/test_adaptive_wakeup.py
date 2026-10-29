import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from psa_car_controller.common.utils import RateLimitException
from psa_car_controller.psa.constants import INPROGRESS
from psa_car_controller.psacc.application.charge_control import ChargeControl

VIN = "VR3UHZKX"


class TestAdaptiveWakeup(unittest.TestCase):

    def setUp(self):
        self.electric = SimpleNamespace(
            level=50,
            updated_at=datetime.now(timezone.utc),
            charging=SimpleNamespace(status=INPROGRESS, charging_mode="Slow", remaining_time=None))
        status = SimpleNamespace(get_energy=lambda energy_type: self.electric)
        car = SimpleNamespace(get_status=lambda: status)
        self.status = status
        self.psacc = MagicMock()
        self.psacc.vehicles_list.get_car_by_vin.return_value = car
        self.wakeup = self.psacc.remote_client.wakeup
        self.wakeup.return_value = True

    def charge_control(self, threshold=80):
        # noinspection PyTypeChecker
        return ChargeControl(self.psacc, VIN, threshold, None)

    def no_update_since(self, minutes):
        self.electric.updated_at = datetime.now(timezone.utc) - timedelta(minutes=minutes)

    def test_car_isnt_woken_up_again_on_every_refresh(self):
        # GIVEN a slow charge which doesn't update the api for 12 minutes (#1130)
        self.no_update_since(12)
        cc = self.charge_control()

        # WHEN charge control runs on every refresh and the car doesn't answer the wakeup
        for _ in range(5):
            cc.force_update(self.status)

        # THEN the car is woken up once, not on every refresh
        self.wakeup.assert_called_once_with(VIN)

    def test_car_is_woken_up_again_after_the_interval(self):
        self.no_update_since(30)
        cc = self.charge_control()
        cc.force_update(self.status)

        cc._last_wakeup -= timedelta(minutes=11)
        cc.force_update(self.status)

        self.assertEqual(2, self.wakeup.call_count)

    def test_failed_wakeup_is_retried(self):
        self.no_update_since(12)
        cc = self.charge_control()
        self.wakeup.side_effect = RateLimitException("too many wakeups")
        cc.force_update(self.status)

        self.wakeup.side_effect = None
        cc.force_update(self.status)

        self.assertEqual(2, self.wakeup.call_count)

    def test_far_from_threshold_wakes_up_rarely(self):
        # GIVEN 20 % -> 80 % with 8 h left to 100 %: about 6 h to the threshold
        self.electric.level = 20
        self.electric.charging.remaining_time = "PT8H"
        self.no_update_since(30)
        cc = self.charge_control(80)

        cc.force_update(self.status)

        self.wakeup.assert_not_called()
        self.assertEqual(ChargeControl.MAX_WAKEUP_TIMEOUT, cc.wakeup_interval(self.status, 30))

    def test_close_to_threshold_wakes_up_often(self):
        # GIVEN 78 % -> 80 % with 4 h 24 min left to 100 %: 24 min to the threshold
        self.electric.level = 78
        self.electric.charging.remaining_time = "PT4H24M"
        cc = self.charge_control(80)

        self.assertEqual(12, cc.wakeup_interval(self.status))
        # the time elapsed since the last update counts: the threshold is closer than the api says
        self.assertEqual(10, cc.wakeup_interval(self.status, 12))

        self.no_update_since(11)
        cc.force_update(self.status)
        self.wakeup.assert_called_once_with(VIN)

    def test_interval_shrinks_as_the_threshold_gets_closer(self):
        self.electric.level = 40
        self.electric.charging.remaining_time = "PT6H"
        cc = self.charge_control(80)

        intervals = [cc.wakeup_interval(self.status, minutes) for minutes in (0, 120, 180, 230)]

        self.assertEqual(sorted(intervals, reverse=True), intervals)
        self.assertEqual(60, intervals[0])
        self.assertEqual(10, intervals[-1])

    def test_quick_charge_keeps_shorter_minimum(self):
        self.electric.charging.charging_mode = "Quick"
        self.electric.level = 79
        self.electric.charging.remaining_time = "PT30M"
        cc = self.charge_control(80)

        self.assertEqual(5, cc.wakeup_interval(self.status))

    def test_unknown_charge_rate_keeps_minimum(self):
        cc = self.charge_control(80)
        for remaining_time in (None, "PT0S", "P1DT2H", "garbage"):
            self.electric.charging.remaining_time = remaining_time
            self.assertEqual(10, cc.wakeup_interval(self.status), remaining_time)

    def test_without_threshold_wakes_up_rarely(self):
        # GIVEN no threshold, only a stop hour, which doesn't need a fresh battery level
        self.electric.charging.remaining_time = "PT1H"
        self.no_update_since(30)
        cc = self.charge_control(100)

        cc.force_update(self.status)

        self.wakeup.assert_not_called()

    def test_get_dict_is_unchanged(self):
        cc = self.charge_control()
        self.no_update_since(12)
        cc.force_update(self.status)

        self.assertNotIn("_last_wakeup", cc.get_dict())


if __name__ == '__main__':
    unittest.main()
