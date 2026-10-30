import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from psa_car_controller.psa.RemoteClient import RemoteClient
from psa_car_controller.psa.connected_car_api.models.energy import Energy
from psa_car_controller.psa.connected_car_api.models.energy_charging import EnergyCharging
from psa_car_controller.psa.constants import DELAYED_CHARGE, INPROGRESS, STOPPED
from psa_car_controller.psa.mqtt_event import VehicleEvents
from psa_car_controller.psacc.application.charge_control import ChargeControl
from psa_car_controller.psacc.model.car import Car, Cars, EMPTY_STATUS_LIMIT
from psa_car_controller.psacc.model.car_status import CarStatus

VIN = "VR3UHZKXZLT000000"
HOUR = "PT22H"
MQTT_IN_PROGRESS = 1


def charging(status=INPROGRESS, next_delayed_time=HOUR):
    return EnergyCharging(status=status, next_delayed_time=next_delayed_time, charging_mode="Slow")


def get_status(energy_charging, level=85, minutes_old=0):
    date = datetime.now(timezone.utc) - timedelta(minutes=minutes_old)
    return CarStatus(links={}, updated_at=date,
                     energy=[Energy(type="Electric", level=level, updated_at=date, charging=energy_charging)])


class TestChargeControlStatus(unittest.TestCase):
    """What happens when the status doesn't tell the charging state, or when it is outdated."""

    def setUp(self):
        patcher = patch.object(ChargeControl, "MQTT_TIMEOUT", 0)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.car = Car(VIN, "vehicle-id", "Peugeot")
        self.psacc = MagicMock()
        self.psacc.vehicles_list = Cars([self.car])
        self.psacc.refresh_delay = 300
        self.psacc.get_vehicle_info.side_effect = lambda vin: self.car.status
        self.remote = self.psacc.remote_client
        # the real command: what it needs from the status is part of what is tested
        self.remote.vehicles_list = self.psacc.vehicles_list
        self.remote.get_charge_hour.side_effect = lambda vin: RemoteClient.get_charge_hour(self.remote, vin)
        self.remote.charge_now.side_effect = lambda vin, now: RemoteClient.charge_now(self.remote, vin, now)

    def charge_control(self, threshold=80, stop_due=False):
        # noinspection PyTypeChecker
        cc = ChargeControl(self.psacc, VIN, threshold, None)
        if stop_due:
            cc._next_stop_hour = datetime.now() - timedelta(minutes=1)
        return cc

    def set_status(self, status, outdated=False):
        self.car.status = status
        self.car._status_failures = EMPTY_STATUS_LIMIT if outdated else 0

    def overlay_mqtt_charge(self):
        events = VehicleEvents(VIN)
        events.add({"date": "2020-01-01T00:00:00Z", "obj_counter": 1, "charging_state": {"hmi_state": 0}})
        events.add({"date": (datetime.now(timezone.utc) + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "obj_counter": 2, "charging_state": {"hmi_state": MQTT_IN_PROGRESS}})
        events.apply(self.car.status)

    def stops(self):
        return self.remote.veh_charge_request.call_count

    # the charging state is missing

    def test_threshold_reached_without_charging_status_stops(self):
        self.set_status(get_status(charging(status=None)))
        cc = self.charge_control()

        cc.process()

        self.remote.veh_charge_request.assert_called_once_with(VIN, 22, 0, DELAYED_CHARGE)

    def test_stop_without_charging_status_is_limited_for_a_parked_car(self):
        self.set_status(get_status(charging(status=None)))
        cc = self.charge_control()

        for _ in range(5):
            cc.process()

        self.assertEqual(2, self.stops())
        self.remote.wakeup.assert_not_called()

    def test_stop_without_charging_status_again_after_the_next_charge(self):
        self.set_status(get_status(charging(status=None)))
        cc = self.charge_control()
        for _ in range(2):
            cc.process()
        self.set_status(get_status(charging(status=None), level=50))
        cc.process()

        self.set_status(get_status(charging(status=None), level=81))
        cc.process()

        self.assertEqual(3, self.stops())

    def test_below_threshold_without_charging_status_does_nothing(self):
        self.set_status(get_status(charging(status=None), level=50))

        self.charge_control().process()

        self.assertEqual(0, self.stops())
        self.remote.wakeup.assert_not_called()

    def test_stop_hour_without_charging_status_isnt_skipped(self):
        self.set_status(get_status(charging(status=None)))
        cc = self.charge_control(threshold=100, stop_due=True)

        for _ in range(3):
            cc.process()

        self.assertEqual(1, self.stops())
        self.assertGreater(cc._next_stop_hour, datetime.now())

    def test_without_charging_data_the_schedule_isnt_overwritten(self):
        # GIVEN no charging section, so no charge hour to send along with the stop
        self.set_status(get_status(None))
        cc = self.charge_control(stop_due=True)
        due = cc._next_stop_hour

        with patch("psa_car_controller.psacc.application.charge_control.logger") as log:
            cc.process()

        self.assertEqual(0, self.stops())
        log.exception.assert_not_called()
        # THEN the stop hour stays due for the next status
        self.assertEqual(due, cc._next_stop_hour)
        self.set_status(get_status(charging()))
        cc.process()
        self.assertGreaterEqual(self.stops(), 1)

    def test_without_any_status_nothing_is_sent_and_no_traceback(self):
        cc = self.charge_control(stop_due=True)

        with patch("psa_car_controller.psacc.application.charge_control.logger") as log:
            cc.process()

        self.remote.charge_now.assert_not_called()
        log.exception.assert_not_called()

    def test_stop_hour_missed_for_days_stops_once(self):
        self.set_status(get_status(charging()))
        cc = self.charge_control(threshold=100)
        cc._next_stop_hour = datetime.now() - timedelta(days=3)

        cc.process()
        first = self.stops()
        for _ in range(3):
            cc.process()

        self.assertEqual(first, self.stops())
        self.assertGreater(cc._next_stop_hour, datetime.now())

    # other missing values don't prevent a stop

    def test_charging_without_level_still_stops_at_the_stop_hour(self):
        self.set_status(get_status(charging(), level=None))
        cc = self.charge_control(stop_due=True)

        cc.process()

        self.assertGreaterEqual(self.stops(), 1)

    def test_charging_without_update_date_still_stops(self):
        status = get_status(charging())
        status.get_energy('Electric').updated_at = None
        self.set_status(status)

        self.charge_control().process()

        self.assertGreaterEqual(self.stops(), 1)
        self.remote.wakeup.assert_called_once_with(VIN)

    def test_charging_without_charge_hour_still_stops(self):
        # GIVEN the charging state only known from mqtt: the api didn't send any charging data
        self.set_status(get_status(None, minutes_old=1))
        self.overlay_mqtt_charge()
        self.assertEqual(INPROGRESS, self.car.status.get_energy('Electric').charging.status)
        now = datetime.now()

        self.charge_control().process()

        # THEN the charge stops, until the hour before now: tomorrow, as when postponing a charge
        self.assertGreaterEqual(self.stops(), 1)
        self.assertEqual((VIN, (now.hour - 1) % 24, 0, DELAYED_CHARGE), self.remote.veh_charge_request.call_args[0])

    def test_stopped_without_charge_hour_has_no_traceback(self):
        self.set_status(get_status(charging(status=STOPPED, next_delayed_time=None)))

        with patch("psa_car_controller.psacc.application.charge_control.logger") as log:
            self.charge_control().process()

        log.exception.assert_not_called()
        self.remote.change_charge_hour.assert_not_called()

    # the status is outdated

    def test_outdated_level_doesnt_stop_the_charge_but_wakes_the_car_up(self):
        # GIVEN the last status before the API failed: 85 %, from 10 h ago
        self.set_status(get_status(charging(), minutes_old=600), outdated=True)

        self.charge_control().process()

        self.assertEqual(0, self.stops())
        self.remote.wakeup.assert_called_once_with(VIN)

    def test_outdated_level_with_a_new_charge_from_mqtt_doesnt_stop_it(self):
        # GIVEN the car was stopped at 85 %, driven since, and mqtt reports it charging again
        self.set_status(get_status(charging(status=STOPPED), minutes_old=600), outdated=True)
        self.overlay_mqtt_charge()
        self.assertEqual(INPROGRESS, self.car.status.get_energy('Electric').charging.status)

        self.charge_control().process()

        self.assertEqual(0, self.stops())

    def test_outdated_level_doesnt_postpone_the_scheduled_charge(self):
        now = datetime.now().replace(hour=12, minute=0)
        self.set_status(get_status(charging(status=STOPPED, next_delayed_time="PT12H10M"), minutes_old=600),
                        outdated=True)

        with patch("psa_car_controller.psacc.application.charge_control.datetime") as mock_datetime:
            mock_datetime.now.return_value = now
            mock_datetime.utcnow.return_value = now
            self.charge_control().process()
            self.remote.change_charge_hour.assert_not_called()

            # while a recent status does
            self.set_status(get_status(charging(status=STOPPED, next_delayed_time="PT12H10M")))
            self.charge_control().process()
            self.remote.change_charge_hour.assert_called_once()

    def test_threshold_is_enforced_again_when_the_api_answers(self):
        self.set_status(get_status(charging(), minutes_old=600), outdated=True)
        cc = self.charge_control()
        cc.process()
        self.assertEqual(0, self.stops())

        self.set_status(get_status(charging()))
        cc.process()

        self.assertGreaterEqual(self.stops(), 1)

    def test_stop_hour_works_with_an_outdated_status(self):
        self.set_status(get_status(charging(), minutes_old=600), outdated=True)
        cc = self.charge_control(threshold=100, stop_due=True)

        cc.process()

        # sent once: an outdated status can't tell if it worked, so there is no second one
        self.assertEqual(1, self.stops())
        self.assertEqual(1, cc.retry_count)
        self.assertGreater(cc._next_stop_hour, datetime.now())

    def test_stop_hour_works_with_an_outdated_stopped_status(self):
        # GIVEN the delayed charge may have started since the API stopped answering
        self.set_status(get_status(charging(status=STOPPED), minutes_old=600), outdated=True)
        cc = self.charge_control(threshold=100, stop_due=True)

        for _ in range(3):
            cc.process()

        self.assertEqual(1, self.stops())

    def test_outdated_status_wakes_the_car_up_rarely(self):
        self.set_status(get_status(charging(), minutes_old=600), outdated=True)
        cc = self.charge_control()
        cc.process()
        self.remote.wakeup.assert_called_once()

        cc._last_wakeup -= timedelta(minutes=ChargeControl.OUTDATED_WAKEUP_TIMEOUT - 5)
        cc.process()
        self.remote.wakeup.assert_called_once()

        cc._last_wakeup -= timedelta(minutes=10)
        cc.process()
        self.assertEqual(2, self.remote.wakeup.call_count)

    def test_failed_status_request_after_the_stop_isnt_a_failed_stop(self):
        # GIVEN a recent status, the stop is sent and then the API stops answering
        self.set_status(get_status(charging()))
        cc = self.charge_control()

        def api_down(vin):
            self.car._status_failures = EMPTY_STATUS_LIMIT
            return self.car.status

        self.psacc.get_vehicle_info.side_effect = api_down
        cc.process()

        # THEN no second stop is sent blindly, one try is left for when the API answers
        self.assertEqual(1, self.stops())
        self.assertEqual(1, cc.retry_count)


if __name__ == '__main__':
    unittest.main()
