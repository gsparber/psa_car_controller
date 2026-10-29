import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from psa_car_controller.psa.RemoteClient import MQTT_EVENT_TOPIC

from tests.utils import get_rc

VIN = "VR3UHZKX"


class FakeTimer:
    created = []

    def __init__(self, interval, function, args=None, kwargs=None):
        self.interval = interval
        self.function = function
        self.args = args or []
        self.kwargs = kwargs or {}
        self.daemon = False
        self.started = False
        self.cancelled = False
        FakeTimer.created.append(self)

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def join(self, timeout=None):
        pass

    def fire(self):
        if not self.cancelled:
            self.function(*self.args, **self.kwargs)


def event(charging_state, date=None):
    payload = {"vin": VIN, "charging_state": charging_state, "precond_state": {}}
    if date:
        payload["date"] = date
    return SimpleNamespace(topic=MQTT_EVENT_TOPIC + VIN, payload=json.dumps(payload).encode())


class TestFixNotUpdatedApi(unittest.TestCase):

    def setUp(self):
        FakeTimer.created = []
        self.charging = SimpleNamespace(status="Disconnected")
        car = MagicMock()
        self.status = car.status
        car.status.get_energy.return_value = SimpleNamespace(charging=self.charging)
        self.rc = get_rc()
        self.rc.vehicles_list = MagicMock()
        self.rc.vehicles_list.get_car_by_vin.return_value = car
        self.wakeup = MagicMock()
        self.rc.wakeup = self.wakeup
        timer_patcher = patch("psa_car_controller.psa.RemoteClient.threading.Timer", FakeTimer)
        timer_patcher.start()
        self.addCleanup(timer_patcher.stop)
        sleep_patcher = patch("time.sleep")
        self.sleep = sleep_patcher.start()
        self.addCleanup(sleep_patcher.stop)

    def test_mqtt_callback_does_not_block(self):
        # GIVEN a charging event while the api doesn't report the charge yet
        # WHEN the mqtt callback handles it
        self.rc._on_mqtt_message(None, None, event({"rate": 4, "remaining_time": 120}))

        # THEN the paho network thread isn't blocked: the delayed wakeup is scheduled on a timer
        self.sleep.assert_not_called()
        self.wakeup.assert_not_called()
        self.assertEqual(1, len(FakeTimer.created))
        self.assertEqual(60, FakeTimer.created[0].interval)
        self.assertTrue(FakeTimer.created[0].daemon)
        self.assertTrue(FakeTimer.created[0].started)

        FakeTimer.created[0].fire()
        self.wakeup.assert_called_once_with(VIN)

    def test_stale_remaining_time_without_charging_rate_is_ignored(self):
        # GIVEN an unplugged car which keeps reporting a stale remaining_time (seen on an e-2008)
        # WHEN it answers a wakeup
        self.rc._on_mqtt_message(None, None, event({"rate": 0, "remaining_time": 635}))

        # THEN no new wakeup is scheduled, otherwise every answer would trigger another wakeup
        self.assertEqual([], FakeTimer.created)
        self.sleep.assert_not_called()
        self.wakeup.assert_not_called()

    def test_event_older_than_api_status_is_ignored(self):
        # GIVEN an api status from 12:05 which reports that the car doesn't charge
        self.status._updated_at = datetime(2026, 10, 29, 12, 5, tzinfo=timezone.utc)

        # WHEN a charging event from 12:00 arrives late
        self.rc._on_mqtt_message(None, None, event({"rate": 4}, "2026-10-29T12:00:00Z"))

        # THEN the api isn't considered outdated: no wakeup is scheduled
        self.assertEqual([], FakeTimer.created)
        self.wakeup.assert_not_called()

    def test_event_newer_than_api_status_schedules_wakeup(self):
        self.status._updated_at = datetime(2026, 10, 29, 12, 5, tzinfo=timezone.utc)

        self.rc._on_mqtt_message(None, None, event({"rate": 4}, "2026-10-29T12:06:00Z"))

        self.assertEqual(1, len(FakeTimer.created))

    def test_unparsable_event_date_schedules_wakeup(self):
        self.status._updated_at = datetime(2026, 10, 29, 12, 5, tzinfo=timezone.utc)

        self.rc._on_mqtt_message(None, None, event({"rate": 4}, "29/10/2026 12:00"))

        self.assertEqual(1, len(FakeTimer.created))

    def test_repeated_events_schedule_one_wakeup_per_vin(self):
        for _ in range(5):
            self.rc._on_mqtt_message(None, None, event({"rate": 4}))
        self.assertEqual(1, len(FakeTimer.created))

        FakeTimer.created[0].fire()
        self.wakeup.assert_called_once_with(VIN)

        # a new event after the delayed check can schedule again
        self.rc._on_mqtt_message(None, None, event({"rate": 4}))
        self.assertEqual(2, len(FakeTimer.created))

    def test_delay_grows_while_the_api_does_not_catch_up(self):
        # GIVEN a car which answers every wakeup with a charging event while the api never
        # reports the charge (#1130)
        for _ in range(8):
            self.rc._on_mqtt_message(None, None, event({"rate": 4}))
            FakeTimer.created[-1].fire()

        # THEN the wakeups get rarer instead of following each other every minute
        self.assertEqual([60, 120, 240, 480, 960, 1800, 1800, 1800], [t.interval for t in FakeTimer.created])
        self.assertEqual(8, self.wakeup.call_count)

    def test_delay_is_reset_when_the_api_caught_up(self):
        for _ in range(3):
            self.rc._on_mqtt_message(None, None, event({"rate": 4}))
            FakeTimer.created[-1].fire()
        self.charging.status = "InProgress"
        self.rc._on_mqtt_message(None, None, event({"rate": 4}))  # ignored: the api reports the charge
        self.rc._delayed_api_update(VIN)
        self.charging.status = "Disconnected"

        self.rc._on_mqtt_message(None, None, event({"rate": 4}))

        self.assertEqual(60, FakeTimer.created[-1].interval)

    def test_delay_is_reset_when_the_charge_stops(self):
        for _ in range(3):
            self.rc._on_mqtt_message(None, None, event({"rate": 4}))
            FakeTimer.created[-1].fire()

        self.rc._on_mqtt_message(None, None, event({"rate": 0}))
        self.rc._on_mqtt_message(None, None, event({"rate": 4}))

        self.assertEqual(60, FakeTimer.created[-1].interval)

    def test_failed_wakeup_does_not_grow_the_delay(self):
        self.wakeup.return_value = False  # not sent, mqtt is disconnected
        for _ in range(2):
            self.rc._on_mqtt_message(None, None, event({"rate": 4}))
            FakeTimer.created[-1].fire()

        self.assertEqual([60, 60], [t.interval for t in FakeTimer.created])

    def test_no_wakeup_if_api_caught_up(self):
        self.rc._on_mqtt_message(None, None, event({"rate": 4}))
        self.charging.status = "InProgress"

        FakeTimer.created[0].fire()

        self.wakeup.assert_not_called()

    def test_stop_cancels_pending_wakeup(self):
        self.rc._on_mqtt_message(None, None, event({"rate": 4}))
        timer = FakeTimer.created[0]

        self.rc.stop()
        timer.fire()
        # a worker which was already running when stop() was called must not wake the car either
        self.rc._delayed_api_update(VIN)

        self.assertTrue(timer.cancelled)
        self.wakeup.assert_not_called()
        self.rc._on_mqtt_message(None, None, event({"rate": 4}))
        self.assertEqual(1, len(FakeTimer.created))


if __name__ == '__main__':
    unittest.main()
