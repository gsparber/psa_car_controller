import argparse
import atexit
import logging
import socket
import sys
import threading
from os import path
from typing import Optional

import psa_car_controller

from .charge_control import ChargeControls
from .charging import Charging
from .ha_mqtt import HomeAssistantMqtt
from psa_car_controller.common.sensitive_filter import sensitive_filter
from psa_car_controller.psacc.repository.config_repository import ConfigRepository
from psa_car_controller.psacc.repository.db import Database
from psa_car_controller.psacc.utils.utils import Singleton
from .psa_client import PSAClient
from psa_car_controller.common.mylogger import my_logger
from psa_car_controller.psa.oauth import refresh_can_recover
from psa_car_controller.psa.otp.otp import CONFIG_NAME as OTP_CONFIG_NAME, ConfigException
from psa_car_controller.psa.status_schema import enable_raw_status_log
from psa_car_controller.common.utils import RateLimitException
from psa_car_controller import __version__

DEFAULT_NAME = "config.json"

logger = logging.getLogger(__name__)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("-f", "--config", help="config file, default file: config.json", default="config.json")
    parser.add_argument("-c", "--charge-control", help="enable charge control, default charge_config.json",
                        const="charge_config.json", nargs='?', metavar='charge config file')
    parser.add_argument("-d", "--debug", help="enable debug", const=10, default=20, nargs='?',
                        metavar='Debug level number or name')
    parser.add_argument("-l", "--listen", help="change server listen address", default="127.0.0.1", metavar="IP")
    parser.add_argument("-p", "--port", help="change server listen port", default="5000")
    parser.add_argument("-r", "--record", help="save vehicle data to db", action='store_true')
    parser.add_argument("-R", "--refresh", help="refresh vehicles status every x min", type=int)
    parser.add_argument("--remote-disable", help="disable remote control", action='store_true')
    parser.add_argument("--offline", help="offline limited mode", action='store_true')
    parser.add_argument("--web-conf", help="ignore if config files not existing yet", action='store_true')
    parser.add_argument("-b", "--base-path", help="base path for web app", default="/")
    parser.add_argument("--log-raw-status", action='store_true',
                        help="log the raw vehicle status from the API once per vehicle (position and ids hidden)")
    parser.add_argument('--version', action='version', version='PSACC {}'.format(psa_car_controller.__version__))

    return parser.parse_args()


class PSACarController(metaclass=Singleton):
    CONNECTION_RETRY_DELAY = 60
    CONNECTION_RETRY_MAX_DELAY = 900

    def __init__(self):
        self.args = parse_args()
        self.myp: PSAClient
        self.chc: ChargeControls = None
        self.config_name = DEFAULT_NAME
        self.is_good: bool = True
        self.saving_config = False
        self.retry_timer: Optional[threading.Timer] = None
        self.offline = self.args.offline
        self.remote_control = not (self.args.remote_disable or self.offline)
        self.config = ConfigRepository.read_config()
        self.home_assistant: Optional[HomeAssistantMqtt] = None
        sensitive_filter.add_source(lambda: [self.config.Home_Assistant_MQTT.password])

    def start_remote_control(self):
        if self.args.remote_disable:
            logger.info("mqtt disabled")
        elif not self.args.web_conf or path.isfile(OTP_CONFIG_NAME):
            if self.myp.remote_client.mqtt_client is not None:
                self.myp.remote_client.stop()
            try:
                self.myp.remote_client.start()
                if self.args.charge_control:
                    chc = ChargeControls.load_config(self.myp, name=self.args.charge_control)
                    # called again after a login or an OTP config: the previous charge controls must stop
                    if self.chc is not None:
                        self.chc.stop()
                    self.chc = chc
                    self.chc.init()
                    self.myp.start_refresh_thread()
            except (socket.timeout, ConnectionResetError):
                logger.exception(
                    "Can't connect to mqtt broker your are not connected to internet or PSA MQTT server is "
                    "down !")
            except ConfigException as e:
                logger.error("start_remote_control failed redo otp config: %s", e)

    def load_app(self) -> bool:
        my_logger(handler_level=int(self.args.debug))
        enable_raw_status_log(getattr(self.args, "log_raw_status", False))

        logger.info("App version %s", __version__)
        if self.args.config:
            self.config_name = self.args.config
        if path.isfile(self.config_name):
            previous_client = getattr(self, "myp", None)
            self.myp = PSAClient.load_config(name=self.config_name)
            if previous_client is not None:
                # load_app runs again after a login: the replaced client would keep polling PSA, keep its
                # mqtt connection and could save its old tokens over the new ones on its next remote token refresh
                previous_client.stop()
        elif self.args.web_conf:
            logger.error("No config file")
            self.is_good = False
            return False
        else:
            raise FileNotFoundError(self.config_name)
        if not Database.check_db_access():
            sys.exit(1)
        self.myp.set_record(self.args.record)
        Charging.elec_price = self.config.Electricity_config
        if self.args.offline:
            logger.info("offline mode")
            self.is_good = True
        else:
            if self.args.refresh:
                # the chosen rate also applies while charging
                self.myp.info_refresh_rate = self.myp.charging_refresh_rate = self.args.refresh * 60
            self.is_good = self.myp.manager.refresh_token_now()
            if self.is_good:
                self.__start_services()
            elif not self.args.web_conf:
                logger.error("Connection need to be updated, Please redo authentication process.")
                raise ConnectionError
            elif self.__refresh_can_recover():
                self.__retry_connection(self.CONNECTION_RETRY_DELAY)
            else:
                logger.error("Please reconnect by going to config web page")
        self.start_home_assistant()
        if self.saving_config:
            self.save_config()
        else:
            # load_app runs again after a login: one saving loop is enough
            self.saving_config = True
            atexit.register(self.save_config)
            self.__save_config_periodically()
        return True

    def start_home_assistant(self):
        """Publish to Home Assistant through the user's MQTT broker, only when enabled in config.ini."""
        if getattr(self, "home_assistant", None) is not None:
            # load_app runs again after a login: a new one publishes the cars of the new PSA client
            self.home_assistant.stop()
            self.home_assistant = None
        ha_config = self.config.Home_Assistant_MQTT
        if not ha_config.is_enable():
            return
        self.home_assistant = HomeAssistantMqtt(ha_config, self.myp, lambda: self.chc,
                                                remote_enabled=self.remote_control)
        self.home_assistant.start()

    def __start_services(self):
        self.myp.manager.start_scheduled_refresh()
        logger.info(str(self.myp.get_vehicles()))
        if self.args.refresh:
            self.myp.start_refresh_thread()
        self.start_remote_control()

    def __refresh_can_recover(self) -> bool:
        # a network failure or an outage says nothing about the stored token, unlike a rejected one
        return refresh_can_recover(self.myp.manager.last_refresh_error)

    def __retry_connection(self, delay):
        logger.warning("Can't connect to the PSA server now, retrying to connect in %d s", delay)
        if self.retry_timer is not None:
            # load_app ran again meanwhile: keep a single retry loop
            self.retry_timer.cancel()
        self.retry_timer = threading.Timer(delay, self.__connect_again, [delay])
        self.retry_timer.daemon = True
        self.retry_timer.start()

    def __connect_again(self, delay):
        if self.is_good:  # already reconnected meanwhile, from the config web page for instance
            return
        next_delay = min(delay * 2, self.CONNECTION_RETRY_MAX_DELAY)
        try:
            self.is_good = self.myp.manager.refresh_token_now()
        except RateLimitException as e:
            logger.warning("Delaying the connection retry: %s", e)
            self.__retry_connection(next_delay)
            return
        if self.is_good:
            logger.info("Connection to the PSA server is back")
            self.__start_services()
        elif self.__refresh_can_recover():
            self.__retry_connection(next_delay)
        else:
            logger.error("Please reconnect by going to config web page")

    def save_config(self):
        self.myp.save_config(self.config_name)

    def __save_config_periodically(self):
        threading.Timer(30, self.__save_config_periodically).start()
        self.save_config()
