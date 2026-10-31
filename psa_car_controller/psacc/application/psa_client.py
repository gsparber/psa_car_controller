import json
import threading
from copy import copy
from datetime import datetime, timedelta, timezone
from json import JSONEncoder
from hashlib import md5
from sqlite3.dbapi2 import IntegrityError
from typing import Optional

from oauth2_client.credentials_manager import ServiceInformation
from urllib3.exceptions import HTTPError

from psa_car_controller.psa.connected_car_api.api.vehicles_api import VehiclesApi
from psa_car_controller.psa.connected_car_api.rest import ApiException
from psa_car_controller.psacc.model.car import Cars, Car, EMPTY_STATUS_LIMIT
from psa_car_controller.psacc.model.car_status import status_updated_at
from psa_car_controller.psacc.application.charging import Charging
from psa_car_controller.psacc.application.command_refresh import CommandStatusRefresh
from psa_car_controller.psacc.application.vehicle_services import VehicleServices
from psa_car_controller.psa.AccountInformation import AccountInformation
from psa_car_controller.psa.RemoteClient import RemoteClient
from psa_car_controller.psa.RemoteCredentials import RemoteCredentials
from psa_car_controller.psa.extra_api import API_ROOT, ExtraApi
from psa_car_controller.psa.oauth import AUTH_LOGIN_REQUIRED, AUTH_TEMPORARY, AUTH_TEMPORARY_MESSAGE, \
    OpenIdCredentialManager, Oauth2PSACCApiConfig, OauthAPIClient
from .ecomix import Ecomix
from psa_car_controller.psa.constants import realm_info, AUTHORIZE_SERVICE, INPROGRESS

from .abrp import Abrp
from psa_car_controller.psacc.repository.db import Database
from psa_car_controller.common.mylogger import CustomLogger
from psa_car_controller.common.sensitive_filter import sensitive_filter
from psa_car_controller.common.utils import AuthenticationRequiredException, RateLimitException, atomic_write

SCOPE = ['openid profile']
CARS_FILE = "cars.json"
DEFAULT_CONFIG_FILENAME = "config.json"

logger = CustomLogger.getLogger(__name__)

# (HTTP status, API error code) of status requests that fail for a moment
# 40400: no status found for the car, 50038: the API couldn't resolve the account's cars
TRANSIENT_STATUS_ERRORS = {(404, "40400"), (500, "50038")}
# the API can't reach the car's connection module, seen after it was replaced
CONNECTION_MODULE_ERROR = (500, "50000")
TOO_MANY_REQUESTS = 429

# seconds between two status refreshes, PSA blocks accounts that poll too often
DEFAULT_INFO_REFRESH_RATE = 300
# while a car charges, charge control needs a fresh battery level to stop at the threshold
CHARGING_REFRESH_RATE = 120
# longest wait when PSA keeps answering that there were too many requests
MAX_REFRESH_DELAY = 3600
# seconds between the status requests of two cars of the account, not all at once
CAR_STAGGER_DELAY = 15


def api_error_code(ex: ApiException):
    """Error code in the body of a failed API request: {"code": 40400, "message": ...}"""
    try:
        return str(json.loads(ex.body)["code"])
    except (TypeError, ValueError, KeyError):
        return None


class PSAClient:
    info_refresh_rate = DEFAULT_INFO_REFRESH_RATE
    charging_refresh_rate = CHARGING_REFRESH_RATE
    # delay until the next refresh, as scheduled
    refresh_delay = DEFAULT_INFO_REFRESH_RATE
    _refresh_started = False
    _rate_limited = False
    _rate_limit_backoff = 0
    _stop_event: threading.Event = None
    _save_lock = threading.Lock()
    # seconds waited between the cars during the current refresh
    _staggered = 0
    command_refresh: CommandStatusRefresh = None
    vehicle_services: VehicleServices = None

    def connect(self, code: str):
        self.manager.connect_with_code(code)

    # pylint: disable=too-many-arguments,too-many-positional-arguments
    def __init__(self, refresh_token, client_id, client_secret, remote_refresh_token, customer_id, realm, country_code,
                 brand=None, proxies=None, weather_api=None, abrp=None, co2_signal_api=None):
        self.realm = realm
        self.service_information = ServiceInformation(AUTHORIZE_SERVICE[self.realm],
                                                      realm_info[self.realm]['oauth_url'],
                                                      client_id,
                                                      client_secret,
                                                      SCOPE, True)
        self.client_id = client_id
        self.country_code = country_code
        self.manager = OpenIdCredentialManager.create(self.service_information,
                                                      realm_info[self.realm]["scheme"], self.country_code)
        self.api_config = Oauth2PSACCApiConfig()
        self.api_config.set_refresh_callback(self._refresh_api_token)
        self.manager.refresh_token = refresh_token
        self.account_info = AccountInformation(client_id, customer_id, realm, country_code)
        self.remote_access_token = None
        self.vehicles_list = Cars.load_cars(CARS_FILE)
        self.customer_id = customer_id
        self._config_hash = None
        self.api_config.verify_ssl = True
        self.api_config.api_key['client_id'] = self.client_id
        self.api_config.api_key['x-introspect-realm'] = self.realm
        self.remote_token_last_update = None
        self._record_enabled = False
        self.weather_api = weather_api
        self.brand = brand
        self.info_callback = []
        if abrp is None:
            self.abrp = Abrp()
        else:
            self.abrp: Abrp = Abrp(**abrp)
        self.set_proxies(proxies)
        self.config_file = DEFAULT_CONFIG_FILENAME
        Ecomix.co2_signal_key = co2_signal_api
        self.refresh_thread: threading.Timer = None
        self._stop_event = threading.Event()
        remote_credentials = RemoteCredentials(remote_refresh_token)
        remote_credentials.update_callbacks.append(self.save_config)
        self.remote_client = RemoteClient(self.account_info,
                                          self.vehicles_list,
                                          self.manager,
                                          remote_credentials)
        # show the result of a command without waiting for the next regular refresh
        self.command_refresh = CommandStatusRefresh(self._refresh_after_command, self._is_rate_limited)
        self.remote_client.command_callbacks.append(self.command_refresh.on_command)
        self.vehicle_services = VehicleServices(lambda: ExtraApi(self.root_api_client()))
        sensitive_filter.add_source(self.sensitive_values)

    def sensitive_values(self):
        remote_credentials = self.remote_client.remoteCredentials
        return [self.manager.access_token, self.manager.refresh_token,
                remote_credentials.access_token, remote_credentials.refresh_token,
                self.service_information.client_secret,
                # the MQTT customer id only differs from customer_id by its brand prefix
                self.customer_id, self.customer_id and self.customer_id[2:],
                self.abrp.token, self.abrp.api_key, self.weather_api, Ecomix.co2_signal_key] + \
            [car.vin for car in self.vehicles_list] + [car.vehicle_id for car in self.vehicles_list]

    def get_app_name(self):
        return realm_info[self.realm]['app_name']

    def is_authenticated(self) -> bool:
        return bool(self.manager.access_token) and not self.manager.needs_login

    def auth_problem(self) -> Optional[str]:
        """Why the API can't be used: None if it can, else AUTH_TEMPORARY or AUTH_LOGIN_REQUIRED."""
        if self.is_authenticated():
            return None
        return AUTH_TEMPORARY if self.manager.refresh_failure_is_temporary() else AUTH_LOGIN_REQUIRED

    def _refresh_api_token(self, rejected_access_token: str) -> bool:
        """Refresh after a 401, and hand the new token to the request that is sent again."""
        if not self.manager.refresh_token_now(rejected_access_token):
            return False
        self.api_config.access_token = self.manager.access_token
        return True

    def _check_access_token(self):
        if self.manager.rejected_grant_retry_due():
            try:
                self.manager.refresh_token_now()
            except RateLimitException as e:
                logger.debug("Can't retry the rejected refresh token now: %s", e)
        if not self.is_authenticated():
            if self.auth_problem() == AUTH_TEMPORARY:
                raise AuthenticationRequiredException(AUTH_TEMPORARY_MESSAGE)
            # don't call PSA with a token it already refused, until a new login
            raise AuthenticationRequiredException()
        self.api_config.access_token = self.manager.access_token

    def api(self) -> VehiclesApi:
        self._check_access_token()
        api_instance = VehiclesApi(OauthAPIClient(self.api_config))
        return api_instance

    def root_api_client(self) -> OauthAPIClient:
        """Client for the endpoints outside /connectedcar/v4 (vehicle rights), with the same credentials."""
        self._check_access_token()
        config = copy(self.api_config)
        config.host = API_ROOT

        def refresh(rejected_access_token):
            if not self._refresh_api_token(rejected_access_token):
                return False
            config.access_token = self.manager.access_token
            return True

        config.set_refresh_callback(refresh)
        return OauthAPIClient(config)

    def set_proxies(self, proxies):
        if proxies is None:
            proxies = {"http": '', "https": ''}
            self.api_config.proxy = None
        else:
            self.api_config.proxy = proxies['http']
            self.abrp.proxies = proxies
        self.manager.proxies = proxies

    def get_vehicle_info(self, vin, cache=False):
        car = self.vehicles_list.get_car_by_vin(vin)
        if cache and car.status is not None:
            return car.status
        for _ in range(0, 2):
            try:
                res = self.api().get_vehicle_status(car.vehicle_id)
            except ApiException as ex:
                if ex.status == TOO_MANY_REQUESTS:
                    # asking again makes it worse, the refresh loop waits longer instead
                    logger.warning("Too many requests to the API, can't get the status of %s", car.vin)
                    self._rate_limited = True
                    break
                if self._log_status_error(car, ex):
                    continue
                break
            except HTTPError as ex:
                logger.error("get_vehicle_info: %s", ex, exc_info_debug=True)
                continue
            if res is not None:
                if car.status_outdated:
                    logger.info("Status of %s received again", car.vin)
                car.status_request_succeeded()
                self._set_status(car, res)
                return car.status
        # keep the last status: the API often fails for a moment, and an empty one breaks the UI and charge control
        failures = car.status_request_failed()
        if car.status is not None and failures == EMPTY_STATUS_LIMIT:
            logger.error("No status of %s from the API for %d requests, the last one is outdated", car.vin, failures)
        return car.status

    def _is_rate_limited(self) -> bool:
        return self._rate_limited or self._rate_limit_backoff > 0

    def _refresh_after_command(self, vin):
        try:
            self.get_vehicle_info(vin)
        except RateLimitException as e:
            logger.warning("status refresh after a command: %s", e)
            self._rate_limited = True
        except AuthenticationRequiredException:
            logger.debug("status refresh after a command: not connected")

    @staticmethod
    def _log_status_error(car: Car, ex: ApiException) -> bool:
        """Log a failed status request, return whether asking again right away can help."""
        error = (ex.status, api_error_code(ex))
        if error in TRANSIENT_STATUS_ERRORS:
            logger.warning("The API has no status of %s right now (HTTP %s, code %s), keeping the last one",
                           car.vin, *error)
            return False
        if error == CONNECTION_MODULE_ERROR:
            logger.error("The API can't reach the connection module of %s (HTTP 500, code 50000): it may have been "
                         "replaced, remove the car from the brand app and add it again", car.vin)
            return False
        logger.error("get_vehicle_info: ApiException: %s", ex, exc_info_debug=True)
        return True

    def _set_status(self, car: Car, status):
        old_date = status_updated_at(car.status) if car.status is not None else None
        new_date = status_updated_at(status)
        newer = True
        if old_date is not None and new_date is not None:
            try:
                if new_date < old_date:
                    # the API sometimes answers with an older cached status, it would undo the newer one
                    logger.debug("Ignore status of %s from %s, older than the current one from %s",
                                 car.vin, new_date, old_date)
                    return
                newer = new_date > old_date
            except TypeError:  # naive and aware dates can't be compared
                pass
        car.status = status
        # a new status wipes anything mqtt filled in, so put it back
        self.remote_client.apply_mqtt_state(car)
        # the same status again would only record the same data again
        if self._record_enabled and newer:
            try:
                self.record_info(car)
            except Exception:  # pylint: disable=broad-except
                # the status itself is fine: the caller, the charge control and the next cars still need it
                logger.exception("record_info of %s: ", car.vin)

    def __refresh_vehicle_info(self):
        if self.info_refresh_rate is None:
            return
        logger.debug("refresh_vehicle_info")
        self._staggered = 0
        try:
            for index, car in enumerate(self.vehicles_list):
                if index > 0 and not self._wait_before_next_car():
                    return  # stopped meanwhile
                self._refresh_car(car)
                if self._rate_limited:  # the next car would be refused too
                    break
        except RateLimitException as e:
            logger.warning("refresh_vehicle_info: %s", e)
            self._rate_limited = True
        except AuthenticationRequiredException:
            # already reported when the token was rejected, no traceback every refresh
            logger.debug("refresh_vehicle_info: not connected")
        except BaseException:
            logger.exception("refresh_vehicle_info: ")
        self._refresh_vehicle_services()
        # scheduled once the status is known: the delay depends on it
        self.__schedule_refresh()
        self._run_info_callbacks()

    def _refresh_car(self, car: Car):
        try:
            self.get_vehicle_info(car.vin)
        except (RateLimitException, AuthenticationRequiredException):
            raise  # the same for the next car
        except Exception:  # pylint: disable=broad-except
            logger.exception("refresh_vehicle_info of %s: ", car.vin)

    def _run_info_callbacks(self):
        for callback in list(self.info_callback):
            try:
                callback()
            except AuthenticationRequiredException:
                logger.debug("refresh_vehicle_info: not connected")
            except BaseException:
                logger.exception("refresh_vehicle_info: ")

    def _car_stagger_delay(self) -> float:
        """Wait between two cars, short enough for every car to be asked before the next refresh."""
        shortest_rate = min(self.info_refresh_rate, self.charging_refresh_rate)
        return min(CAR_STAGGER_DELAY, shortest_rate / len(self.vehicles_list))

    def _wait_before_next_car(self) -> bool:
        """Return False when the client was stopped during the wait."""
        if self._stop_event is None:
            self._stop_event = threading.Event()
        delay = self._car_stagger_delay()
        if self._stop_event.wait(delay) or self.info_refresh_rate is None:
            return False
        self._staggered += delay
        return True

    def _refresh_vehicle_services(self):
        """Maintenance and rights, they are only asked for once a day: no extra request at each refresh."""
        if self.vehicle_services is None or self._is_rate_limited():
            return
        try:
            for car in self.vehicles_list:
                if not self.vehicle_services.refresh(car):
                    self._rate_limited = True
                    break
        except BaseException:  # pylint: disable=broad-except
            logger.exception("refresh_vehicle_services: ")

    def get_stellantis_trips(self, car: Car, since=None, max_pages=1):
        """Trips as Stellantis recorded them, apart from the ones computed from the recorded positions."""
        if self._is_rate_limited():
            raise RateLimitException("stellantis trips")
        try:
            return self.vehicle_services.trips(car.vehicle_id, since, max_pages)
        except ApiException as ex:
            if ex.status == TOO_MANY_REQUESTS:
                # the status refresh waits longer too
                self._rate_limited = True
                raise RateLimitException("stellantis trips") from ex
            raise

    def __schedule_refresh(self):
        if self.info_refresh_rate is None:  # stopped meanwhile
            return
        if self.refresh_thread is not None and self.refresh_thread is not threading.current_thread():
            logger.debug("refresh_vehicle_info: precedent task still alive")
            self.refresh_thread.cancel()
        # the wait between the cars is part of the delay: each car is still asked once per delay
        self.refresh_delay = max(self._next_refresh_delay() - self._staggered, 0)
        self.refresh_thread = threading.Timer(self.refresh_delay, self.__refresh_vehicle_info)
        self.refresh_thread.daemon = True
        self.refresh_thread.start()

    def _next_refresh_delay(self) -> float:
        if self._rate_limited:
            self._rate_limited = False
            self._rate_limit_backoff += 1
            delay = max(min(self.info_refresh_rate * 2 ** self._rate_limit_backoff, MAX_REFRESH_DELAY),
                        self.info_refresh_rate)
            logger.warning("PSA rate limit reached, next status refresh in %d s", delay)
            return delay
        self._rate_limit_backoff = 0
        if any(self._is_charging(car) for car in self.vehicles_list):
            return min(self.info_refresh_rate, self.charging_refresh_rate)
        return self.info_refresh_rate

    @staticmethod
    def _is_charging(car: Car) -> bool:
        try:
            return car.status.get_energy('Electric').charging.status == INPROGRESS
        except AttributeError:  # no status yet, or a car without battery
            return False

    def start_refresh_thread(self):
        if not self._refresh_started:
            self._refresh_started = True
            if len(self.vehicles_list) > 1:
                # the wait between the cars mustn't hold up the start
                thread = threading.Thread(target=self.__refresh_vehicle_info, daemon=True)
                thread.start()
            else:
                self.__refresh_vehicle_info()

    def stop(self):
        """Stop polling PSA and the remote control, for a client that is being replaced."""
        # no refresh rate: a refresh running right now doesn't schedule the next one either
        self.info_refresh_rate = None
        if self._stop_event is not None:  # ends the wait between two cars
            self._stop_event.set()
        if self.refresh_thread is not None:
            self.refresh_thread.cancel()
        self.manager.stop_scheduled_refresh()
        if self.command_refresh is not None:
            self.command_refresh.stop()
        self.remote_client.stop()

    def get_vehicles(self):
        try:
            res = self.api().get_vehicles_by_device()
            for vehicle in res.embedded.vehicles:
                self.vehicles_list.add(Car(vehicle.vin, vehicle.id, vehicle.brand, vehicle.label))
                if self.vehicle_services is not None:
                    self.vehicle_services.set_maintenance_href(
                        vehicle.vin, getattr(getattr(vehicle.links, "maintenance", None), "href", None))
            self.vehicles_list.save_cars()
        except (ApiException, HTTPError):
            logger.exception("get_vehicles:")
        return self.vehicles_list

    def get_charge_status(self, vin):
        data = self.get_vehicle_info(vin)
        status = data.get_energy('Electric').charging.status
        return status

    def save_config(self, name=None, force=False):
        if name is None:
            name = self.config_file
        with self._save_lock:  # read and write in one go: a slower save must not put older tokens back
            config = json.loads(json.dumps(self, cls=PSAClientEncoder))
            if not config["refresh_token"]:
                # null would lose the login for good, while a stored token might still be accepted
                config["refresh_token"] = self._stored_refresh_token(name)
                if config["refresh_token"]:
                    logger.warning("No refresh token in memory, keeping the one in %s", name)
            config_str = json.dumps(config, sort_keys=True, indent=4).encode("utf8")
            new_hash = md5(config_str).hexdigest()
            if force or self._config_hash != new_hash:
                atomic_write(name, config_str)
                self._config_hash = new_hash
                logger.info("save config change")

    @staticmethod
    def _stored_refresh_token(name) -> Optional[str]:
        try:
            with open(name, "r", encoding="utf-8") as f:
                return json.load(f).get("refresh_token")
        except (OSError, ValueError, AttributeError):
            # no config yet, or not one we can read
            return None

    @staticmethod
    def load_config(name="config.json"):
        with open(name, "r", encoding="utf-8") as f:
            config_str = f.read()
            config = {**json.loads(config_str)}
            if "country_code" not in config:
                config["country_code"] = input("What is your country code ? (ex: FR, GB, DE, ES...)\n")
            for new_el in ["abrp", "co2_signal_api"]:
                if new_el not in config:
                    config[new_el] = None
            psacc = PSAClient(**config)
            psacc.config_file = name
            return psacc

    def set_record(self, value: bool):
        self._record_enabled = value

    def record_info(self, car: Car):  # pylint: disable=too-many-locals
        mileage = car.status.timed_odometer.mileage
        level = car.status.get_energy('Electric').level
        level_fuel = car.status.get_energy('Fuel').level
        if car.is_thermal():
            charge_date = car.status.get_energy('Fuel').updated_at
        else:
            charge_date = car.status.get_energy('Electric').updated_at
        moving = car.status.kinetic.moving

        longitude = car.status.last_position.geometry.coordinates[0]
        latitude = car.status.last_position.geometry.coordinates[1]
        altitude = car.status.last_position.geometry.coordinates[2]
        date = car.status.last_position.properties.updated_at
        if date is None or date < datetime.now(timezone.utc) - timedelta(days=1):  # if position isn't updated
            date = charge_date

        temp = getattr(getattr(getattr(car.status, "environment", None), "air", None), "temp", None)

        logger.debug("vin:%s date:%s mileage:%s level:%s charge_date:%s level_fuel:%s moving:%s temp:%s",
                     car.vin, date, mileage, level, charge_date, level_fuel, moving, temp)
        Database.record_position(self.weather_api, car.vin, mileage, latitude, longitude, altitude, date, level,
                                 level_fuel, moving, temp)
        self.abrp.call(car, Database.get_last_temp(car.vin))
        if car.has_battery():
            electric_energy_status = car.status.get_energy('Electric')
            try:
                charging_status = electric_energy_status.charging.status
                charging_mode = electric_energy_status.charging.charging_mode
                charging_rate = electric_energy_status.charging.charging_rate
                autonomy = electric_energy_status.autonomy
                Charging.record_charging(car, charging_status, charge_date, level, latitude, longitude,
                                         self.country_code,
                                         charging_mode, charging_rate, autonomy, mileage)
                logger.debug("charging_status:%s ", charging_status)
            except AttributeError as ex:
                logger.error("charging status not available from api")
                logger.debug(ex)
            try:
                soh = electric_energy_status.battery.health.resistance
                Database.record_battery_soh(car.vin, charge_date, soh)
            except IntegrityError:
                logger.debug("SOH already recorded")
            except AttributeError as ex:
                logger.debug("Failed to record SOH: %s", ex)

    def __iter__(self):
        for key, value in self.__dict__.items():
            yield key, value


class PSAClientEncoder(JSONEncoder):

    def default(self, mp: PSAClient):  # pylint: disable=arguments-renamed
        mpd = {"proxies": mp.manager.proxies,
               "refresh_token": mp.manager.refresh_token,
               "client_secret": mp.service_information.client_secret,
               "abrp": dict(mp.abrp),
               "remote_refresh_token": mp.remote_client.remoteCredentials.refresh_token,
               "customer_id": mp.account_info.customer_id,
               "client_id": mp.account_info.client_id,
               "realm": mp.account_info.realm,
               "country_code": mp.account_info.country_code,
               "weather_api": mp.weather_api,
               "co2_signal_api": Ecomix.co2_signal_key
               }
        return mpd
