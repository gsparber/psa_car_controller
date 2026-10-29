import json
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

from paho.mqtt.client import MQTTMessage

from psa_car_controller.psa.RemoteClient import MQTT_EVENT_TOPIC, RemoteException
from psa_car_controller.psacc.model.car import Car, Cars
from tests.test_refresh_remote_token import get_remote_client

VIN = "VR3UHZKXZLT000000"


def get_command_remote_client():
    remote_client = get_remote_client()
    remote_client.account_info.get_mqtt_customer_id.return_value = "AP-ACNT200000000000"
    return remote_client


def get_ready_remote_client(car=None):
    remote_client = get_command_remote_client()
    remote_client.vehicles_list = Cars([car or Car(VIN, "vid", "Peugeot")])
    remote_client.vehicles_list.save_cars = MagicMock()
    remote_client.remoteCredentials.access_token = "access_token"
    remote_client.remoteCredentials.last_update = datetime.now()
    return remote_client


class TestRemoteCommands(unittest.TestCase):

    def test_horn_publishes_topic_and_message(self):
        # GIVEN a remote client with a valid remote token
        remote_client = get_ready_remote_client()

        # WHEN the horn is requested (regression #1199: the MQTTRequest object was passed as topic)
        res = remote_client.horn(VIN, 2)

        # THEN a str topic and a json message are published
        self.assertTrue(res)
        remote_client.mqtt_client.publish.assert_called_once()
        topic, message = remote_client.mqtt_client.publish.call_args.args
        self.assertIsInstance(topic, str)
        self.assertTrue(topic.endswith("/Horn"))
        self.assertEqual({"nb_horn": 2, "action": "activate"}, json.loads(message)["req_parameters"])

    def test_lights_publishes_topic_and_message(self):
        remote_client = get_ready_remote_client()

        res = remote_client.lights(VIN, 10)

        self.assertTrue(res)
        topic, message = remote_client.mqtt_client.publish.call_args.args
        self.assertTrue(topic.endswith("/Lights"))
        self.assertEqual({"action": "activate", "duration": 10}, json.loads(message)["req_parameters"])

    def test_horn_returns_false_when_remote_token_refresh_fails(self):
        # GIVEN a remote client whose token can't be refreshed
        remote_client = get_command_remote_client()
        remote_client.manager.refresh_token_now.return_value = False

        # WHEN the horn is requested
        res = remote_client.horn(VIN, 1)

        # THEN nothing is published and the failure is reported
        self.assertFalse(res)
        remote_client.mqtt_client.publish.assert_not_called()


PROGRAMS = {
    "program1": {"day": [1, 1, 1, 1, 1, 0, 0], "hour": 7, "minute": 15, "on": 1},
    "program2": {"day": [0, 0, 0, 0, 0, 0, 0], "hour": 34, "minute": 7, "on": 0},
    "program3": {"day": [0, 0, 0, 0, 0, 0, 0], "hour": 34, "minute": 7, "on": 0},
    "program4": {"day": [0, 0, 0, 0, 0, 0, 0], "hour": 34, "minute": 7, "on": 0},
}


def published(remote_client):
    """(topic suffix, req_parameters) of every published mqtt message."""
    return [(topic.rsplit("/", 1)[-1], json.loads(message)["req_parameters"])
            for topic, message in (call.args for call in remote_client.mqtt_client.publish.call_args_list)]


class TestPreconditioningPrograms(unittest.TestCase):

    def test_unknown_programs_are_never_overwritten(self):
        # GIVEN a car whose programs were never reported (fresh start, no mqtt event yet)
        remote_client = get_ready_remote_client()

        # WHEN preconditioning is requested (regression #1152: the default programs were sent)
        with self.assertRaises(RemoteException):
            remote_client.preconditioning(VIN, True)

        # THEN no ThermalPrecond command is sent, the car is only woken up to report its programs
        self.assertEqual([("state", {"action": "state"})], published(remote_client))

    def test_programs_from_mqtt_event_are_sent_and_saved(self):
        remote_client = get_ready_remote_client()
        msg = MQTTMessage(topic=(MQTT_EVENT_TOPIC + VIN).encode("utf-8"))
        msg.payload = json.dumps({"vin": VIN, "charging_state": {}, "precond_state": {"programs": PROGRAMS}})
        remote_client._on_mqtt_message(None, None, msg)

        remote_client.preconditioning(VIN, True)

        self.assertEqual([("ThermalPrecond", {"asap": "activate", "programs": PROGRAMS})], published(remote_client))
        self.assertEqual(PROGRAMS, remote_client.vehicles_list[0].precond_programs)
        remote_client.vehicles_list.save_cars.assert_called_once()

    def test_saved_programs_survive_restart(self):
        # GIVEN programs saved in cars.json by a previous run
        car = Car.from_json(Car(VIN, "vid", "Peugeot", precond_programs=PROGRAMS).to_dict())
        remote_client = get_ready_remote_client(car)

        remote_client.preconditioning(VIN, False)

        self.assertEqual([("ThermalPrecond", {"asap": "deactivate", "programs": PROGRAMS})],
                         published(remote_client))

    def test_programs_from_rest_status(self):
        # GIVEN no mqtt event yet, but the REST status lists the programs
        car = Car(VIN, "vid", "Peugeot")
        status_programs = [{"slot": 1, "enabled": True, "start": "PT7H15M",
                            "occurence": {"day": ["Mon", "Tue", "Wed", "Thu", "Fri"]}}]
        car._status = SimpleNamespace(preconditionning=SimpleNamespace(
            air_conditioning=SimpleNamespace(programs=status_programs)))
        remote_client = get_ready_remote_client(car)

        remote_client.preconditioning(VIN, True)

        self.assertEqual([("ThermalPrecond", {"asap": "activate", "programs": PROGRAMS})], published(remote_client))


if __name__ == '__main__':
    unittest.main()
