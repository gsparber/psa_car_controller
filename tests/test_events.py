"""The event log of the log page: what is recorded about status requests, MQTT, commands and user actions, that it
masks secrets and VINs, and the debug switch."""
import json
import logging
import unittest
from unittest.mock import MagicMock, patch

from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.reasoncodes import ReasonCode

from psa_car_controller.common import events, mylogger
from psa_car_controller.common.events import EventLog
from psa_car_controller.common.sensitive_filter import sensitive_filter
from psa_car_controller.psa.constants import DELAYED_CHARGE, IMMEDIATE_CHARGE
from psa_car_controller.psa.mqtt_event import describe_event
from psa_car_controller.psa.mqtt_request import MQTTRequest
from psa_car_controller.psa.remote_commands import describe_request
from psa_car_controller.psacc.model.car_status import status_summary
from tests.test_collector_api import get_client
from tests.test_health import connect, get_remote_client, on_disconnect
from tests.test_remote_command_ui import text
from tests.test_remote_commands import get_ready_remote_client
from tests.test_status_refresh import api_error, get_status
from tests.utils import get_web_view

VIN = "VR3UHZKXZLT000000"


def request(topic, parameters):
    return MQTTRequest(topic, VIN, parameters, "AP-customer")


class TestEventLog(unittest.TestCase):

    def test_newest_first_and_by_category(self):
        log = EventLog()
        log.record(events.API, "one")
        log.record(events.MQTT, "two")
        log.record(events.API, "three %s", "args")
        self.assertEqual(["three args", "two", "one"], [event.message for event in log.entries()])
        self.assertEqual(["three args", "one"], [event.message for event in log.entries([events.API])])

    def test_only_the_last_ones_are_kept(self):
        log = EventLog(size=3)
        for index in range(5):
            log.record(events.API, str(index))
        self.assertEqual(["4", "3", "2"], [event.message for event in log.entries()])

    def test_vin_and_secrets_are_masked(self):
        log = EventLog()
        sensitive_filter.add_source(lambda: ["very-secret-token-value"])
        self.addCleanup(sensitive_filter.clear_sources)
        event = log.record(events.API, "token very-secret-token-value of %s", VIN, vin=VIN)
        self.assertNotIn("very-secret-token-value", event.message)
        self.assertNotIn(VIN, event.message)
        self.assertNotIn(VIN, event.vin)
        self.assertTrue(event.vin.endswith(VIN[-4:]))

    def test_the_same_again_is_counted_on_the_last_event(self):
        log = EventLog()
        first = log.record(events.API, "Status: 66 %, level from 8 h ago", vin=VIN, same_as=("status", 66))
        log.record(events.MQTT, "Connected")  # another category in between doesn't count
        log.record(events.API, "Status: 66 %, level from 9 h ago", vin=VIN, same_as=("status", 66))
        last = log.record(events.API, "Status: 66 %, level from 10 h ago", vin=VIN, same_as=("status", 66))
        api = log.entries([events.API])
        self.assertEqual(1, len(api))
        self.assertEqual((3, first.at), (last.count, last.first_at))
        self.assertIn("10 h ago", api[0].message)
        # the newest first: the counted event moved up
        self.assertEqual(events.API, log.entries()[0].category)

    def test_something_new_is_a_new_event(self):
        log = EventLog()
        log.record(events.API, "Status: 66 %", vin=VIN, same_as=("status", 66))
        log.record(events.API, "Status: 65 %", vin=VIN, same_as=("status", 65))
        log.record(events.API, "Status: 66 %", vin=VIN, same_as=("status", 66))
        log.record(events.API, "Status: 66 %", vin="VR3UHZKXZLT111111", same_as=("status", 66))
        log.record(events.API, "pressed", vin=VIN)
        log.record(events.API, "pressed", vin=VIN)
        self.assertEqual(6, len(log.entries()))

    def test_as_text(self):
        log = EventLog()
        log.record(events.API, "one", vin=VIN, same_as=1)
        log.record(events.API, "one", vin=VIN, same_as=1)
        log.record(events.USER, "two", level=logging.WARNING)
        lines = events.as_text(log.entries()).splitlines()
        self.assertEqual(2, len(lines))
        self.assertRegex(lines[0], r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d – \d\d:\d\d:\d\d INFO \[PSA API\] \*\*\*0000: one "
                                   r"\(2x the same\)$")
        self.assertRegex(lines[1], r"WARNING \[User\] two$")
        self.assertNotIn(VIN, "\n".join(lines))

    def test_bad_format_is_kept(self):
        event = EventLog().record(events.API, "%d %s", "x")
        self.assertIn("x", event.message)

    def test_written_to_the_log_at_debug_level(self):
        with self.assertLogs("psa_car_controller.common.events", logging.DEBUG) as logs:
            EventLog().record(events.USER, "pressed", vin=VIN)
        self.assertEqual("DEBUG", logs.records[0].levelname)
        self.assertIn("[user]", logs.output[0])


class TestDescriptions(unittest.TestCase):

    def test_commands(self):
        self.assertEqual("Charge now", describe_request(request("/VehCharge", {"type": IMMEDIATE_CHARGE,
                                                                               "program": {"hour": 1, "minute": 0}})))
        self.assertEqual("Stop the charge, delayed charge at 22:05",
                         describe_request(request("/VehCharge", {"type": DELAYED_CHARGE,
                                                                 "program": {"hour": 22, "minute": 5}})))
        self.assertEqual("Stop the charge, delayed charge at ?",
                         describe_request(request("/VehCharge", {"type": DELAYED_CHARGE})))
        self.assertEqual("Wake-up", describe_request(request("/VehCharge/state", {"action": "state"})))
        self.assertEqual("Lock the doors", describe_request(request("/Doors", {"action": "lock"})))
        self.assertEqual("Start preconditioning", describe_request(request("/ThermalPrecond", {"asap": "activate"})))
        self.assertEqual("/Other", describe_request(request("/Other", None)))

    def test_mqtt_event(self):
        description = describe_event({"vin": VIN, "date": "2026-11-06T10:00:00Z", "obj_counter": 7,
                                      "charging_state": {"hmi_state": 1, "cable_detected": 0, "rate": 11,
                                                         "remaining_time": 60},
                                      "doors_state": {"doors_locking_state": 1}})
        for expected in ("charging InProgress (hmi_state 1)", "plugged yes (cable_detected 0)", "rate 11",
                         "remaining_time 60", "doors Locked", "10:00:00 UTC", "counter 7"):
            self.assertIn(expected, description)

    def test_unknown_remaining_time(self):
        description = describe_event({"vin": VIN, "charging_state": {"hmi_state": 3, "remaining_time": 4094}})
        self.assertIn("remaining_time unknown", description)
        self.assertNotIn("4094", description)

    def test_mqtt_event_without_values(self):
        description = describe_event({"vin": VIN})
        self.assertIn("plugged ?", description)
        self.assertIn("no date", description)

    def test_status(self):
        summary = status_summary(get_status("2026-11-06T10:00:00Z", level=63))
        self.assertIn("battery 63 %", summary)
        self.assertRegex(summary, r"level from \d+ (min|h|days) ago")


class TestRecorded(unittest.TestCase):

    def setUp(self):
        events.EVENTS.clear()

    def messages(self, category):
        return [event.message for event in events.EVENTS.entries([category])]

    def test_status_requests(self):
        client = get_client(get_status("2026-11-06T10:00:00Z", level=63), api_error(500), api_error(500),
                            api_error(429))
        client.get_vehicle_info(VIN)
        client.get_vehicle_info(VIN)
        client.get_vehicle_info(VIN)
        messages = self.messages(events.API)
        self.assertIn("Rate limit (HTTP 429)", messages[0])
        self.assertIn("HTTP 500", messages[1])
        self.assertIn("battery 63 %", messages[-1])
        # waiting for the limit and cached reads aren't events: a collector polls often
        client.get_vehicle_info(VIN)
        client.get_vehicle_info(VIN, cache=True)
        self.assertEqual(len(messages), len(self.messages(events.API)))

    def test_mqtt_connection(self):
        remote_client = get_remote_client()
        connect(remote_client)
        with patch.object(remote_client, "_refresh_remote_token"):
            on_disconnect(remote_client, ReasonCode(PacketTypes.DISCONNECT, "Unspecified error"))
        self.assertEqual(["Disconnected: Unspecified error", "Connected"], self.messages(events.MQTT))

    def test_commands_and_answers(self):
        remote_client = get_ready_remote_client()
        remote_client.mqtt_client = MagicMock()
        with patch.object(remote_client, "_refresh_remote_token", return_value=True):
            remote_client.lock_door(VIN, True)
        correlation_id = remote_client.commands.to_list(VIN)[-1]["correlation_id"]
        remote_client._on_command_answer({"correlation_id": correlation_id, "return_code": "901"})
        remote_client._on_command_answer({"correlation_id": correlation_id, "return_code": "0"})
        self.assertEqual(["Lock the doors: done", "Lock the doors: the server wakes the car up, waiting for its "
                          "answer", "Lock the doors: sent"], self.messages(events.COMMAND))

    def test_command_not_sent(self):
        remote_client = get_ready_remote_client()
        remote_client.mqtt_client = MagicMock()
        with patch.object(remote_client, "_refresh_remote_token", return_value=False):
            remote_client.lock_door(VIN, True)
        self.assertIn("not sent", self.messages(events.COMMAND)[0])
        self.assertEqual("ERROR", events.EVENTS.entries()[0].level)

    def test_vehicle_event(self):
        remote_client = get_ready_remote_client()
        payload = {"vin": VIN, "date": "2026-11-06T10:00:00Z", "obj_counter": 1,
                   "charging_state": {"hmi_state": 1, "cable_detected": 0}}
        remote_client._on_vehicle_event(payload)
        remote_client._on_vehicle_event(dict(payload))  # the same again isn't news
        self.assertEqual(1, len(self.messages(events.MQTT)))

    def test_user_on_the_api(self):
        api = get_web_view("api")
        client = MagicMock()
        client.remote_client.track_commands.return_value.__enter__.return_value = []
        client.remote_client.charge_now.return_value = True
        with patch.object(api, "APP", MagicMock(myp=client)):
            api.app.test_client().get(f"/charge_now/{VIN}/0")
        self.assertEqual(["REST API: stop charging"], self.messages(events.USER))


class TestLogPage(unittest.TestCase):

    def setUp(self):
        self.log = get_web_view("log")
        events.EVENTS.clear()
        self.addCleanup(mylogger.set_debug, False)

    def test_events_by_category(self):
        events.record(events.API, "status from PSA", vin=VIN)
        events.record(events.CHARGE_CONTROL, "Stop sent", vin=VIN, level=logging.WARNING)
        everything = text(self.log.render_events(self.log.ALL))
        self.assertIn("status from PSA", everything)
        self.assertIn("Stop sent", everything)
        self.assertNotIn(VIN, everything)
        only_api = text(self.log.render_events(events.API))
        self.assertNotIn("Stop sent", only_api)

    def test_empty(self):
        self.assertIn("Nothing happened", text(self.log.render_events(events.MQTT)))

    def test_view_switch(self):
        events.record(events.API, "status from PSA")
        content, category_hidden, level_hidden, _ = self.log.show_log(self.log.EVENTS_VIEW, self.log.ALL, "info", 0)
        self.assertIn("status from PSA", text(content))
        self.assertEqual((False, True), (category_hidden, level_hidden))
        with patch.object(self.log, "render", return_value="file"):
            content, category_hidden, level_hidden, _ = self.log.show_log(self.log.FILE_VIEW, self.log.ALL, "info", 0)
        self.assertEqual(("file", True, False), (content, category_hidden, level_hidden))

    def test_counted_event_shows_its_time_span(self):
        events.record(events.API, "Status: 66 %", vin=VIN, same_as=1)
        events.record(events.API, "Status: 66 %", vin=VIN, same_as=1)
        self.assertIn("2 times the same", text(self.log.render_events(self.log.ALL)))

    def test_download_follows_the_view(self):
        events.record(events.API, "status from PSA", vin=VIN)
        events.record(events.MQTT, "Connected")
        download = self.log.download_log(1, self.log.EVENTS_VIEW, events.API)
        self.assertTrue(download["filename"].startswith("psacc-events-"))
        self.assertIn("status from PSA", download["content"])
        self.assertNotIn("Connected", download["content"])
        self.assertIn("Connected", self.log.download_log(1, self.log.EVENTS_VIEW, self.log.ALL)["content"])
        with patch.object(self.log.dcc, "send_file", return_value="file") as send_file:
            self.assertEqual("file", self.log.download_log(1, self.log.FILE_VIEW, self.log.ALL))
        send_file.assert_called_once()

    def test_parked_car_is_one_event(self):
        client = get_client(*[get_status("2026-11-06T00:00:00Z", level=66) for _ in range(5)])
        for _ in range(5):
            client.get_vehicle_info(VIN)
        api = events.EVENTS.entries([events.API])
        self.assertEqual(1, len(api))
        self.assertEqual(5, api[0].count)

    def test_debug_switch(self):
        mylogger.my_logger(logging.INFO)
        self.log.switch_debug(True)
        self.assertTrue(mylogger.is_debug())
        self.assertEqual(logging.DEBUG, mylogger.file_handler.level)
        self.assertIn("Debug logging on", json.dumps([e.message for e in events.EVENTS.entries([events.USER])]))
        self.log.switch_debug(False)
        self.assertFalse(mylogger.is_debug())
        self.assertEqual(logging.INFO, mylogger.file_handler.level)


if __name__ == "__main__":
    unittest.main()
