import json
import unittest
from datetime import datetime

from paho.mqtt.client import MQTTMessage

from psa_car_controller.psa.RemoteClient import MQTT_RESP_TOPIC
from psa_car_controller.psa.remote_commands import RemoteCommands, REASON_NOT_COMPATIBLE, SENT, SUCCESS, \
    FAILED, NOT_COMPATIBLE, RETRIED
from psa_car_controller.psa.mqtt_request import MQTTRequest
from tests.test_remote_commands import get_ready_remote_client, VIN

OTHER_VIN = "VR3UHZKXZLT000001"
CUSTOMER_ID = "AP-ACNT200000000000"


def answer(correlation_id, return_code, vin=VIN, reason=None):
    msg = MQTTMessage(topic=(MQTT_RESP_TOPIC + CUSTOMER_ID + "/VehCharge").encode("utf-8"))
    data = {"vin": vin, "correlation_id": correlation_id, "return_code": return_code}
    if reason is not None:
        data["reason"] = reason
    msg.payload = json.dumps(data).encode("utf-8")
    return msg


def published(remote_client):
    """(topic, message) of each mqtt publish."""
    return [(call.args[0], json.loads(call.args[1])) for call in remote_client.mqtt_client.publish.call_args_list]


class TestCommandAnswers(unittest.TestCase):

    def setUp(self):
        self.rc = get_ready_remote_client()
        # a forced refresh right before the retry must not be throttled away from the test
        self.rc.remote_token_last_update = datetime(2000, 1, 1)

    def send_horn_and_lights(self):
        self.rc.horn(VIN, 2)
        self.rc.lights(OTHER_VIN, 10)
        (_, horn), (_, lights) = published(self.rc)
        self.rc.mqtt_client.publish.reset_mock()
        return horn["correlation_id"], lights["correlation_id"]

    def test_expired_token_resends_the_refused_command_not_the_last_one(self):
        # GIVEN two commands sent to two cars, the horn first
        horn_id, _ = self.send_horn_and_lights()

        # WHEN the horn is refused because the remote token expired
        self.rc._on_mqtt_message(None, None, answer(horn_id, "400"))

        # THEN the horn is sent again (the global last_request resent the lights)
        [(topic, message)] = published(self.rc)
        self.assertTrue(topic.endswith("/Horn"))
        self.assertEqual(VIN, message["vin"])
        self.assertNotEqual(horn_id, message["correlation_id"])
        self.assertEqual(RETRIED, self.rc.commands.get(horn_id).status)
        self.assertEqual(message["correlation_id"], self.rc.commands.get(horn_id).retried_as)
        self.assertTrue(self.rc.commands.get(message["correlation_id"]).retried)

    def test_each_refused_command_is_resent(self):
        # GIVEN two commands both sent with the expired token
        horn_id, lights_id = self.send_horn_and_lights()

        # WHEN both are refused
        self.rc._on_mqtt_message(None, None, answer(horn_id, "400"))
        self.rc._on_mqtt_message(None, None, answer(lights_id, "400", vin=OTHER_VIN))

        # THEN both are sent again (the old code only resent one and dropped the other)
        topics = [topic for topic, _ in published(self.rc)]
        self.assertEqual(2, len(topics))
        self.assertTrue(topics[0].endswith("/Horn"))
        self.assertTrue(topics[1].endswith("/Lights"))

    def test_retried_command_is_not_resent_again(self):
        horn_id, _ = self.send_horn_and_lights()
        self.rc._on_mqtt_message(None, None, answer(horn_id, "400"))
        [(_, retry)] = published(self.rc)
        self.rc.mqtt_client.publish.reset_mock()

        # WHEN the retry is refused too
        self.rc._on_mqtt_message(None, None, answer(retry["correlation_id"], "400"))

        # THEN it gives up
        self.rc.mqtt_client.publish.assert_not_called()
        self.assertEqual(FAILED, self.rc.commands.get(retry["correlation_id"]).status)

    def test_not_compatible_is_not_retried(self):
        horn_id, _ = self.send_horn_and_lights()

        self.rc._on_mqtt_message(None, None, answer(horn_id, "400", reason=REASON_NOT_COMPATIBLE))

        self.rc.mqtt_client.publish.assert_not_called()
        self.rc.manager.refresh_token_now.assert_not_called()
        self.assertEqual(NOT_COMPATIBLE, self.rc.commands.get(horn_id).status)

    def test_unknown_command_is_not_resent(self):
        self.send_horn_and_lights()

        self.rc._on_mqtt_message(None, None, answer("unknown", "400"))

        self.rc.mqtt_client.publish.assert_not_called()

    def test_success_and_failure_codes(self):
        horn_id, lights_id = self.send_horn_and_lights()

        self.rc._on_mqtt_message(None, None, answer(horn_id, "901"))
        self.assertEqual(SENT, self.rc.commands.get(horn_id).status)
        self.rc._on_mqtt_message(None, None, answer(horn_id, "0"))
        self.rc._on_mqtt_message(None, None, answer(lights_id, "113", vin=OTHER_VIN))

        horn = self.rc.commands.get(horn_id)
        self.assertEqual(SUCCESS, horn.status)
        self.assertEqual(["901", "0"], [update["return_code"] for update in horn.updates])
        self.assertEqual(FAILED, self.rc.commands.get(lights_id).status)
        self.rc.mqtt_client.publish.assert_not_called()

    def test_command_is_not_registered_when_not_published(self):
        self.rc.manager.refresh_token_now.return_value = False
        self.rc.remoteCredentials.last_update = None

        self.assertFalse(self.rc.horn(VIN, 2))

        self.assertEqual([], self.rc.commands.to_list())


class TestRemoteCommands(unittest.TestCase):

    def setUp(self):
        self.now = 1000.0
        self.commands = RemoteCommands(ttl=60, max_commands=3, clock=lambda: self.now)

    def request(self, vin=VIN):
        return MQTTRequest("/Horn", vin, {}, CUSTOMER_ID)

    def test_correlation_id_is_stable_across_messages(self):
        request = self.request()
        first = request.get_message("token")["correlation_id"]

        self.assertEqual(first, request.get_message("token")["correlation_id"])
        self.assertEqual(first, request.correlation_id)

    def test_expired_commands_are_pruned(self):
        old = self.commands.add(self.request())
        self.now += 61
        new = self.commands.add(self.request())

        self.assertIsNone(self.commands.get(old.correlation_id))
        self.assertIs(new, self.commands.get(new.correlation_id))
        self.assertIsNone(self.commands.answer(old.correlation_id, "0"))

    def test_oldest_commands_are_dropped_past_max(self):
        ids = [self.commands.add(self.request()).correlation_id for _ in range(4)]

        self.assertEqual(ids[1:], [command["correlation_id"] for command in self.commands.to_list()])

    def test_to_list_by_vin(self):
        self.commands.add(self.request())
        other = self.commands.add(self.request(OTHER_VIN))

        [command] = self.commands.to_list(OTHER_VIN)
        self.assertEqual(other.correlation_id, command["correlation_id"])
        self.assertEqual("/Horn", command["name"])
        self.assertEqual(SENT, command["status"])


if __name__ == '__main__':
    unittest.main()
