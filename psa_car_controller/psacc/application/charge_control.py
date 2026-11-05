import json
import logging
import threading
from copy import copy
from datetime import UTC, datetime, timedelta
from hashlib import md5
from time import sleep


from psa_car_controller.common import events
from psa_car_controller.psa.constants import DISCONNECTED, INPROGRESS, FINISHED, STOPPED
from psa_car_controller.psa.mqtt_event import mqtt_charging_status
from psa_car_controller.common.utils import RateLimitException, atomic_write, parse_hour
from .psa_client import PSAClient

logger = logging.getLogger(__name__)

# phases of the automatic stop, shown on the charge page and in /charge_control
WATCHING = "watching"  # charging below the limit
WAITING = "waiting"  # the stop was sent, the status is checked MQTT_TIMEOUT s later
CONFIRMED = "confirmed"  # the status after the stop says the car doesn't charge anymore
UNCONFIRMED = "unconfirmed"  # the status after the stop says nothing, the next refreshes decide
RETRYING = "retrying"  # still charging after the stop, it was sent again
GAVE_UP = "gave_up"  # still charging after MAX_STOP_ATTEMPTS stops
REASON_LIMIT = "limit"
REASON_STOP_HOUR = "stop_hour"


def describe_settings(threshold, stop_hour, estimate=False, stop_margin=0) -> str:
    """The automatic stop as set, for the event log."""
    stop_hour = stop_hour if stop_hour and any(stop_hour) else None
    if threshold >= 100 and not stop_hour:
        return "off"
    parts = []
    if threshold < 100:
        if estimate:
            parts.append(f"at {threshold} % (estimated level)")
        elif stop_margin:
            parts.append(f"at {threshold - stop_margin} % ({stop_margin} % before {threshold} %)")
        else:
            parts.append(f"at {threshold} %")
    if stop_hour:
        parts.append(f"at {stop_hour[0]:02d}:{stop_hour[1]:02d}")
    return " or ".join(parts)


class ChargeControl:
    MQTT_TIMEOUT = 60
    # most minutes between two wakeups of a charging car (wakeup_timeout is the fewest): far from
    # the threshold they get rarer (#1130: a slow charge doesn't update the api for a long time,
    # and waking the car up every refresh only drains its 12V battery)
    MAX_WAKEUP_TIMEOUT = 60
    # fewest minutes between two wakeups while the API doesn't answer: the car can't fix most of
    # these failures, and its battery level stays unknown until the API answers again anyway
    OUTDATED_WAKEUP_TIMEOUT = 30
    # minutes without news from a charging car after which the API answers, but its level says nothing:
    # more than one wakeup went unanswered by then, however rare they are. Only during a charge: a parked
    # car has nothing new to send, so its old date is normal and its level is the real one.
    STALE_CHARGE_TIMEOUT = 2 * MAX_WAKEUP_TIMEOUT
    # a stop that didn't take is sent twice in a row, then once every LATE_RETRY_INTERVAL minutes while the car
    # still charges over the limit, MAX_STOP_ATTEMPTS times in all: a car can miss a stop (seen on a Mokka-e)
    LATE_RETRY_INTERVAL = 15
    MAX_STOP_ATTEMPTS = 4
    # The car sends its level only every 10-15 minutes while charging (a Mokka-e: about 4 % each time), so waiting
    # for it to report the limit ends the charge a few % above. The level is estimated from how fast it rose:
    # not further than this from the last report, the car is woken up instead,
    MAX_ESTIMATE_MINUTES = 30
    # and a little ahead, for the stop to reach the car and for the next refresh
    STOP_LEAD_MINUTES = 2
    # the last levels reported during the charge give its speed: about 40 minutes, the charge slows down near 100 %
    MAX_LEVEL_POINTS = 4
    # largest stop_margin, in %
    MAX_STOP_MARGIN = 20

    # pylint: disable-next=too-many-arguments,too-many-positional-arguments
    def __init__(self, psacc: PSAClient, vin, percentage_threshold, stop_hour, estimate=False, stop_margin=0):
        self.vin = vin
        self.percentage_threshold = percentage_threshold
        self.set_stop_hour(stop_hour)
        self.set_estimate(estimate, stop_margin)
        self.psacc = psacc
        self.retry_count = 0
        self.wakeup_timeout = 10
        self._last_wakeup = None
        self._last_stop_at = None
        # (date of the level, level) during the current charge
        self._level_points = []
        self._state = None

    def set_estimate(self, estimate, stop_margin=0):
        """estimate: stop when the level estimated between two reports reaches the limit. Without it, stop_margin %
        below the limit: the car reports its level only every few % while charging. Invalid values are the defaults:
        they are optional, the limit itself must not be lost because of them."""
        self.estimate = estimate is True
        valid = isinstance(stop_margin, int) and not isinstance(stop_margin, bool) and \
            0 <= stop_margin <= self.MAX_STOP_MARGIN
        if not valid:
            logger.warning("Invalid stop margin %r for %s, using 0", stop_margin, self.vin)
        self.stop_margin = stop_margin if valid else 0

    def stop_level(self):
        """The battery level at which the charge is stopped."""
        if self.estimate or self.percentage_threshold >= 100:
            return self.percentage_threshold
        return max(self.percentage_threshold - self.stop_margin, 0)

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

    def _set_state(self, phase, reason=None, message="", level=logging.INFO):
        """What the automatic stop does, for the charge page; recorded as an event when it changes."""
        previous = self._state
        self._state = {"phase": phase, "reason": reason if reason is not None else (previous or {}).get("reason"),
                       "message": message, "attempts": self.retry_count,
                       "since": datetime.now(UTC) if previous is None or previous["phase"] != phase
                       else previous["since"]}
        if previous is None or (previous["phase"], previous["message"]) != (phase, message):
            if phase != WATCHING or previous is None or previous["phase"] != WATCHING:
                events.record(events.CHARGE_CONTROL, message, vin=self.vin, level=level)

    def get_state(self) -> dict:
        """The automatic stop now, with the answer of the car to the last charge command: None while it has nothing
        to do."""
        state = dict(self._state) if self._state else None
        if state is not None:
            state["since"] = state["since"].isoformat()
            state["command"] = self._last_charge_command()
        return state

    def _last_charge_command(self):
        try:
            commands = [command for command in self.psacc.remote_client.commands.to_list(self.vin)
                        if command["name"] == "/VehCharge"]
        except (AttributeError, TypeError, KeyError):
            return None
        if not commands:
            return None
        last = commands[-1]
        return {"correlation_id": last["correlation_id"], "status": last["status"], "pending": last["pending"],
                "error": last["error"], "sent_at": datetime.fromtimestamp(last["sent_at"], UTC).isoformat()}

    def control_charge_with_ack(self, charge: bool, reason=None):
        self.psacc.remote_client.charge_now(self.vin, charge)
        self.retry_count += 1
        self._last_stop_at = datetime.now(UTC)
        if not charge:
            self._set_state(WAITING, reason, f"Stop sent ({self._reason_text(reason)}), attempt {self.retry_count}: "
                            f"checking in {ChargeControl.MQTT_TIMEOUT} s if the car stopped")
        sleep(ChargeControl.MQTT_TIMEOUT)
        # part of the refresh loop: it asks PSA as the loop does, the stored status would hide the result
        vehicle_status = self.psacc.get_vehicle_info(self.vin, scheduled=True)
        status = self._charging_status(vehicle_status)
        if status is None or self.psacc.vehicles_list.get_car_by_vin(self.vin).status_outdated \
                or self._charge_status_stale(vehicle_status, status):
            # sending the command again wouldn't tell more, the next refreshes decide
            logger.warning("Can't check if the charge of %s changed, its status is unknown or outdated", self.vin)
            if not charge:
                self._set_state(UNCONFIRMED, message="Can't tell yet if the car stopped: its status is unknown or "
                                                     "outdated, the next refreshes decide", level=logging.WARNING)
            return False
        if status in (FINISHED, DISCONNECTED):
            logger.warning("Car state isn't compatible with charging %s", status)
        if (status == INPROGRESS) != charge:
            logger.warning("retry to control the charge of %s", self.vin)
            self.psacc.remote_client.charge_now(self.vin, charge)
            self.retry_count += 1
            self._last_stop_at = datetime.now(UTC)
            if not charge:
                self._set_state(RETRYING, message=f"Still charging after the stop, sent again (attempt "
                                f"{self.retry_count})", level=logging.WARNING)
            return False
        self.retry_count = 0
        if not charge:
            level = vehicle_status.get_energy('Electric').level
            self._set_state(CONFIRMED, message=f"Charge stopped, battery {level} % (status {status})")
        return True

    @staticmethod
    def _reason_text(reason):
        return {REASON_LIMIT: "battery limit", REASON_STOP_HOUR: "stop time"}.get(reason, "charge control")

    def _record_level(self, electric):
        """Keep the levels of this charge, they tell how fast it goes."""
        updated_at = getattr(electric, "updated_at", None)
        level = getattr(electric, "level", None)
        if not isinstance(updated_at, datetime) or not isinstance(level, (int, float)):
            return
        if self._level_points and self._level_points[-1][0] >= updated_at:
            return
        self._level_points = (self._level_points + [(updated_at, level)])[-self.MAX_LEVEL_POINTS:]

    def _percent_per_minute(self, electric):
        """(speed of the charge in % per minute, where it comes from): from the levels reported during it, else from
        the time left to 100 %; (None, why not) if neither tells."""
        if len(self._level_points) >= 2:
            (first_at, first_level), (last_at, last_level) = self._level_points[0], self._level_points[-1]
            minutes = (last_at - first_at).total_seconds() / 60
            if minutes >= 10 and last_level > first_level:
                return (last_level - first_level) / minutes, \
                    f"{len(self._level_points)} reports, {first_level} → {last_level} % in {minutes:.0f} min"
        try:
            level = float(electric.level)
            remaining = parse_hour(electric.charging.remaining_time)
            remaining_minutes = remaining[0] * 60 + remaining[1] + remaining[2] / 60
        except (AttributeError, TypeError, ValueError, IndexError):
            return None, "too few reports and no remaining time"
        if remaining_minutes <= 0 or level >= 100:
            return None, "too few reports and no remaining time"
        return (100 - level) / remaining_minutes, f"remaining time {electric.charging.remaining_time} to 100 %"

    def estimated_level(self, vehicle_status):
        """Battery level now plus STOP_LEAD_MINUTES, from the last one reported and the speed of the charge; None
        when it can't be estimated. How it was estimated goes to the debug log."""
        electric = vehicle_status.get_energy('Electric')
        minutes = self._minutes_since_update(vehicle_status)
        speed, source = self._percent_per_minute(electric)
        if speed is None or not 0 <= minutes <= self.MAX_ESTIMATE_MINUTES:
            logger.debug("estimate of %s: none, %s", self.vin,
                         source if speed is None else f"last report {minutes:.0f} min ago, over "
                         f"{self.MAX_ESTIMATE_MINUTES} min")
            return None
        try:
            estimated = min(float(electric.level) + speed * (minutes + self.STOP_LEAD_MINUTES), 100)
        except (TypeError, ValueError):
            return None
        logger.debug("estimate of %s: %.1f %% = %s %% reported %.1f min ago + %.3f %%/min (%s) * (%.1f + %d min "
                     "ahead), limit %s %%", self.vin, estimated, electric.level, minutes, speed, source, minutes,
                     self.STOP_LEAD_MINUTES, self.percentage_threshold)
        return estimated

    def _late_retry_due(self, now_utc: datetime):
        if self.retry_count >= self.MAX_STOP_ATTEMPTS:
            return False
        return self._last_stop_at is None or \
            (now_utc - self._last_stop_at).total_seconds() >= 60 * self.LATE_RETRY_INTERVAL

    @staticmethod
    def _charging_status(vehicle_status):
        """The charging status of the car, None if the API didn't send it."""
        if vehicle_status is None:
            return None
        charging = vehicle_status.get_energy('Electric').charging
        return None if charging is None else charging.status

    @staticmethod
    def _minutes_since_update(vehicle_status):
        """Age of the battery level: the date of the status also moves with the position."""
        last_update = vehicle_status.get_energy('Electric').updated_at
        if last_update is None:
            return float('inf')
        return (datetime.now(UTC) - last_update).total_seconds() / 60

    def _charge_status_stale(self, vehicle_status, status):
        """The API answers but the charging car doesn't: as outdated as when the API fails."""
        if status != INPROGRESS:
            return False
        minutes_since_update = self._minutes_since_update(vehicle_status)
        return self.STALE_CHARGE_TIMEOUT < minutes_since_update < float('inf')

    def force_update(self, vehicle_status, outdated=False, stale=False):
        # force update if the car doesn't send info during wakeup_interval() minutes. The time of
        # the last wakeup counts too: a car which doesn't answer keeps the old updated_at, so it was
        # woken up again on every refresh until the wakeup rate limit stopped it (#1130).
        now = datetime.now(UTC)
        minutes_since_update = self._minutes_since_update(vehicle_status)
        interval = self.wakeup_interval(vehicle_status, minutes_since_update)
        if outdated:
            interval = max(interval, self.OUTDATED_WAKEUP_TIMEOUT)
        elif stale:
            # each unanswered wakeup doubles the wait: "charging" can be left over from a car which
            # isn't plugged in anymore, nothing recharges its 12V battery then
            interval = max(interval, self.OUTDATED_WAKEUP_TIMEOUT, minutes_since_update / 2)
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
        if remaining_minutes <= 0 or level >= 100 or level >= self.stop_level():
            return None
        # charging slows down near 100 %, so below the threshold the car charges faster than this
        # linear estimate: wakeup_interval() only waits for half of it
        return (self.stop_level() - level) * remaining_minutes / (100 - level)

    def __is_approaching_scheduled_time(self, now: datetime):
        try:
            scheduled_hour, scheduled_minute = self.psacc.remote_client.get_charge_hour(self.vin)
        except (AttributeError, TypeError):  # the API didn't send it
            return False
        minutes_passed = now.hour * 60 + now.minute
        scheduled_minute_of_day = scheduled_hour * 60 + scheduled_minute
        return minutes_passed < scheduled_minute_of_day and scheduled_minute_of_day - minutes_passed < 30

    def _advance_stop_hour(self, now: datetime):
        # more than once if no status came in for days: the stop isn't sent once per missed day
        while self._next_stop_hour < now:
            self._next_stop_hour += timedelta(days=1)

    def _stop_if_state_unknown(self, vehicle_status, over_threshold, now: datetime):
        """No status tells whether the car charges, so stop it as if it did: that's harmless if it doesn't.

        At most twice for each time the threshold is passed and once for each stop hour, as the car
        has to be reached each time. charge_now() needs the charge hour of the car not to overwrite
        it: without one, wait for a status (a stop hour stays due until then).
        """
        if not over_threshold:
            self.retry_count = 0
        charging = vehicle_status.get_energy('Electric').charging
        if charging is None or charging.next_delayed_time is None:
            logger.info("Charging status of %s is unknown, can't control its charge", self.vin)
            return
        if self._next_stop_hour is not None and self._next_stop_hour < now:
            self._advance_stop_hour(now)
            logger.info("it's time to stop the charge, if the car is charging")
            self.control_charge_with_ack(False, REASON_STOP_HOUR)
        elif over_threshold and self.retry_count < 2:
            logger.info("Charge threshold is reached, stop the charge if the car is charging")
            self.control_charge_with_ack(False, REASON_LIMIT)

    def process(self):
        now = datetime.now()
        try:
            car = self.psacc.vehicles_list.get_car_by_vin(self.vin)
            vehicle_status = car.status
            if vehicle_status is None:
                logger.info("No status of %s yet, can't control its charge", self.vin)
                return
            status = self._charging_status(vehicle_status)
            level = vehicle_status.get_energy('Electric').level
            outdated = car.status_outdated
            stale = self._charge_status_stale(vehicle_status, status)
            has_threshold = self.percentage_threshold < 100
            estimated = None
            if status == INPROGRESS and not outdated and not stale:
                self._record_level(vehicle_status.get_energy('Electric'))
                estimated = self.estimated_level(vehicle_status) if has_threshold and self.estimate else None
            elif status != INPROGRESS:
                self._level_points = []
            # An outdated level says nothing: the car may have been driven and plugged in again since
            # (and mqtt reports that charge but no level), so nothing is decided from it until the
            # API answers. It only ever hides a reached threshold, while the car is woken up to fix that.
            hit_threshold = not outdated and not stale and level is not None and (
                level >= self.stop_level() or
                (estimated is not None and estimated >= self.percentage_threshold))
            if status == INPROGRESS:
                logger.info("charging status of %s is %s, battery level: %s%s%s", self.vin, status, level,
                            "" if estimated is None else f" (estimated {estimated:.1f})",
                            " (too old to decide from)" if stale else "")
                self.force_update(vehicle_status, outdated, stale)
                sent = self._control_limit(level, estimated, hit_threshold, outdated or stale)
                if self._next_stop_hour is not None and not sent:
                    if self._next_stop_hour < now:
                        self._advance_stop_hour(now)
                        logger.info("it's time to stop the charge")
                        self.control_charge_with_ack(False, REASON_STOP_HOUR)
                    else:
                        next_in_second = (self._next_stop_hour - now).total_seconds()
                        if next_in_second < self.psacc.refresh_delay:
                            thread = threading.Timer(next_in_second, self.process)
                            thread.daemon = True
                            thread.start()
            elif status == STOPPED and has_threshold and hit_threshold and self.__is_approaching_scheduled_time(now):
                logger.info("Approaching scheduled charging time, but should not charge. Postponing charge hour!")
                self.force_update(vehicle_status)
                hour = now.hour - 1 if now.hour > 0 else 23
                self.psacc.remote_client.change_charge_hour(self.vin, hour, 0)
            elif status is None or (outdated and mqtt_charging_status(vehicle_status) is None):
                # with a state from mqtt, newer than the outdated status and not a charge: it isn't unknown
                self._stop_if_state_unknown(vehicle_status, has_threshold and hit_threshold, now)
            else:
                if self._next_stop_hour is not None and self._next_stop_hour < now:
                    self._advance_stop_hour(now)
                self.retry_count = 0
            if status not in (None, INPROGRESS) and not outdated:
                self._charge_ended(status, level)
        except (AttributeError, ValueError):
            logger.exception("Probably can't retrieve all information from API:")
        except BaseException:
            logger.exception("Charge control:")

    def _control_limit(self, level, estimated, hit_threshold, too_old) -> bool:
        """During a charge: stop it at the limit, again later if the car didn't follow. True if a stop was sent."""
        has_threshold = self.percentage_threshold < 100
        if has_threshold and hit_threshold and self.retry_count < 2:
            logger.info("Charge threshold is reached, stop the charge")
            self.control_charge_with_ack(False, REASON_LIMIT)
            return True
        if has_threshold and hit_threshold and self._late_retry_due(datetime.now(UTC)):
            logger.warning("%s still charges over the threshold, stop the charge again", self.vin)
            self.control_charge_with_ack(False, REASON_LIMIT)
            return True
        if has_threshold and hit_threshold and self.retry_count >= self.MAX_STOP_ATTEMPTS:
            if (self._state or {}).get("phase") != GAVE_UP:
                logger.error("%s still charges at %s %% after %d stops, giving up until the next charge",
                             self.vin, level, self.retry_count)
            self._set_state(GAVE_UP, REASON_LIMIT, f"Still charging at {level} % after {self.retry_count} "
                            "stops: no more until the next charge", logging.ERROR)
        elif not hit_threshold and (has_threshold or self._next_stop_hour is not None):
            self._set_state(WATCHING, REASON_LIMIT if has_threshold else REASON_STOP_HOUR,
                            self._watching_text(level, estimated, too_old))
        return False

    def _watching_text(self, level, estimated, too_old):
        targets = []
        if self.percentage_threshold < 100:
            if self.stop_level() < self.percentage_threshold:
                targets.append(f"at {self.stop_level()} % ({self.stop_margin} % before the limit of "
                               f"{self.percentage_threshold} %)")
            else:
                targets.append(f"at {self.percentage_threshold} %")
        if self._next_stop_hour is not None:
            targets.append(f"at {self._next_stop_hour:%H:%M}")
        text = f"Charging, stops {' or '.join(targets)}: battery {level} %"
        if estimated is not None:
            text += f", about {estimated:.0f} % in {self.STOP_LEAD_MINUTES} min"
        if too_old:
            text += " (the level is too old to decide from, the car is woken up)"
        return text

    def _charge_ended(self, status, level):
        """The car doesn't charge anymore: a stop that couldn't be confirmed is now, a charge that ended on its own
        is noted."""
        phase = (self._state or {}).get("phase")
        if phase in (WAITING, UNCONFIRMED, RETRYING, GAVE_UP):
            self._set_state(CONFIRMED, message=f"Charge stopped, battery {level} % (status {status})")
        elif phase == WATCHING:
            events.record(events.CHARGE_CONTROL, "Charge ended before the automatic stop: battery %s %% (status %s)",
                          level, status, vin=self.vin)
            self._state = None

    def get_dict(self):
        chd = copy(self.__dict__)
        chd.pop("psacc")
        chd.pop("_last_wakeup")
        for name in ("_last_stop_at", "_level_points", "_state"):
            chd.pop(name, None)
        chd["auto_stop"] = self.get_state()
        return chd


class ChargeControls(dict):
    _save_lock = threading.Lock()

    def __init__(self, file_name="charge_config.json"):
        self.file_name = file_name
        super().__init__()
        self._config_hash = None

    def save_config(self, force=False):
        with self._save_lock:  # read and write in one go: a slower save must not put older values back
            chd = {}
            charge_control: ChargeControl
            for charge_control in self.values():
                settings = {"percentage_threshold": charge_control.percentage_threshold,
                            "stop_hour": charge_control.get_stop_hour()}
                # only when set: the file stays as it was for those who don't use them
                if charge_control.estimate:
                    settings["estimate"] = True
                if charge_control.stop_margin:
                    settings["stop_margin"] = charge_control.stop_margin
                chd[charge_control.vin] = settings
            config_str = json.dumps(chd, sort_keys=True, indent=4).encode('utf-8')
            new_hash = md5(config_str).hexdigest()
            if force or self._config_hash != new_hash:
                atomic_write(self.file_name, config_str)
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
