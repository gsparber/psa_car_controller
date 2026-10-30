import json
import logging
import threading
import time
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
from os import environ
from typing import Callable, List, Optional

import paho.mqtt.client as mqtt
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.reasoncodes import ReasonCode
from requests import RequestException, Timeout

from psa_car_controller.psacc.model.car import Cars
from psa_car_controller.psacc.model.car_status import status_updated_at
from psa_car_controller.psa.AccountInformation import AccountInformation
from psa_car_controller.psa.RemoteCredentials import RemoteCredentials
from psa_car_controller.psa.constants import INPROGRESS, DEFAULT_PRECONDITIONING_PROGRAM, IMMEDIATE_CHARGE, \
    DELAYED_CHARGE, REMOTE_URL
from psa_car_controller.psa import precond_programs
from psa_car_controller.psa.mqtt_client import MqttClient
from psa_car_controller.psa.mqtt_event import VehicleEvents, CHANGED, OUT_OF_ORDER, parse_event_date, \
    rest_charging_status
from psa_car_controller.psa.mqtt_request import MQTTRequest
from psa_car_controller.psa.oauth import OpenIdCredentialManager
from psa_car_controller.psa.remote_commands import RemoteCommand, RemoteCommands, RC_SUCCESS, \
    RC_TOKEN_EXPIRED, RC_WAKEUP_FORWARDED, REASON_NOT_COMPATIBLE, FAILED, RETRIED
from psa_car_controller.common.utils import RateLimitException, rate_limit, parse_hour, TIMEOUT_IN_S
from psa_car_controller.psa.otp.otp import ConfigException, save_otp, load_otp

logger = logging.getLogger(__name__)

MQTT_SERVER = "mwa.mpsa.com"
MQTT_RESP_TOPIC = "psa/RemoteServices/to/cid/"
MQTT_EVENT_TOPIC = "psa/RemoteServices/events/MPHRTServices/"
MQTT_TOKEN_TTL = 890
# minimum delay between two remote token refresh attempts: the broker closes the socket without
# CONNACK when the access token is expired, and paho reconnects at least every
# MAX_REFRESH_DELAY seconds, so refreshing on every disconnect would burn the
# refresh_token_now() rate limit (6 per 30 min) during a mere network outage.
MAX_REFRESH_DELAY = 120
KEEPALIVE_TIMEOUT = ReasonCode(PacketTypes.DISCONNECT, "Keep alive timeout")
# The remote refresh token's server-side lifetime is about 24h: a grant attempted 24h
# (plus a few seconds) after the previous one fails with 'invalid_grant', and each grant
# rotates the token. The __keep_mqtt timer is the ONLY periodic refresh on an idle car,
# so it must run well within that lifetime: 8h keeps the chain rotating forever and
# survives one missed cycle.
KEEPALIVE_PERIOD = 3600 * 8
# a failed keepalive refresh is retried sooner, still well within the token lifetime
KEEPALIVE_RETRY_PERIOD = 1800
# Each wakeup powers the car's telematics unit from the 12V service battery: too many drain
# it (keyless entry stops working). Same limit as the Home Assistant integration, per car.
WAKEUP_LIMIT = 6
WAKEUP_PERIOD = 60 * 20
# When a charge begins, the api sometimes keeps reporting no charge: the car is woken up after
# API_UPDATE_DELAY to make it send its status again. Some cars (seen with slow charges, #1130)
# answer every wakeup with a new charging event while the api never catches up, so the delay
# doubles after each wakeup which didn't help, up to API_UPDATE_MAX_DELAY.
API_UPDATE_DELAY = 60
API_UPDATE_MAX_DELAY = 30 * 60
# the SMS endpoint regularly takes longer than the default timeout to answer
SMS_TIMEOUT_IN_S = 20


class RemoteException(Exception):
    pass


class SmsRequestException(RemoteException):
    """The SMS code could not be requested: hint tells the user what to do, details what happened."""

    def __init__(self, hint: str, details: str):
        super().__init__(f"{hint} ({details})")
        self.hint = hint
        self.details = details


class WakeupRateLimiter:
    """At most `limit` wakeups per car within `period` seconds.

    Shared by every RemoteClient (module level): a new login replaces the client, it must
    not reset the budget of the cars.
    """

    def __init__(self, limit=WAKEUP_LIMIT, period=WAKEUP_PERIOD, clock=time.monotonic):
        self.limit = limit
        self.period = period
        self.clock = clock
        self._lock = threading.Lock()
        self._sent = {}

    def acquire(self, vin):
        """Reserve a wakeup for vin, returns its token for release(); raises RateLimitException."""
        with self._lock:
            now = self.clock()
            sent = self._sent.setdefault(vin, deque())
            while sent and now - sent[0] >= self.period:
                sent.popleft()
            if len(sent) >= self.limit:
                retry_in = self.period - (now - sent[0])
                raise RateLimitException(f"wakeup of {vin}: {self.limit} per {self.period // 60} min, "
                                         f"retry in {retry_in:.0f}s")
            sent.append(now)
            return now

    def release(self, vin, token):
        """Give back a reservation whose wakeup never reached the car."""
        with self._lock:
            try:
                self._sent.get(vin, deque()).remove(token)
            except ValueError:
                pass

    def reset(self):
        with self._lock:
            self._sent.clear()


wakeup_limiter = WakeupRateLimiter()


class RemoteClient:

    def __init__(self, account_info: AccountInformation, vehicles_list: Cars, manager: OpenIdCredentialManager,
                 remoteCredentials: RemoteCredentials):
        self.vehicles_list = vehicles_list
        self.remoteCredentials: RemoteCredentials = remoteCredentials
        self.manager = manager
        self.precond_programs = {}
        # vehicle events received on mqtt, by vin
        self.vehicle_events = {}
        self._vehicle_events_lock = threading.Lock()
        self.account_info = account_info
        self.headers = {
            "x-introspect-realm": self.account_info.realm,
            "accept": "application/hal+json",
            "User-Agent": "okhttp/4.8.0",
        }
        # commands sent recently, by correlation_id: their answers are matched against them
        self.commands = RemoteCommands()
        # called with the command after each answer to it, e.g. to fetch the new status after a success
        self.command_callbacks: List[Callable[[RemoteCommand], None]] = []
        # correlation ids published by the current thread inside track_commands()
        self._tracked = threading.local()
        self.mqtt_client = None
        self.otp = None
        self._lock = threading.Lock()
        self.update_thread: threading.Timer = None
        self._api_update_lock = threading.Lock()
        self._api_update_timers = {}
        # wakeups sent in a row because of charging events which the api didn't catch up with
        self._api_update_attempts = {}
        self._api_update_stopped = False
        self.remote_token_last_update: Optional[datetime] = None
        # consecutive disconnects because the broker refused the remote credentials, reset on a
        # successful connect: exposed on /health so a dead remote control doesn't go unnoticed
        self.mqtt_auth_failures = 0
        self.mqtt_last_connect: Optional[datetime] = None
        # the broker refused the last CONNACK: the disconnect following it isn't an expired token
        self._mqtt_connect_refused = False

    def __on_mqtt_connect(self, client, userdata, flags, reason_code, *_):  # pylint: disable=unused-argument
        logger.info("Connected with result code %s", reason_code)
        if reason_code.is_failure:
            # paho calls on_disconnect right after, but only with a generic reason code
            self._mqtt_connect_refused = True
            self.mqtt_auth_failures += 1
            logger.warning("MQTT credentials refused %d times in a row", self.mqtt_auth_failures)
            return
        self.mqtt_auth_failures = 0
        self.mqtt_last_connect = datetime.now(timezone.utc)
        topics = [MQTT_RESP_TOPIC + self.account_info.get_mqtt_customer_id() + "/#"]
        for car in self.vehicles_list:
            topics.append(MQTT_EVENT_TOPIC + car.vin)
        for topic in topics:
            client.subscribe(topic)
            logger.info("subscribe to %s", topic)

    def _on_mqtt_disconnect(self, client, userdata, flags, reason_code, *_):  # pylint: disable=unused-argument
        # the client reconnects on its own, so report the reason on a single line
        logger.warning("MQTT disconnected: %s", reason_code)
        refused, self._mqtt_connect_refused = self._mqtt_connect_refused, False
        # paho 2 reports a lost connection (socket closed, no CONNACK) only as "Unspecified error";
        # a keepalive timeout is a network problem, a refused CONNACK is counted in on_connect
        if refused or not reason_code.is_failure or reason_code == KEEPALIVE_TIMEOUT:
            return
        # the broker closes the socket without CONNACK when the access token is expired:
        # paho then retries forever with the same stale token unless it is refreshed here.
        # Throttling is handled inside _refresh_remote_token.
        self._refresh_remote_token(force=True)

    def _on_mqtt_message(self, client, userdata, msg):  # pylint: disable=unused-argument
        try:
            logger.debug("mqtt msg received: %s %s", msg.topic, msg.payload)
            data = json.loads(msg.payload)
            charge_info = None
            if msg.topic.startswith(MQTT_RESP_TOPIC):
                if "return_code" not in data:
                    logger.debug("mqtt msg hasn't return code")
                else:
                    self._on_command_answer(data)
            elif msg.topic.startswith(MQTT_EVENT_TOPIC):
                charge_info = data["charging_state"]
                if not self._on_vehicle_event(data):
                    return
            self._fix_not_updated_api(charge_info, data["vin"], data.get("date"))
        except KeyError:
            logger.exception("on_mqtt_message:")

    def _on_command_answer(self, data):
        return_code = str(data["return_code"])
        reason = data.get("reason")
        command = self.commands.answer(data.get("correlation_id"), return_code, reason, data.get("resp_data"))
        name = command.name if command else "unknown command"
        try:
            self._handle_command_answer(command, name, return_code, reason)
        finally:
            if command is not None:
                self._notify_command(command)

    def _handle_command_answer(self, command, name, return_code, reason):
        if return_code == RC_SUCCESS:
            return
        if return_code == RC_WAKEUP_FORWARDED:
            logger.info("%s: wakeup forwarded to the car, waiting for its answer", name)
        elif return_code != RC_TOKEN_EXPIRED:
            error = command.error if command is not None and command.error else reason
            logger.error("mqtt error %s : %s (%s)", return_code, error or "?", name)
        elif reason == REASON_NOT_COMPATIBLE:
            logger.error("%s isn't supported by this car: %s", name, reason)
        elif command is None:
            # sent by a previous client or too long ago: resending a random other command
            # (what the old global last_request did) could be the wrong car
            self._refresh_remote_token(force=True)
            logger.error("remote token was refused for an unknown command, it isn't sent again")
        elif command.retried:
            self.commands.set_status(command, FAILED, error="remote token refused again after a refresh")
            logger.error("%s refused again after a remote token refresh, giving up", name)
        else:
            self._refresh_remote_token(force=True)
            retry = command.request.retry()
            logger.warning("%s is sent again, remote token was expired", name)
            self.commands.set_status(command, RETRIED, retried_as=retry.correlation_id)
            if not self.publish(retry):
                self.commands.set_status(command, FAILED, error="remote token refresh failed")

    def _notify_command(self, command: RemoteCommand):
        for callback in self.command_callbacks:
            try:
                callback(command)
            except Exception:  # pylint: disable=broad-except
                logger.exception("command callback:")

    @contextmanager
    def track_commands(self):
        """Collect the correlation ids of the commands this thread publishes inside the with block."""
        sent: List[str] = []
        self._tracked.sent = sent
        try:
            yield sent
        finally:
            self._tracked.sent = None

    def _get_vehicle_events(self, vin) -> VehicleEvents:
        with self._vehicle_events_lock:
            if vin not in self.vehicle_events:
                self.vehicle_events[vin] = VehicleEvents(vin)
            return self.vehicle_events[vin]

    def _on_vehicle_event(self, data) -> bool:
        """Take a vehicle event into account, False if it is older than one already received."""
        vin = data["vin"]
        result = self._get_vehicle_events(vin).add(data)
        if result == OUT_OF_ORDER:
            return False
        if result == CHANGED:
            programs = (data.get("precond_state") or {}).get("programs")
            if programs:
                self._store_precond_programs(vin, programs)
            self.apply_mqtt_state(self.vehicles_list.get_car_by_vin(vin))
        return True

    def apply_mqtt_state(self, car):
        """Overwrite the parts of car.status which changed on mqtt after the api reported them.

        mqtt pushes changes live while the rest api is polled every ~2 min, and some cars never
        report lockedState on the api at all. See mqtt_event for which values are used.
        """
        if car is None or car.status is None or car.vin not in self.vehicle_events:
            return
        self.vehicle_events[car.vin].apply(car.status)

    def _fix_not_updated_api(self, charge_info, vin, event_date=None):
        # Only a real charging rate counts: some cars (seen on an e-2008) keep reporting a stale
        # remaining_time while unplugged, so every answer to a wakeup triggered another wakeup.
        if charge_info is not None and charge_info.get('rate', 0) == 0:
            with self._api_update_lock:
                self._api_update_attempts.pop(vin, None)  # not charging: the next charge starts afresh
        elif charge_info is not None:
            try:
                car = self.vehicles_list.get_car_by_vin(vin=vin)
                if car and rest_charging_status(car.status) != INPROGRESS:
                    # events can arrive late or out of order: an event older than the api status
                    # doesn't mean the api missed the charge, the car stopped charging since
                    if self._is_older_than_status(event_date, car.status):
                        logger.debug("ignore charging event of %s from %s, the api status is newer", vin, event_date)
                        return
                    # fix a psa server bug where charge beginning without status api being properly updated.
                    # The delayed wakeup runs on a timer: sleeping here blocked the paho network thread
                    # for 60s, as long as the mqtt keepalive, so the broker dropped the connection (rc 16).
                    with self._api_update_lock:
                        if self._api_update_stopped or vin in self._api_update_timers:
                            return
                        delay = self._api_update_delay(vin)
                        logger.warning("charge begin but API isn't updated, wake up %s in %d s", vin, delay)
                        timer = threading.Timer(delay, self._delayed_api_update, args=[vin])
                        timer.daemon = True
                        self._api_update_timers[vin] = timer
                        timer.start()
            except (IndexError, AttributeError):
                logger.exception("on_mqtt_message:")

    def _api_update_delay(self, vin):
        attempts = self._api_update_attempts.get(vin, 0)
        return min(API_UPDATE_DELAY * 2 ** min(attempts, 16), API_UPDATE_MAX_DELAY)

    @staticmethod
    def _is_older_than_status(event_date, status):
        event_time = parse_event_date(event_date)
        try:
            status_time = status_updated_at(status)
            return event_time is not None and status_time is not None and event_time < status_time
        except TypeError:  # naive status date
            return False

    def _delayed_api_update(self, vin):
        with self._api_update_lock:
            if self._api_update_stopped:
                return
        try:
            car = self.vehicles_list.get_car_by_vin(vin=vin)
            if car and rest_charging_status(car.status) != INPROGRESS:
                if self.wakeup(vin):
                    with self._api_update_lock:
                        self._api_update_attempts[vin] = self._api_update_attempts.get(vin, 0) + 1
            else:
                with self._api_update_lock:
                    self._api_update_attempts.pop(vin, None)  # the api caught up
        except RateLimitException as e:
            # expected when the car is woken up too often, no need for a traceback
            logger.warning("Can't wake up %s: %s", vin, e)
        except Exception:  # pylint: disable=broad-except
            logger.exception("_delayed_api_update:")
        finally:
            with self._api_update_lock:
                self._api_update_timers.pop(vin, None)

    def is_mqtt_connected(self) -> bool:
        return self.mqtt_client is not None and self.mqtt_client.is_connected()

    def start(self):
        self.mqtt_auth_failures = 0
        with self._api_update_lock:
            self._api_update_stopped = False
        if self.load_otp():
            self.mqtt_client = MqttClient(clean_session=True, protocol=mqtt.MQTTv311)
            if environ.get("MQTT_LOG", "0") == "1":
                self.mqtt_client.enable_logger(logger=logger)
            if self._refresh_remote_token():
                self.mqtt_client.tls_set_context()
                self.mqtt_client.on_connect = self.__on_mqtt_connect
                self.mqtt_client.on_message = self._on_mqtt_message
                self.mqtt_client.on_disconnect = self._on_mqtt_disconnect
                self.mqtt_client.connect(MQTT_SERVER, 8885, 60)
                self.mqtt_client.loop_start()
                self.__schedule_keep_mqtt(KEEPALIVE_PERIOD)
                return self.mqtt_client.is_connected()
        logger.error("Can't configure MQTT Client")
        return False

    def stop(self):
        self.mqtt_auth_failures = 0
        with self._api_update_lock:
            self._api_update_stopped = True
            timers = list(self._api_update_timers.values())
            self._api_update_timers.clear()
        for timer in timers:
            timer.cancel()
        if self.mqtt_client:
            logger.info("stop mqtt...")
            self.mqtt_client.on_disconnect = None
            self.mqtt_client.disconnect()
        if self.update_thread:
            self.update_thread.cancel()
            self.update_thread.join(timeout=TIMEOUT_IN_S)

    def __keep_mqtt(self):  # avoid token expiration
        # Only rotate the remote token: this used to wake the first car up every cycle, which
        # drains its 12V battery for nothing (the refresh in publish() kept the token alive).
        try:
            refreshed = self._refresh_remote_token()
        except Exception:  # pylint: disable=broad-except
            logger.exception("__keep_mqtt")
            refreshed = False
        if not refreshed:
            logger.warning("keepalive: remote token refresh failed, retrying in %ds", KEEPALIVE_RETRY_PERIOD)
        self.__schedule_keep_mqtt(KEEPALIVE_PERIOD if refreshed else KEEPALIVE_RETRY_PERIOD)

    def __schedule_keep_mqtt(self, delay):
        with self._api_update_lock:
            if self._api_update_stopped:  # stop() ran during the refresh
                return
        if self.update_thread:
            self.update_thread.cancel()
        self.update_thread = threading.Timer(delay, self.__keep_mqtt)
        self.update_thread.daemon = True
        self.update_thread.start()

    def veh_charge_request(self, vin, hour, minute, charge_type):
        msg = self.mqtt_request(vin, {"program": {"hour": hour, "minute": minute}, "type": charge_type}, "/VehCharge")
        logger.info("veh_charge_request: %s", msg)
        self.publish(msg)
        return msg

    def publish(self, mqtt_request: MQTTRequest) -> bool:
        if not self._refresh_remote_token():
            logger.error("Can't publish %s: remote token refresh failed", mqtt_request.topic)
            return False
        message = mqtt_request.get_message_to_json(self.remoteCredentials.access_token)
        logger.debug("mqtt publish: %s %s", mqtt_request.topic, message)
        # registered before publishing: the answer can arrive before publish() returns
        self.commands.add(mqtt_request)
        tracked = getattr(self._tracked, "sent", None)
        if tracked is not None:
            tracked.append(mqtt_request.correlation_id)
        self.mqtt_client.publish(mqtt_request.topic, message)
        return True

    def mqtt_request(self, vin, req_parameters, topic):
        return MQTTRequest(topic, vin, req_parameters, self.account_info.get_mqtt_customer_id())

    def _refresh_remote_token(self, force=False) -> bool:
        with self._lock:
            bad_remote_token = self.remoteCredentials.refresh_token is None
            if not force and not bad_remote_token:
                # remoteCredentials.last_update only moves when PSA hands out a new remote
                # refresh token, which a refresh usually doesn't: also count our own refreshes,
                # or every publish refreshes and burns the refresh_token_now() rate limit.
                last_update: datetime = max(filter(None, (self.remoteCredentials.last_update,
                                                          self.remote_token_last_update)))
                if (datetime.now() - last_update).total_seconds() < MQTT_TOKEN_TTL:
                    return True
            if force and self.remote_token_last_update is not None:
                # throttle forced refreshes (e.g. one per paho reconnect attempt on rc 7):
                # a stale token makes the broker drop the socket, and paho retries at most every
                # MAX_REFRESH_DELAY seconds, so refreshing more often than that is either
                # useless (network outage: the token isn't the problem) or harmful
                # (it burns the refresh_token_now() rate limit: 6 per 30 min).
                since_last_refresh = (datetime.now() - self.remote_token_last_update).total_seconds()
                if since_last_refresh < MAX_REFRESH_DELAY:
                    logger.debug("remote token refreshed %.0fs ago, skipping forced refresh",
                                 since_last_refresh)
                    return True
            if self.manager.needs_login and not self.manager.rejected_grant_retry_due():
                logger.debug("Can't refresh remote token: waiting for a new login")
                return False
            try:
                if not self.manager.refresh_token_now():
                    logger.error("Can't refresh remote token: access token refresh failed")
                    return False
                if bad_remote_token:
                    logger.warning("remote_refresh_token isn't defined")
                else:
                    res = self.manager.post(REMOTE_URL + self.account_info.client_id,
                                            json={"grant_type": "refresh_token",
                                                  "refresh_token": self.remoteCredentials.refresh_token},
                                            headers=self.headers, timeout=TIMEOUT_IN_S)
                    data = res.json()
                    logger.debug("refresh_remote_token: %s", data)
                    if "access_token" in data:
                        self.remoteCredentials.access_token = data["access_token"]
                        bad_remote_token = False
                        if "refresh_token" in data:
                            self.remoteCredentials.refresh_token = data["refresh_token"]
                    else:
                        logger.error("can't refresh_remote_token: %s\n Create a new one", data)
                        bad_remote_token = True
                if bad_remote_token:
                    otp_code = self.get_otp_code()
                    self._get_remote_access_token(otp_code)
                self.remote_token_last_update = datetime.now()
                self.mqtt_client.username_pw_set("IMA_OAUTH_ACCESS_TOKEN", self.remoteCredentials.access_token)
                return True
            except RateLimitException as e:
                logger.error("Can't refresh remote token please wait... %s", e)
                return False
            except (RequestException, KeyError, AttributeError, RemoteException, ConfigException):
                logger.exception("Can't refresh remote token, please redo otp procedure")
                return False

    def get_sms_otp_code(self):
        url = "https://api.groupe-psa.com/applications/cvs/v4/mobile/smsCode?client_id=" + self.account_info.client_id
        try:
            res = self.manager.post(url, headers=self.headers, timeout=SMS_TIMEOUT_IN_S)
        except Timeout as e:
            logger.error("SMS code request timed out: %s", e)
            raise SmsRequestException(f"PSA didn't answer within {SMS_TIMEOUT_IN_S} seconds. Its servers are slow "
                                      "at times, please try again in a few minutes.", str(e)) from e
        except RequestException as e:
            logger.error("SMS code request failed: %s", e)
            raise SmsRequestException("PSA can't be reached. Check the internet connection of this server, "
                                      "or try again later.", str(e)) from e
        logger.info("SMS code request: HTTP %s", res.status_code)
        if not res.ok:
            logger.error("SMS code request failed: HTTP %s %s", res.status_code, res.text)
            if res.status_code in (401, 403):
                hint = "PSA refused the request. Reconnect from the User config tab first, then ask for the SMS again."
            else:
                hint = "PSA couldn't send the SMS, please try again later."
            raise SmsRequestException(hint, f"HTTP {res.status_code} {res.text}")
        return res

    # 6 otp by day
    @rate_limit(6, 3600 * 24)
    def get_otp_code(self):
        try:
            otp_code = self.otp.get_otp_code()
        except ConfigException:
            logger.exception("get_otp_code:")
            # load_otp() re-reads otp.bin from disk: it is the ONLY recovery when the pickled
            # session is desynchronized with the server. force_new would NOT reload it (it
            # always takes the "Please redo otp config" branch and returns False), so the old
            # code retried the very session that just failed.
            if not self.load_otp():
                raise
            otp_code = self.otp.get_otp_code()
        save_otp(self.otp)
        return otp_code

    def _get_remote_access_token(self, password):
        res = self.manager.post(REMOTE_URL + self.account_info.client_id,
                                json={"grant_type": "password", "password": password},
                                headers=self.headers, timeout=TIMEOUT_IN_S)
        data = res.json()
        try:
            self.remoteCredentials.access_token = data["access_token"]
            self.remoteCredentials.refresh_token = data["refresh_token"]
        except KeyError as e:
            raise RemoteException("get_remote_access_token: bad response" + str(data)) from e
        return res

    def horn(self, vin, count):
        msg = self.mqtt_request(vin, {"nb_horn": count, "action": "activate"}, "/Horn")
        logger.info(msg)
        return self.publish(msg)

    def lights(self, vin, duration: int):
        msg = self.mqtt_request(vin, {"action": "activate", "duration": duration}, "/Lights")
        logger.info(msg)
        return self.publish(msg)

    def wakeup(self, vin):
        """Ask the car to send its state; raises RateLimitException past WAKEUP_LIMIT per WAKEUP_PERIOD."""
        token = wakeup_limiter.acquire(vin)
        logger.info("ask wakeup to %s", vin)
        msg = self.mqtt_request(vin, {"action": "state"}, "/VehCharge/state")
        logger.info(msg)
        published = False
        try:
            published = self.publish(msg)
        finally:
            if not published:
                # the car wasn't woken up: don't count it
                wakeup_limiter.release(vin, token)
        return published

    def lock_door(self, vin, lock: bool):
        if lock:
            value = "lock"
        else:
            value = "unlock"

        msg = self.mqtt_request(vin, {"action": value}, "/Doors")
        logger.info(msg)
        self.publish(msg)
        return True

    def preconditioning(self, vin, activate: bool):
        if activate:
            value = "activate"
        else:
            value = "deactivate"
        programs = self._known_precond_programs(vin)
        if programs is None:
            # The command overwrites the programs stored in the car: sending the default (all unset)
            # ones would wipe the schedules set in the app (#1152). Wake the car up instead, its
            # next event reports the programs and the command can be sent again.
            try:
                self.wakeup(vin)
            except RateLimitException:
                logger.warning("can't wake up %s to get its preconditioning programs", vin)
            raise RemoteException("Preconditioning programs of the car are unknown, "
                                  "it has been asked to report them: try again in a few minutes")
        msg = self.mqtt_request(vin, {"asap": value, "programs": programs}, "/ThermalPrecond")
        logger.info("Preconditioning: %s", msg)
        self.publish(msg)
        return True

    def get_preconditioning_program(self, vin):
        programs = self._known_precond_programs(vin)
        if programs is None:
            return DEFAULT_PRECONDITIONING_PROGRAM
        return programs

    def _known_precond_programs(self, vin):
        """Programs from the last mqtt event, else the ones saved in cars.json, else the REST status, else None."""
        if vin in self.precond_programs:
            return self.precond_programs[vin]
        car = self.vehicles_list.get_car_by_vin(vin)
        if car is None:
            return None
        if car.precond_programs:
            self.precond_programs[vin] = car.precond_programs
            return car.precond_programs
        try:
            status_programs = car.status.preconditionning.air_conditioning.programs
        except AttributeError:
            status_programs = None
        if status_programs:
            return precond_programs.from_status(status_programs)
        return None

    def _store_precond_programs(self, vin, programs):
        self.precond_programs[vin] = programs
        car = self.vehicles_list.get_car_by_vin(vin)
        if car is not None and car.precond_programs != programs:
            car.precond_programs = programs
            try:
                self.vehicles_list.save_cars()
            except OSError:
                logger.exception("can't save the preconditioning programs of %s", vin)

    def set_preconditioning_program(self, vin, programs):
        # Push the schedule to the car. "asap": "deactivate" updates the stored
        # programs without starting an immediate preconditioning session. Record
        # the programs locally only once the publish succeeds, so a command that
        # never reaches the car (e.g. the remote token can't refresh) doesn't
        # leave get_preconditioning_program reporting a schedule the car never
        # received.
        msg = self.mqtt_request(vin, {"asap": "deactivate", "programs": programs}, "/ThermalPrecond")
        logger.info("set_preconditioning_program: %s", msg)
        published = self.publish(msg)
        if published:
            self._store_precond_programs(vin, programs)
        return published

    def load_otp(self, force_new=False):
        otp_session = load_otp()
        if otp_session is None or force_new:
            logger.error("Please redo otp config")
            return False
        self.otp = otp_session
        return True

    def change_charge_hour(self, vin, hour, miinute):
        self.veh_charge_request(vin, hour, miinute, DELAYED_CHARGE)
        return True

    def charge_now(self, vin, now):
        if now:
            charge_type = IMMEDIATE_CHARGE
        else:
            charge_type = DELAYED_CHARGE
        try:
            hour, minute = self.get_charge_hour(vin)
        except (AttributeError, TypeError):
            # The command has to carry a charge hour and the API didn't send the one of the car: a
            # stop mustn't fail because of that. The hour just passed delays the charge the longest.
            hour, minute = (datetime.now().hour - 1) % 24, 0
            logger.warning("Charge hour of %s is unknown, setting it to %d:00", vin, hour)
        res = self.veh_charge_request(vin, hour, minute, charge_type)
        logger.info("charge_now: %s", res)
        return True

    def get_charge_hour(self, vin):
        hour_str = self.vehicles_list.get_car_by_vin(vin).status.get_energy('Electric').charging.next_delayed_time
        try:
            return parse_hour(hour_str)[:2]
        except IndexError:
            logger.exception("Can't get charge hour: %s", hour_str)
            return None
