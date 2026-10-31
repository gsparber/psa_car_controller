import io
import logging
import re
import threading
from datetime import datetime, timezone, timedelta
from statistics import mean, StatisticsError

from configupdater import ConfigUpdater
from pydantic import BaseModel, ConfigDict, Field, GetCoreSchemaHandler, field_validator
from pydantic_core import core_schema
from typing import Any, List, Optional

from psa_car_controller.common import language
from psa_car_controller.common.utils import atomic_write
from psa_car_controller.psacc.application.battery_charge_curve import BatteryChargeCurve
from psa_car_controller.psacc.model.charge import Charge

logger = logging.getLogger(__name__)

_WRITE_LOCK = threading.Lock()

DEFAULT_CONFIG = """[General]
currency = €
# define format for data export, can be csv or xlsx
export_format = csv
# minimum trip length in km so it's added to stats and map in website
minimum trip length =
# km or mi: unit of distances, speeds and consumptions on the web pages, recorded data stays in km
length unit = km
# language of charts, dates and numbers on the web pages, e.g. de-AT; auto takes the one of the browser
language = auto
[Electricity config]
# price by kw/h
day price =
night price =
# ex: 22h30
night hour start =
# ex: 6h00
night hour end =
dc charge price =
high speed dc charge price =
# minimum power in kW that should be delivered during a charge so it can be considered as a high speed charger
high speed dc charge threshold =
charger efficiency =
[Home Assistant MQTT]
# publish the cars and their remote commands to Home Assistant through your own MQTT broker (MQTT discovery)
enabled = false
# your broker, e.g. 192.168.1.10 or core-mosquitto; nothing connects while it's empty
host =
port = 1883
username =
password =
# true to connect to the broker with TLS
tls = false
discovery prefix = homeassistant
base topic = psa_car_controller
[Login Worker]
# your own stelloauth worker, tried first at login, e.g. http://stelloauth:8080; empty: off
# your account email and password are sent to it: only use a service you host yourself, with https unless it's local
url =
"""


def utc_to_local(utc_dt):
    return utc_dt.replace(tzinfo=timezone.utc).astimezone(tz=None)


def replace_key_underscore_by_space(obj, key):
    new_obj = {}
    for k, v in obj.items():
        if isinstance(v, dict):
            new_obj[k.replace("_", " ")] = replace_key_underscore_by_space(v, key)
        else:
            new_obj[k.replace("_", " ")] = v
    return new_obj


HOUR_REGEX = re.compile(r"([0-9]{1,2})h([0-9]{1,2})")


class Hour(str):
    hours: int
    minutes: int

    @classmethod
    def __get_pydantic_core_schema__(cls, _source_type: Any, _handler: GetCoreSchemaHandler):
        return core_schema.no_info_plain_validator_function(
            cls.validate, serialization=core_schema.plain_serializer_function_ser_schema(str))

    @classmethod
    def validate(cls, v):
        if not isinstance(v, str):
            raise ValueError('string required')
        if len(v) == 0:
            return None

        m = HOUR_REGEX.fullmatch(v.lower())
        if not m:
            raise ValueError('invalid hour format')
        hour = cls(v)
        hour.hours = int(m.group(1))
        hour.minutes = int(m.group(2))
        return hour

    def __repr__(self):
        return '{}h{}'.format(self.hours, self.minutes)


currency = ""
CONFIG_FILENAME = "config.ini"


class SectionConfig(BaseModel):
    # the web settings assign values one by one, numbers typed in text fields (e.g. a numeric password)
    # arrive as numbers: check and convert each value right away, an invalid one never reaches the config
    model_config = ConfigDict(coerce_numbers_to_str=True, validate_assignment=True)


class GeneralConfig(SectionConfig):
    currency: str = "€"
    length_unit: str = "km"
    language: str = language.AUTO
    minimum_trip_length: float = 10
    export_format: str = "csv"

    @field_validator("language")
    @classmethod
    def known_language(cls, value):
        return language.normalize(value)


class ElectricityPriceConfig(SectionConfig):
    day_price: Optional[float] = 0.15
    night_price: Optional[float] = None
    night_hour_start: Optional[Hour] = None
    night_hour_end: Optional[Hour] = None
    dc_charge_price: Optional[float] = None
    high_speed_dc_charge_price: Optional[float] = None
    high_speed_dc_charge_threshold: Optional[float] = None
    charger_efficiency: float = 0.8942

    @staticmethod
    def compare_hour(date: datetime, hour, minute):
        if date.hour < hour:
            return False
        if date.hour == hour and date.minute < minute:
            return False
        return True

    def is_nigh_hour_enabled(self):
        return self.night_hour_start and self.night_hour_end and self.night_price

    def get_instant_price(self, date):
        local_date = utc_to_local(date)
        if self.night_price is None:
            return self.day_price
        if self.compare_hour(local_date, self.night_hour_start.hours, self.night_hour_start.minutes) or \
                not self.compare_hour(local_date, self.night_hour_end.hours, self.night_hour_end.minutes):
            return self.night_price
        return self.day_price

    def _get_dc_charge_price(self, charge: Charge, battery_charge_curves: List[BatteryChargeCurve]):
        max_consumption = sum(battery_charge_curve.speed for battery_charge_curve in battery_charge_curves)
        total_consumption = charge.kw
        if self.high_speed_dc_charge_threshold and max_consumption > self.high_speed_dc_charge_threshold:
            return self.high_speed_dc_charge_price * total_consumption
        return self.dc_charge_price * total_consumption

    def _get_ac_charge_price(self, start, end, consumption):
        prices = []
        date = start
        res = None
        if not (start is None or end is None):
            while date < end:
                prices.append(self.get_instant_price(date))
                date = date + timedelta(minutes=30)
            try:
                res = round(consumption * mean(prices) / self.charger_efficiency, 2)
            except StatisticsError as e:
                logger.error("Can't get_price of charge: %s-%s prices: %s", start, end, prices, exc_info=e)
            except TypeError as e:
                logger.error("Can't get_price of charge, check config", exc_info=e)
        return res

    def get_price(self, charge: Charge, battery_charge_curves: List[BatteryChargeCurve]):
        if charge.charging_mode.DC and self.dc_charge_price:
            return self._get_dc_charge_price(charge, battery_charge_curves)
        return self._get_ac_charge_price(charge.start_at, charge.stop_at, charge.kw)

    def is_enable(self):
        return self.day_price is not None


class HomeAssistantMqttConfig(SectionConfig):
    enabled: bool = False
    host: Optional[str] = None
    port: int = 1883
    username: Optional[str] = None
    password: Optional[str] = Field(default=None, repr=False)  # a logged config or error must not show it
    tls: bool = False
    discovery_prefix: str = "homeassistant"
    base_topic: str = "psa_car_controller"

    def is_enable(self) -> bool:
        return bool(self.enabled and self.host)


class LoginWorkerConfig(SectionConfig):
    url: Optional[str] = None


class ConfigRepository(BaseModel):
    General: GeneralConfig
    Electricity_config: ElectricityPriceConfig
    Home_Assistant_MQTT: HomeAssistantMqttConfig = Field(default_factory=HomeAssistantMqttConfig)
    # the credentials go to this URL: the name keeps it out of reach of /settings/<section>, tests check it
    Login_Worker: LoginWorkerConfig = Field(default_factory=LoginWorkerConfig)

    @staticmethod
    def _read_file(name):
        if name is None:
            name = CONFIG_FILENAME
        with open(name, "r", encoding="utf-8") as f:
            return f.read()

    @staticmethod
    def read_config(name=None) -> 'ConfigRepository':
        try:
            config_str = ConfigRepository._read_file(name)
            config = ConfigUpdater(allow_no_value=True)
            config.read_string(config_str)
            return ConfigRepository.config_file_to_dto(config)
        except FileNotFoundError:
            config = ConfigRepository.config_file_to_dto(ConfigRepository.get_default_config())
            config.write_config()
            return config

    @staticmethod
    def get_default_config():
        config = ConfigUpdater(allow_no_value=True)
        config.read_string(DEFAULT_CONFIG)
        return config

    def config_dto_to_config_file(self, config):
        res = replace_key_underscore_by_space(self.model_dump(), None)
        for section, option in res.items():
            for key, value in option.items():
                config[section][key] = value

        return config

    def write_config(self, name=None):
        if name is None:
            name = CONFIG_FILENAME
        with _WRITE_LOCK:  # the last change wins, not the slower request
            ConfigRepository(**self.model_dump())  # validate property before write
            config_to_write = ConfigRepository.get_default_config()
            self.config_dto_to_config_file(config_to_write)
            self._write(name, config_to_write)

    @staticmethod
    def _write(name, config):
        content = io.StringIO()
        config.write(content)
        atomic_write(name, content.getvalue().encode("utf-8"))

    @staticmethod
    def config_file_to_dto(config) -> 'ConfigRepository':
        new_dict = {}
        for section in config:
            new_section = section.replace(" ", "_")
            new_dict[new_section] = {}
            for option in config[section]:
                new_option = option.replace(" ", "_")
                value = config[section][option].value
                if value and len(value) > 0:
                    new_dict[new_section][new_option] = value

        config_obj = ConfigRepository(**new_dict)
        return config_obj
