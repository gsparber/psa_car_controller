"""Status of the remote commands for the API and the UI (T34).

Before, every answer but 0/113/400 was only logged: a car which never woke up (901 then 300)
looked like a command still on its way, and nobody could ask what became of a command.
"""
import json
import threading
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

from psa_car_controller.psa.mqtt_request import MQTTRequest
from psa_car_controller.psa.remote_commands import RemoteCommands, PENDING_TIMEOUT, SENT, WOKEN_UP, SUCCESS, \
    FAILED, TIMEOUT, RETRIED, describe_error
from psa_car_controller.psacc.application.command_refresh import CommandStatusRefresh
from tests.test_remote_command_answers import answer, published, OTHER_VIN, CUSTOMER_ID
from tests.test_remote_commands import get_ready_remote_client, VIN


class FakeClock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


def request(vin=VIN, name="/Doors"):
    return MQTTRequest(name, vin, {"action": "lock"}, CUSTOMER_ID)


class TestCommandStates(unittest.TestCase):

    def setUp(self):
        self.clock = FakeClock()
        self.commands = RemoteCommands(clock=self.clock)

    def test_wakeup_forwarded_then_success(self):
        command = self.commands.add(request())
        self.commands.answer(command.correlation_id, "901")
        state = self.commands.get_dict(command.correlation_id)
        self.assertEqual((WOKEN_UP, False, True), (state["status"], state["final"], state["pending"]))

        self.commands.answer(command.correlation_id, "0")
        state = self.commands.get_dict(command.correlation_id)
        self.assertEqual((SUCCESS, True, False), (state["status"], state["final"], state["pending"]))
        self.assertIsNone(state["error"])

    def test_timeout_and_server_error_are_final(self):
        timeout = self.commands.add(request())
        error = self.commands.add(request(OTHER_VIN))
        self.commands.answer(timeout.correlation_id, "901")
        self.commands.answer(timeout.correlation_id, "300")
        self.commands.answer(error.correlation_id, "500", resp_data={"error": "remote service unavailable"})

        self.assertEqual(TIMEOUT, self.commands.get(timeout.correlation_id).status)
        failed = self.commands.get_dict(error.correlation_id)
        self.assertEqual((FAILED, "remote service unavailable"), (failed["status"], failed["error"]))
        self.assertEqual({"error": "remote service unavailable"}, failed["updates"][0]["resp_data"])

    def test_late_wakeup_answer_keeps_the_result(self):
        command = self.commands.add(request())
        self.commands.answer(command.correlation_id, "0")
        self.commands.answer(command.correlation_id, "901")
        self.assertEqual(SUCCESS, self.commands.get(command.correlation_id).status)

    def test_unknown_code_stays_pending_until_timeout(self):
        command = self.commands.add(request())
        self.commands.answer(command.correlation_id, "42")
        self.assertEqual(SENT, self.commands.get(command.correlation_id).status)
        self.assertEqual(command.correlation_id, self.commands.pending(VIN)["correlation_id"])

        self.clock.now += PENDING_TIMEOUT
        self.assertIsNone(self.commands.pending(VIN))
        self.assertFalse(self.commands.get_dict(command.correlation_id)["pending"])

    def test_pending_is_per_car_and_newest_first(self):
        first = self.commands.add(request())
        self.clock.now += 1
        second = self.commands.add(request(name="/Horn"))
        other = self.commands.add(request(OTHER_VIN))
        self.commands.answer(other.correlation_id, "0")

        self.assertEqual(second.correlation_id, self.commands.pending(VIN)["correlation_id"])
        self.assertIsNone(self.commands.pending(OTHER_VIN))
        self.commands.answer(second.correlation_id, "0")
        self.assertEqual(first.correlation_id, self.commands.pending(VIN)["correlation_id"])

    def test_unknown_id_has_no_dict(self):
        self.assertIsNone(self.commands.get_dict("unknown"))

    def test_describe_error(self):
        self.assertEqual("a reason", describe_error("a reason", {"error": "x"}))
        self.assertEqual("x", describe_error(None, {"message": "", "error": "x"}))
        self.assertEqual('{"other": 1}', describe_error(None, {"other": 1}))
        self.assertEqual("text", describe_error(None, "text"))
        self.assertIsNone(describe_error(None, {}))
        self.assertIsNone(describe_error(None, None))


class TestRemoteClientCommandStatus(unittest.TestCase):

    def setUp(self):
        self.rc = get_ready_remote_client()
        self.rc.remote_token_last_update = datetime(2000, 1, 1)

    def test_callbacks_get_every_answer(self):
        statuses = []
        self.rc.command_callbacks.append(lambda command: statuses.append(command.status))
        self.rc.horn(VIN, 2)
        [(_, horn)] = published(self.rc)

        self.rc._on_mqtt_message(None, None, answer(horn["correlation_id"], "901"))
        self.rc._on_mqtt_message(None, None, answer(horn["correlation_id"], "0"))
        self.rc._on_mqtt_message(None, None, answer("unknown", "0"))

        self.assertEqual([WOKEN_UP, SUCCESS], statuses)

    def test_failing_callback_doesnt_break_the_answer_handling(self):
        self.rc.command_callbacks.append(MagicMock(side_effect=ValueError))
        self.rc.horn(VIN, 2)
        [(_, horn)] = published(self.rc)
        self.rc.mqtt_client.publish.reset_mock()

        self.rc._on_mqtt_message(None, None, answer(horn["correlation_id"], "400"))

        self.assertEqual(RETRIED, self.rc.commands.get(horn["correlation_id"]).status)
        self.rc.mqtt_client.publish.assert_called_once()

    def test_resp_data_tells_why_it_failed(self):
        self.rc.horn(VIN, 2)
        [(_, horn)] = published(self.rc)
        msg = answer(horn["correlation_id"], "113")
        data = json.loads(msg.payload)
        data["resp_data"] = {"error": "doors open"}
        msg.payload = json.dumps(data).encode("utf-8")

        self.rc._on_mqtt_message(None, None, msg)

        self.assertEqual("doors open", self.rc.commands.get(horn["correlation_id"]).error)

    def test_track_commands_only_collects_this_thread(self):
        with self.rc.track_commands() as sent:
            self.rc.horn(VIN, 2)
            other = threading.Thread(target=self.rc.lights, args=(VIN, 10))
            other.start()
            other.join()
        self.rc.lock_door(VIN, True)

        horn, _lights, _doors = [message for _, message in published(self.rc)]
        self.assertEqual([horn["correlation_id"]], sent)


class TestCommandStatusRefresh(unittest.TestCase):

    def setUp(self):
        self.refreshed = threading.Event()
        self.refresh = MagicMock(side_effect=lambda vin: self.refreshed.set())
        self.rate_limited = False
        self.clock = FakeClock()
        self.scheduler = CommandStatusRefresh(self.refresh, lambda: self.rate_limited, delay=0, min_interval=60,
                                              clock=self.clock)

    def tearDown(self):
        self.scheduler.stop()

    def command(self, status, vin=VIN):
        command = RemoteCommands().add(request(vin))
        command.status = status
        return command

    def test_success_refreshes_the_status(self):
        self.scheduler.on_command(self.command(SUCCESS))
        self.assertTrue(self.refreshed.wait(2))
        self.refresh.assert_called_once_with(VIN)

    def test_other_answers_dont_refresh(self):
        for status in (WOKEN_UP, FAILED, TIMEOUT):
            self.scheduler.on_command(self.command(status))
        self.assertFalse(self.refreshed.wait(0.2))

    def test_no_refresh_while_rate_limited(self):
        self.rate_limited = True
        self.scheduler.on_command(self.command(SUCCESS))
        self.assertFalse(self.refreshed.wait(0.2))

    def test_refreshes_at_most_once_per_interval(self):
        with patch("psa_car_controller.psacc.application.command_refresh.threading.Timer") as timer:
            self.scheduler.on_command(self.command(SUCCESS))
            self.scheduler.on_command(self.command(SUCCESS))  # timer already planned
            self.assertEqual(1, timer.call_count)
            self.scheduler._run(VIN)
            self.clock.now += 20
            self.scheduler.on_command(self.command(SUCCESS))
        self.assertEqual(2, timer.call_count)
        self.assertEqual(40, timer.call_args.args[0])

    def test_stop_cancels_the_refresh(self):
        scheduler = CommandStatusRefresh(self.refresh, lambda: False, delay=0.2)
        scheduler.on_command(self.command(SUCCESS))
        scheduler.stop()
        self.assertFalse(self.refreshed.wait(0.4))


if __name__ == '__main__':
    unittest.main()
