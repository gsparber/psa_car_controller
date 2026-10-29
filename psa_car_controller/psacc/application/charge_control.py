import json
import logging
import threading
from copy import copy
from datetime import datetime, timedelta
from hashlib import md5
from time import sleep

import pytz

from psa_car_controller.psa.constants import DISCONNECTED, INPROGRESS, FINISHED, STOPPED
from psa_car_controller.common.utils import RateLimitException, parse_hour
from .psa_client import PSAClient

logger = logging.getLogger(__name__)


class ChargeControl:
    MQTT_TIMEOUT = 60
    # most minutes between two wakeups of a charging car (wakeup_timeout is the fewest): far from
    # the threshold they get rarer (#1130: a slow charge doesn't update the api for a long time,
    # and waking the car up every refresh only drains its 12V battery)
    MAX_WAKEUP_TIMEOUT = 60

    def __init__(self, psacc: PSAClient, vin, percentage_threshold, stop_hour):
        self.vin = vin
        self.percentage_threshold = percentage_threshold
        self.set_stop_hour(stop_hour)
        self.psacc = psacc
        self.retry_count = 0
        self.wakeup_timeout = 10
        self._last_wakeup = None

    def set_stop_hour(self, stop_hour):
        if stop_hour is None or stop_hour == [0, 0]:
            self._stop_hour = None
            self._next_stop_hour = None
        else:
            self._stop_hour = stop_hour
            self._next_stop_hour = datetime.now().replace(hour=stop_hour[0], minute=stop_hour[1], second=0)
            if self._next_stop_hour < datetime.now():
                self._next_stop_hour += timedelta(days=1)

    def get_stop_hour(self):
        return self._stop_hour

    def control_charge_with_ack(self, charge: bool):
        self.psacc.remote_client.charge_now(self.vin, charge)
        self.retry_count += 1
        sleep(ChargeControl.MQTT_TIMEOUT)
        vehicle_status = self.psacc.get_vehicle_info(self.vin)
        status = vehicle_status.get_energy('Electric').charging.status
        if status in (FINISHED, DISCONNECTED):
            logger.warning("Car state isn't compatible with charging %s", status)
        if (status == INPROGRESS) != charge:
            logger.warning("retry to control the charge of %s", self.vin)
            self.psacc.remote_client.charge_now(self.vin, charge)
            self.retry_count += 1
            return False
        self.retry_count = 0
        return True

    def force_update(self, vehicle_status):
        # force update if the car doesn't send info during wakeup_interval() minutes. The time of
        # the last wakeup counts too: a car which doesn't answer keeps the old updated_at, so it was
        # woken up again on every refresh until the wakeup rate limit stopped it (#1130).
        now = datetime.utcnow().replace(tzinfo=pytz.UTC)
        last_update = self.psacc.vehicles_list.get_car_by_vin(self.vin).get_status().get_energy('Electric').updated_at
        minutes_since_update = (now - last_update).total_seconds() / 60
        interval = self.wakeup_interval(vehicle_status, minutes_since_update)
        if minutes_since_update <= interval:
            return
        if self._last_wakeup is not None and (now - self._last_wakeup).total_seconds() <= 60 * interval:
            return
        try:
            if self.psacc.remote_client.wakeup(self.vin):
                self._last_wakeup = now
        except RateLimitException as e:
            # expected when the car is woken up too often, no need for a traceback
            logger.warning("Can't wake up %s: %s", self.vin, e)

    def wakeup_interval(self, vehicle_status, minutes_since_update=0):
        """Minutes without news from the charging car before waking it up.

        Half of the estimated time left until the threshold is reached: the car is woken up rarely
        while the threshold is far away and more and more often when it gets close.
        """
        electric = vehicle_status.get_energy('Electric')
        charging_mode = electric.charging.charging_mode
        if isinstance(charging_mode, str) and charging_mode == "Quick":
            min_timeout = self.wakeup_timeout / 2
        else:
            min_timeout = self.wakeup_timeout
        max_timeout = max(min_timeout, self.MAX_WAKEUP_TIMEOUT)
        if self.percentage_threshold >= 100:
            # only a stop hour (or nothing) to enforce: it doesn't depend on the battery level
            return max_timeout
        minutes_to_threshold = self._minutes_to_threshold(electric)
        if minutes_to_threshold is None:
            return min_timeout
        return min(max(min_timeout, (minutes_to_threshold - minutes_since_update) / 2), max_timeout)

    def _minutes_to_threshold(self, electric):
        """Estimated from the remaining time to a full battery; None if it can't be estimated."""
        try:
            level = float(electric.level)
            remaining = parse_hour(electric.charging.remaining_time)
            remaining_minutes = remaining[0] * 60 + remaining[1] + remaining[2] / 60
        except (AttributeError, TypeError, ValueError, IndexError):
            return None
        if remaining_minutes <= 0 or level >= 100 or level >= self.percentage_threshold:
            return None
        # charging slows down near 100 %, so below the threshold the car charges faster than this
        # linear estimate: wakeup_interval() only waits for half of it
        return (self.percentage_threshold - level) * remaining_minutes / (100 - level)

    def __is_approaching_scheduled_time(self, now: datetime):
        scheduled_hour, scheduled_minute = self.psacc.remote_client.get_charge_hour(self.vin)
        minutes_passed = now.hour * 60 + now.minute
        scheduled_minute_of_day = scheduled_hour * 60 + scheduled_minute
        return minutes_passed < scheduled_minute_of_day and scheduled_minute_of_day - minutes_passed < 30

    def process(self):
        now = datetime.now()
        try:
            vehicle_status = self.psacc.vehicles_list.get_car_by_vin(self.vin).get_status()
            status = vehicle_status.get_energy('Electric').charging.status
            level = vehicle_status.get_energy('Electric').level
            has_threshold = self.percentage_threshold < 100
            hit_threshold = level >= self.percentage_threshold
            if status == INPROGRESS:
                logger.info("charging status of %s is %s, battery level: %d", self.vin, status, level)
                self.force_update(vehicle_status)
                if has_threshold and hit_threshold and self.retry_count < 2:
                    logger.info("Charge threshold is reached, stop the charge")
                    self.control_charge_with_ack(False)
                elif self._next_stop_hour is not None:
                    if self._next_stop_hour < now:
                        self._next_stop_hour += timedelta(days=1)
                        logger.info("it's time to stop the charge")
                        self.control_charge_with_ack(False)
                    else:
                        next_in_second = (self._next_stop_hour - now).total_seconds()
                        if next_in_second < self.psacc.refresh_delay:
                            periodicity = next_in_second
                            thread = threading.Timer(periodicity, self.process)
                            thread.daemon = True
                            thread.start()
            elif status == STOPPED and has_threshold and hit_threshold and self.__is_approaching_scheduled_time(now):
                logger.info("Approaching scheduled charging time, but should not charge. Postponing charge hour!")
                self.force_update(vehicle_status)
                hour = now.hour - 1 if now.hour > 0 else 23
                self.psacc.remote_client.change_charge_hour(self.vin, hour, 0)
            else:
                if self._next_stop_hour is not None and self._next_stop_hour < now:
                    self._next_stop_hour += timedelta(days=1)
                self.retry_count = 0
        except (AttributeError, ValueError):
            logger.exception("Probably can't retrieve all information from API:")
        except BaseException:
            logger.exception("Charge control:")

    def get_dict(self):
        chd = copy(self.__dict__)
        chd.pop("psacc")
        chd.pop("_last_wakeup")
        return chd


class ChargeControls(dict):

    def __init__(self, file_name="charge_config.json"):
        self.file_name = file_name
        super().__init__()
        self._config_hash = None

    def save_config(self, force=False):
        chd = {}
        charge_control: ChargeControl
        for charge_control in self.values():
            chd[charge_control.vin] = {"percentage_threshold": charge_control.percentage_threshold,
                                       "stop_hour": charge_control.get_stop_hour()}
        config_str = json.dumps(chd, sort_keys=True, indent=4).encode('utf-8')
        new_hash = md5(config_str).hexdigest()
        if force or self._config_hash != new_hash:
            with open(self.file_name, "wb") as f:
                f.write(config_str)
            self._config_hash = new_hash
            logger.info("save config change")

    @staticmethod
    def load_config(psacc: PSAClient, name="charge_config.json"):
        with open(name, "r", encoding="utf-8") as file:
            config_str = file.read()
            chd = json.loads(config_str)
            charge_control_list = ChargeControls(name)
            for vin, params in chd.items():
                charge_control_list[vin] = ChargeControl(psacc, vin, **params)
            return charge_control_list

    def get(self, vin) -> ChargeControl:
        try:
            return self[vin]
        except KeyError:
            pass
        return None

    def init(self):
        for charge_control in self.values():
            charge_control.psacc.info_callback.append(charge_control.process)

    def stop(self):
        for charge_control in self.values():
            if charge_control.process in charge_control.psacc.info_callback:
                charge_control.psacc.info_callback.remove(charge_control.process)
