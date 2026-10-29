import socket
import ssl
import unittest
from unittest.mock import MagicMock, patch

import paho.mqtt.client as mqtt

from psa_car_controller.psa import mqtt_client
from psa_car_controller.psa.mqtt_client import MqttClient, TCP_MAX_SEGMENT_SIZE

HOST = "mwa.mpsa.com"
ADDRESS_1 = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 8885))
ADDRESS_2 = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.2", 8885))


def no_shuffle(_):
    pass


class FakeSocket:
    def __init__(self, unreachable=(), maxseg_error=False):
        self.unreachable = unreachable
        self.maxseg_error = maxseg_error
        self.options = []
        self.connected_to = None
        self.closed = False
        self.bound_to = None

    def setsockopt(self, level, option, value):
        if self.maxseg_error:
            raise OSError(22, "Invalid argument")
        self.options.append((level, option, value))

    def settimeout(self, timeout):
        pass

    def bind(self, address):
        self.bound_to = address

    def connect(self, address):
        if address[0] in self.unreachable:
            raise ConnectionRefusedError(61, "Connection refused")
        self.connected_to = address

    def close(self):
        self.closed = True


class TestMqttClientConnection(unittest.TestCase):
    def setUp(self):
        self.client = MqttClient(clean_session=True, protocol=mqtt.MQTTv311)
        self.client._host = HOST
        self.client._port = 8885
        self.sockets = []

    def connect(self, addr_infos, **socket_kwargs):
        def new_socket(*_):
            sock = FakeSocket(**socket_kwargs)
            self.sockets.append(sock)
            return sock

        with patch.object(mqtt_client.socket, "getaddrinfo", return_value=list(addr_infos)) as getaddrinfo, \
                patch.object(mqtt_client.socket, "socket", side_effect=new_socket), \
                patch.object(mqtt_client.random, "shuffle", side_effect=no_shuffle):
            sock = self.client._create_socket_connection()
        getaddrinfo.assert_called_once_with(HOST, 8885, 0, socket.SOCK_STREAM)
        return sock

    def test_falls_back_to_next_broker_address(self):
        sock = self.connect([ADDRESS_1, ADDRESS_2], unreachable=("192.0.2.1",))
        self.assertEqual(2, len(self.sockets))
        self.assertTrue(self.sockets[0].closed)
        self.assertIs(self.sockets[1], sock)
        self.assertEqual(("192.0.2.2", 8885), sock.connected_to)
        self.assertFalse(sock.closed)
        self.assertIsNone(sock.bound_to)

    def test_raises_when_no_broker_is_reachable(self):
        with self.assertRaises(ConnectionRefusedError):
            self.connect([ADDRESS_1, ADDRESS_2], unreachable=("192.0.2.1", "192.0.2.2"))
        self.assertTrue(all(sock.closed for sock in self.sockets))

    def test_raises_without_address(self):
        with self.assertRaises(OSError):
            self.connect([])

    def test_shuffles_broker_addresses(self):
        with patch.object(mqtt_client.socket, "getaddrinfo", return_value=[ADDRESS_1, ADDRESS_2]), \
                patch.object(mqtt_client.socket, "socket", side_effect=lambda *_: FakeSocket()), \
                patch.object(mqtt_client.random, "shuffle", side_effect=lambda infos: infos.reverse()) as shuffle:
            sock = self.client._create_socket_connection()
        shuffle.assert_called_once()
        self.assertEqual(("192.0.2.2", 8885), sock.connected_to)

    @unittest.skipUnless(hasattr(socket, "TCP_MAXSEG"), "TCP_MAXSEG not supported by this OS")
    def test_sets_max_segment_size_on_linux(self):
        with patch.object(mqtt_client.sys, "platform", "linux"):
            sock = self.connect([ADDRESS_1])
        self.assertIn((socket.IPPROTO_TCP, socket.TCP_MAXSEG, TCP_MAX_SEGMENT_SIZE), sock.options)

    def test_skips_max_segment_size_on_macos(self):
        with patch.object(mqtt_client.sys, "platform", "darwin"):
            sock = self.connect([ADDRESS_1])
        self.assertEqual([], sock.options)

    @unittest.skipUnless(hasattr(socket, "TCP_MAXSEG"), "TCP_MAXSEG not supported by this OS")
    def test_ignores_rejected_max_segment_size(self):
        with patch.object(mqtt_client.sys, "platform", "linux"):
            sock = self.connect([ADDRESS_1], maxseg_error=True)
        self.assertEqual(("192.0.2.1", 8885), sock.connected_to)

    def test_without_tcp_maxseg_constant(self):
        with patch.object(mqtt_client, "socket", wraps=socket) as fake_socket_module, \
                patch.object(mqtt_client.sys, "platform", "linux"):
            del fake_socket_module.TCP_MAXSEG
            sock = FakeSocket()
            mqtt_client._set_max_segment_size(sock)
        self.assertEqual([], sock.options)

    def test_binds_configured_source_address(self):
        self.client._bind_address = "10.0.0.5"
        sock = self.connect([ADDRESS_1])
        self.assertEqual(("10.0.0.5", 0), sock.bound_to)

    def test_proxy_uses_paho_connection(self):
        with patch.object(self.client, "_get_proxy", return_value={"proxy_type": 2, "proxy_addr": "proxy"}), \
                patch.object(mqtt.Client, "_create_socket_connection", return_value="proxied") as parent, \
                patch.object(mqtt_client.socket, "getaddrinfo") as getaddrinfo:
            self.assertEqual("proxied", self.client._create_socket_connection())
        parent.assert_called_once()
        getaddrinfo.assert_not_called()

    def test_tls_keeps_host_name_for_sni(self):
        context = MagicMock(spec=ssl.SSLContext)
        context.check_hostname = True
        self.client.tls_set_context(context)
        self.client.on_socket_open = None
        sock = MagicMock()
        with patch.object(MqttClient, "_create_socket_connection", return_value=sock), \
                patch.object(self.client, "_send_connect", return_value=mqtt.MQTT_ERR_SUCCESS):
            self.client.connect(HOST, 8885, 60)
        self.assertEqual(HOST, context.wrap_socket.call_args.kwargs["server_hostname"])


if __name__ == "__main__":
    unittest.main()
