import json
import logging
import time
from datetime import datetime
from json.decoder import JSONDecodeError

import requests

from psa_car_controller.psacc.model.car import Car

logger = logging.getLogger(__name__)
TIMEOUT_IN_S = 10
# each try holds up the refresh of the cars until the timeout, and the car status comes again soon anyway
RETRY_DELAY_IN_S = 300


class Abrp:
    api_key = "1e28ad14-df16-49f0-97da-364c9154b44a"
    url = "https://api.iternio.com/1/tlm/send"

    def __init__(self, token: str = "", abrp_enable_vin=None):
        if abrp_enable_vin is None:
            abrp_enable_vin = []
        self.token = token
        self.abrp_enable_vin = set(abrp_enable_vin)
        self.proxies = None
        self._retry_at = None

    def enable_abrp(self, vin, enable):
        if enable:
            self.abrp_enable_vin.add(vin)
        else:
            self.abrp_enable_vin.discard(vin)

    def _is_paused(self) -> bool:
        return self._retry_at is not None and time.monotonic() < self._retry_at

    def _failed(self, reason):
        if self._retry_at is None:  # it can last for hours: tell it once
            logger.warning("ABRP is unavailable (%s), trying again every %d s", reason, RETRY_DELAY_IN_S)
        self._retry_at = time.monotonic() + RETRY_DELAY_IN_S

    def _succeeded(self):
        if self._retry_at is not None:
            logger.info("ABRP is available again")
        self._retry_at = None

    def call(self, car: Car, ext_temp: float = None):
        try:
            if self.token is None or len(self.token) == 0:
                logger.debug("No abrp token provided")
            elif car.vin in self.abrp_enable_vin:
                if self._is_paused():
                    logger.debug("ABRP is unavailable, not asking it now")
                    return False
                energy = car.status.get_energy('Electric')

                if energy.level is None:
                    logger.debug("No energy level available")
                    return False

                tlm = {"utc": int(datetime.timestamp(energy.updated_at)),
                       "soc": energy.level,
                       "speed": getattr(car.status.kinetic, "speed", None),
                       "car_model": car.get_abrp_name(),
                       "current": car.status.battery.current,
                       "is_charging": energy.charging.status == "InProgress",
                       "lat": car.status.last_position.geometry.coordinates[1],
                       "lon": car.status.last_position.geometry.coordinates[0],
                       "power": energy.consumption
                       }
                if ext_temp is not None:
                    tlm["ext_temp"] = ext_temp
                params = {"tlm": json.dumps(tlm), "token": self.token, "api_key": self.api_key}
                response = requests.request("POST", self.url, params=params, proxies=self.proxies,
                                            verify=self.proxies is None, timeout=TIMEOUT_IN_S)
                logger.debug(response.text)
                try:
                    result = json.loads(response.text)["status"] == "ok"
                except (JSONDecodeError, KeyError, TypeError):
                    self._failed(f"bad response, HTTP {response.status_code}")
                    return False
                self._succeeded()
                return result
        except requests.RequestException as e:
            # only the type: the text holds the url with the position, the token and the api key
            self._failed(type(e).__name__)
        except Exception:  # pylint: disable=broad-except
            # a problem with this data must not stop the recording of the charge
            logger.exception("abrp:")
        return False

    def __iter__(self):
        yield "abrp_enable_vin", list(self.abrp_enable_vin)
        yield "token", self.token
