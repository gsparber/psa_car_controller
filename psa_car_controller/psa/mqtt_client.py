import logging
import random
import socket
import sys

import paho.mqtt.client as mqtt

logger = logging.getLogger(__name__)

# 1460 (Ethernet MTU 1500 - IP/TCP headers) minus 4 bytes of headroom: the broker connection
# has been seen to stall on paths with a slightly smaller MTU (PPPoE, VPN, mobile).
TCP_MAX_SEGMENT_SIZE = 1456


def _set_max_segment_size(sock: socket.socket):
    # Only Linux clamps the MSS announced in the SYN to the value set before connect(). macOS
    # defines TCP_MAXSEG too, but only accepts values below the current MSS (512 before connect)
    # and returns EINVAL otherwise; Windows doesn't define it. The connection works without it,
    # so a rejected option is only logged.
    option = getattr(socket, "TCP_MAXSEG", None)
    if option is None or not sys.platform.startswith("linux"):
        return
    try:
        sock.setsockopt(socket.IPPROTO_TCP, option, TCP_MAX_SEGMENT_SIZE)
    except OSError as e:
        logger.debug("Can't set TCP_MAXSEG on MQTT socket: %s", e)


class MqttClient(mqtt.Client):
    """paho client that tries every broker address returned by DNS.

    mwa.mpsa.com resolves to several brokers, but the resolver keeps returning them in the same
    order until its cache expires, so paho kept reconnecting to the same unreachable one.
    TLS still uses the host name (paho wraps the socket with server_hostname=self._host).
    """

    def _create_socket_connection(self):
        if self._get_proxy():
            # the SOCKS proxy resolves the host itself
            return super()._create_socket_connection()

        addr_infos = socket.getaddrinfo(self._host, self._port, 0, socket.SOCK_STREAM)
        if not addr_infos:
            raise OSError("getaddrinfo returned no address for {}".format(self._host))
        random.shuffle(addr_infos)

        last_error = None
        for family, sock_type, proto, _, address in addr_infos:
            sock = None
            try:
                sock = socket.socket(family, sock_type, proto)
                _set_max_segment_size(sock)
                sock.settimeout(self._connect_timeout)
                if self._bind_address or self._bind_port:
                    sock.bind((self._bind_address, self._bind_port))
                logger.debug("Connecting to MQTT broker %s", address)
                sock.connect(address)
                return sock
            except OSError as e:
                logger.debug("Can't connect to MQTT broker %s: %s", address, e)
                last_error = e
                if sock is not None:
                    sock.close()
        raise last_error
