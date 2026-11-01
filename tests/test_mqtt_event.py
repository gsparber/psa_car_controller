import json
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

from psa_car_controller.psa.RemoteClient import MQTT_EVENT_TOPIC
from psa_car_controller.psa.connected_car_api import ApiClient
from psa_car_controller.psa.mqtt_event import rest_charging_status
from psa_car_controller.psacc.model.car import Car, Cars
from tests.utils import get_rc

VIN = "VXKUKZKW0SW011711"
# the api status is from 12:00
REST_STATUS = {
    "_links": {"self": {"href": "https://api.groupe-psa.com/connectedcar/v4/user/vehicles/myid/status"}},
    "updatedAt": "2026-08-30T12:00:00Z",
    "energy": [{"createdAt": "2026-08-30T12:00:00Z", "updatedAt": "2026-08-30T12:00:00Z", "type": "Electric",
                "level": 53, "autonomy": 150,
                "charging": {"plugged": False, "status": "Disconnected", "chargingRate": 0}}],
    "doorsState": {"updatedAt": "2026-08-30T12:00:00Z", "lockedState": ["Locked"],
                   "opening": [{"identifier": "Driver", "state": "Closed"},
                               {"identifier": "Trunk", "state": "Closed"}]},
    "preconditioning": {"airConditioning": {"createdAt": "2026-08-30T12:00:00Z",
                                            "updatedAt": "2026-08-30T12:00:00Z", "status": "Disabled"}},
}
PROGRAMS = {"program1": {"hour": 7, "minute": 30, "on": 1, "day": [1, 1, 1, 1, 1, 0, 0]}}


def payload(date, counter, hmi_state=0, cable_detected=1, locking=1, opening=None, precond=0, soc=58, rate=0,
            programs=None):
    precond_state = {"available": 1, "status": precond, "asap": 0}
    if programs:
        precond_state["programs"] = programs
    return {"vin": VIN, "date": date, "obj_counter": counter, "reason": 4, "signal_quality": 5,
            "charging_state": {"hmi_state": hmi_state, "cable_detected": cable_detected, "soc_batt": soc,
                               "autonomy_zev": 200, "rate": rate, "remaining_time": 0},
            "precond_state": precond_state,
            "doors_state": {"doors_locking_state": locking, "doors_opening_state": opening or [0, 0, 0, 0, 0, 0, 0]}}


def rest_status(**changes):
    status = deepcopy(REST_STATUS)
    status.update(changes)
    return ApiClient()._ApiClient__deserialize(status, "Status")


class TestMqttEvents(unittest.TestCase):

    def setUp(self):
        self.rc = get_rc()
        self.car = Car(VIN, "vehicle_id", "Opel")
        self.car.status = rest_status()
        self.rc.vehicles_list = Cars([self.car])
        self.rc._store_precond_programs = lambda vin, programs: self.rc.precond_programs.__setitem__(vin, programs)
        timer_patcher = patch("psa_car_controller.psa.RemoteClient.threading.Timer")
        self.timer = timer_patcher.start()
        self.addCleanup(timer_patcher.stop)

    def send(self, data):
        msg = SimpleNamespace(topic=MQTT_EVENT_TOPIC + VIN, payload=json.dumps(data).encode())
        self.rc._on_mqtt_message(None, None, msg)

    def new_rest_status(self, **changes):
        # what PSAClient._set_status does with a new api status
        self.car.status = rest_status(**changes)
        self.rc.apply_mqtt_state(self.car)

    @property
    def charging(self):
        return self.car.status.get_energy("Electric").charging

    def test_first_event_leaves_the_api_values(self):
        self.send(payload("2026-08-30T13:00:00Z", 1, hmi_state=1, cable_detected=0, locking=3, precond=1))
        self.assertEqual("Disconnected", self.charging.status)
        self.assertEqual(["Locked"], self.car.status.doors_state.locked_state)
        self.assertEqual("Disabled", self.car.status.preconditionning.air_conditioning.status)

    def test_changes_newer_than_the_api_overwrite_it(self):
        self.send(payload("2026-08-30T13:00:00Z", 1))
        self.send(payload("2026-08-30T13:05:00Z", 2, hmi_state=1, cable_detected=0, locking=3,
                          opening=[0, 0, 0, 0, 1, 0, 0], precond=1, soc=61))
        self.assertEqual("InProgress", self.charging.status)
        self.assertTrue(self.charging.plugged)
        self.assertEqual(["Unlocked"], self.car.status.doors_state.locked_state)
        self.assertEqual(["Closed", "Open"], [opening.state for opening in self.car.status.doors_state.opening])
        self.assertEqual("Enabled", self.car.status.preconditionning.air_conditioning.status)

    def test_battery_level_and_range_stay_api_only(self):
        self.send(payload("2026-08-30T13:00:00Z", 1, soc=58))
        self.send(payload("2026-08-30T13:05:00Z", 2, soc=61, hmi_state=1))
        electric = self.car.status.get_energy("Electric")
        self.assertEqual(53, electric.level)
        self.assertEqual(150, electric.autonomy)

    def test_changes_older_than_the_api_are_ignored(self):
        self.send(payload("2026-08-30T11:00:00Z", 1))
        self.send(payload("2026-08-30T11:05:00Z", 2, hmi_state=1, locking=3))
        self.assertEqual("Disconnected", self.charging.status)
        self.assertEqual(["Locked"], self.car.status.doors_state.locked_state)

    def test_out_of_order_event_is_ignored(self):
        self.send(payload("2026-08-30T13:00:00Z", 1, locking=1))
        self.send(payload("2026-08-30T13:10:00Z", 3, locking=3, programs=PROGRAMS))
        self.assertEqual(["Unlocked"], self.car.status.doors_state.locked_state)
        # an older event delivered late must not undo the newer state
        self.send(payload("2026-08-30T13:05:00Z", 2, locking=1, rate=7,
                          programs={"program1": {"hour": 34, "minute": 7, "on": 0, "day": [0] * 7}}))
        self.assertEqual(["Unlocked"], self.car.status.doors_state.locked_state)
        self.assertEqual("Unlocked", self.rc.vehicle_events[VIN].lock_state)
        self.assertEqual(PROGRAMS, self.rc.precond_programs[VIN])
        # nor make a wakeup for a charge
        self.timer.assert_not_called()

    def test_same_date_orders_by_counter(self):
        self.send(payload("2026-08-30T13:00:00Z", 1, locking=1))
        self.send(payload("2026-08-30T13:00:00Z", 3, locking=3))
        self.send(payload("2026-08-30T13:00:00Z", 2, locking=1))
        self.assertEqual(["Unlocked"], self.car.status.doors_state.locked_state)

    def test_new_session_restarts_the_counter(self):
        self.send(payload("2026-08-30T13:00:00Z", 40, locking=1))
        self.send(payload("2026-08-30T14:00:00Z", 1, locking=3))
        self.assertEqual(["Unlocked"], self.car.status.doors_state.locked_state)

    def test_duplicate_event_with_a_new_date_does_not_make_old_values_new(self):
        self.send(payload("2026-08-30T11:00:00Z", 1, hmi_state=0))
        self.send(payload("2026-08-30T11:05:00Z", 2, hmi_state=1))
        # same state repeated later on: the charge started at 11:05, before the api status of 12:00
        self.send(payload("2026-08-30T13:00:00Z", 3, hmi_state=1))
        self.assertEqual("Disconnected", self.charging.status)
        self.assertEqual(("2026-08-30 11:05:00+00:00"), str(self.rc.vehicle_events[VIN].changed_at["charging"]))

    def test_stale_value_repeated_with_a_new_date_is_ignored(self):
        # only the battery level changes, which says nothing about the charging state
        self.send(payload("2026-08-30T11:00:00Z", 1, hmi_state=1, soc=58))
        self.send(payload("2026-08-30T13:00:00Z", 2, hmi_state=1, soc=59))
        self.assertEqual("Disconnected", self.charging.status)

    def test_newer_api_status_wins_again(self):
        self.send(payload("2026-08-30T13:00:00Z", 1, locking=1))
        self.send(payload("2026-08-30T13:05:00Z", 2, locking=3))
        self.assertEqual(["Unlocked"], self.car.status.doors_state.locked_state)
        self.new_rest_status(updatedAt="2026-08-30T14:00:00Z",
                             doorsState={"updatedAt": "2026-08-30T14:00:00Z", "lockedState": ["Locked"]})
        self.assertEqual(["Locked"], self.car.status.doors_state.locked_state)

    def test_overlay_survives_an_older_api_status(self):
        self.send(payload("2026-08-30T13:00:00Z", 1, hmi_state=0))
        self.send(payload("2026-08-30T13:05:00Z", 2, hmi_state=1))
        self.new_rest_status()
        self.assertEqual("InProgress", self.charging.status)

    def test_finer_api_lock_state_is_kept(self):
        self.car.status.doors_state.locked_state = ["Locked", "SuperLocked"]
        self.send(payload("2026-08-30T13:00:00Z", 1, locking=3))
        self.send(payload("2026-08-30T13:05:00Z", 2, locking=1))
        self.assertEqual(["Locked", "SuperLocked"], self.car.status.doors_state.locked_state)

    def test_transient_lock_state_is_not_applied(self):
        self.send(payload("2026-08-30T13:00:00Z", 1, locking=1))
        self.send(payload("2026-08-30T13:05:00Z", 2, locking=0))
        self.assertEqual(["Locked"], self.car.status.doors_state.locked_state)

    def test_car_without_api_lock_state_keeps_live_lock_changes(self):
        # some cars never report lockedState: mqtt is the only source, even for the first event
        self.new_rest_status(doorsState=None)
        self.send(payload("2026-08-30T11:00:00Z", 1, locking=3))
        self.assertEqual(["Unlocked"], self.car.status.doors_state.locked_state)
        self.send(payload("2026-08-30T11:05:00Z", 2, locking=7))
        self.send(payload("2026-08-30T11:05:03Z", 3, locking=1))
        self.assertEqual(["Locked"], self.car.status.doors_state.locked_state)
        # a new api status without lock state gets it back
        self.new_rest_status(doorsState={"updatedAt": "2026-08-30T12:00:00Z", "lockedState": []})
        self.assertEqual(["Locked"], self.car.status.doors_state.locked_state)
        # a late older event doesn't undo it
        self.send(payload("2026-08-30T11:00:01Z", 2, locking=3))
        self.assertEqual(["Locked"], self.car.status.doors_state.locked_state)

    def test_programs_of_the_first_event_are_stored(self):
        self.send(payload("2026-08-30T11:00:00Z", 1, programs=PROGRAMS))
        self.assertEqual(PROGRAMS, self.rc.precond_programs[VIN])

    def test_charge_seen_on_mqtt_still_wakes_up_the_api(self):
        # the overlay shows the charge, but the api still has to catch up with it
        self.send(payload("2026-08-30T13:00:00Z", 1, hmi_state=0))
        self.send(payload("2026-08-30T13:05:00Z", 2, hmi_state=1, rate=7))
        self.assertEqual("InProgress", self.charging.status)
        self.assertEqual("Disconnected", rest_charging_status(self.car.status))
        self.timer.assert_called_once_with(60, self.rc._delayed_api_update, args=[VIN])

    def test_duplicate_charging_event_still_checks_the_api(self):
        # cars answering every wakeup with the same charging event rely on it (#1130)
        self.send(payload("2026-08-30T13:00:00Z", 1, hmi_state=1, rate=7))
        self.rc._api_update_timers.clear()
        self.send(payload("2026-08-30T13:05:00Z", 2, hmi_state=1, rate=7))
        self.assertEqual(2, self.timer.call_count)

    def test_event_without_date_or_counter_is_taken(self):
        self.send(payload("2026-08-30T13:00:00Z", 5, locking=1))
        data = payload(None, None, locking=3)
        self.send(data)
        self.assertEqual("Unlocked", self.rc.vehicle_events[VIN].lock_state)

    def test_null_parts_do_not_raise(self):
        self.send({"vin": VIN, "date": "2026-08-30T13:00:00Z", "obj_counter": 1, "charging_state": {},
                   "precond_state": None, "doors_state": None})
        self.send({"vin": VIN, "date": "2026-08-30T13:01:00Z", "obj_counter": 2, "charging_state": {},
                   "precond_state": None, "doors_state": None, "sev_state": 1})
        self.assertEqual(["Locked"], self.car.status.doors_state.locked_state)


if __name__ == '__main__':
    unittest.main()
