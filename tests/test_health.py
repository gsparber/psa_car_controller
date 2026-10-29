import unittest
from unittest.mock import MagicMock, patch

from paho.mqtt.client import ConnectFlags, DisconnectFlags, MQTTErrorCode, MQTTv311
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.reasoncodes import ReasonCode

from psa_car_controller.psa.RemoteClient import RemoteClient
from psa_car_controller.psa.mqtt_client import MqttClient
from psa_car_controller.psacc.application.health import get_health
from psa_car_controller.psacc.model.car import Cars

from tests.utils import get_rc

MAX_FAILURES = 5
CONNECTED = ReasonCode(PacketTypes.CONNACK, "Success")
BAD_CREDENTIALS = ReasonCode(PacketTypes.CONNACK, "Bad user name or password")
CONNECTION_LOST = ReasonCode(PacketTypes.DISCONNECT, "Unspecified error")


def get_remote_client() -> RemoteClient:
    remote_client = get_rc()
    remote_client.vehicles_list = Cars()
    remote_client.mqtt_client = MagicMock()
    remote_client.mqtt_client.is_connected.return_value = False
    return remote_client


def on_connect(remote_client: RemoteClient, reason_code: ReasonCode):
    remote_client._RemoteClient__on_mqtt_connect(MagicMock(), None, ConnectFlags(False), reason_code, None)


def on_disconnect(remote_client: RemoteClient, reason_code: ReasonCode):
    remote_client._on_mqtt_disconnect(None, None, DisconnectFlags(False), reason_code, None)


def refuse(remote_client: RemoteClient, times: int):
    # paho calls on_connect with the refused CONNACK, then on_disconnect with a generic reason code
    for _ in range(times):
        on_connect(remote_client, BAD_CREDENTIALS)
        on_disconnect(remote_client, CONNECTION_LOST)


def connect(remote_client: RemoteClient):
    remote_client.mqtt_client.is_connected.return_value = True
    on_connect(remote_client, CONNECTED)


class TestMqttAuthFailureCounter(unittest.TestCase):

    def test_refused_disconnects_are_counted(self):
        remote_client = get_remote_client()
        refuse(remote_client, 3)
        self.assertEqual(3, remote_client.mqtt_auth_failures)

    def test_paho_calls_the_callbacks(self):
        # the real client converts its error codes before calling on_connect/on_disconnect
        remote_client = get_remote_client()
        client = MqttClient(clean_session=True, protocol=MQTTv311)
        client.on_disconnect = remote_client._on_mqtt_disconnect
        with patch.object(remote_client, "_refresh_remote_token") as refresh:
            client._do_on_disconnect(packet_from_broker=False, v1_rc=MQTTErrorCode.MQTT_ERR_CONN_LOST)
        refresh.assert_called_once_with(force=True)

    def test_other_disconnects_are_not_counted(self):
        remote_client = get_remote_client()
        with patch.object(remote_client, "_refresh_remote_token") as refresh:
            on_disconnect(remote_client, CONNECTION_LOST)
        self.assertEqual(0, remote_client.mqtt_auth_failures)
        # the broker closes the socket without CONNACK when the token is expired
        refresh.assert_called_once_with(force=True)

    def test_refused_connection_does_not_refresh_the_token(self):
        remote_client = get_remote_client()
        with patch.object(remote_client, "_refresh_remote_token") as refresh:
            refuse(remote_client, 1)
        refresh.assert_not_called()

    def test_keepalive_timeout_does_not_refresh_the_token(self):
        remote_client = get_remote_client()
        with patch.object(remote_client, "_refresh_remote_token") as refresh:
            on_disconnect(remote_client, ReasonCode(PacketTypes.DISCONNECT, "Keep alive timeout"))
        refresh.assert_not_called()

    def test_successful_connect_resets_the_counter(self):
        remote_client = get_remote_client()
        refuse(remote_client, 3)
        connect(remote_client)
        self.assertEqual(0, remote_client.mqtt_auth_failures)
        self.assertIsNotNone(remote_client.mqtt_last_connect)

    def test_refused_connack_does_not_reset_the_counter(self):
        remote_client = get_remote_client()
        refuse(remote_client, 3)
        on_connect(remote_client, BAD_CREDENTIALS)
        self.assertEqual(4, remote_client.mqtt_auth_failures)
        self.assertIsNone(remote_client.mqtt_last_connect)

    def test_stop_resets_the_counter(self):
        remote_client = get_remote_client()
        refuse(remote_client, 3)
        remote_client.stop()
        self.assertEqual(0, remote_client.mqtt_auth_failures)

    def test_start_resets_the_counter(self):
        remote_client = get_remote_client()
        refuse(remote_client, 3)
        with patch.object(remote_client, "load_otp", return_value=False):
            remote_client.start()
        self.assertEqual(0, remote_client.mqtt_auth_failures)


class TestHealth(unittest.TestCase):

    def test_healthy_while_connected(self):
        remote_client = get_remote_client()
        connect(remote_client)
        body, status = get_health(remote_client, True, MAX_FAILURES)
        self.assertEqual(200, status)
        self.assertTrue(body["mqtt_connected"])
        self.assertEqual(0, body["consecutive_auth_failures"])
        self.assertIsNotNone(body["last_connect"])

    def test_healthy_below_the_threshold(self):
        remote_client = get_remote_client()
        refuse(remote_client, MAX_FAILURES - 1)
        _, status = get_health(remote_client, True, MAX_FAILURES)
        self.assertEqual(200, status)

    def test_unhealthy_after_max_refusals(self):
        remote_client = get_remote_client()
        refuse(remote_client, MAX_FAILURES)
        body, status = get_health(remote_client, True, MAX_FAILURES)
        self.assertEqual(503, status)
        self.assertFalse(body["mqtt_connected"])
        self.assertEqual(MAX_FAILURES, body["consecutive_auth_failures"])
        self.assertIsNone(body["last_connect"])

    def test_one_connect_makes_it_healthy_again(self):
        remote_client = get_remote_client()
        refuse(remote_client, MAX_FAILURES)
        connect(remote_client)
        _, status = get_health(remote_client, True, MAX_FAILURES)
        self.assertEqual(200, status)

    def test_healthy_when_remote_control_is_disabled(self):
        remote_client = get_remote_client()
        refuse(remote_client, MAX_FAILURES)
        body, status = get_health(remote_client, False, MAX_FAILURES)
        self.assertEqual(200, status)
        self.assertEqual({"api_authenticated": True, "remote_control": False}, body)

    def test_rejected_api_login_is_reported_but_stays_healthy(self):
        # a restart can't renew a rejected refresh token, only a new login can
        remote_client = get_remote_client()
        connect(remote_client)
        body, status = get_health(remote_client, True, MAX_FAILURES, api_authenticated=False)
        self.assertEqual(200, status)
        self.assertFalse(body["api_authenticated"])

    def test_healthy_without_client(self):
        _, status = get_health(None, True, MAX_FAILURES)
        self.assertEqual(200, status)


if __name__ == '__main__':
    unittest.main()
