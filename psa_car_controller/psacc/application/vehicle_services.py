"""Maintenance, subscription rights and Stellantis trips of each car, asked for rarely and kept in memory.

Their values change at most daily: maintenance and rights are asked for with the first status refresh, then
once a day. An endpoint the account doesn't have (404/403/400) or an empty maintenance answer turns the
feature off until the next start, any other failure is retried after RETRY_DELAY. Nothing here may stop the
status refresh: every failure is logged and swallowed.
"""
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

from urllib3.exceptions import HTTPError

from psa_car_controller.common.utils import AuthenticationRequiredException, RateLimitException
from psa_car_controller.psa.connected_car_api.rest import ApiException
from psa_car_controller.psa.extra_api import ExtraApi

logger = logging.getLogger(__name__)

REFRESH_PERIOD = 24 * 3600
RETRY_DELAY = 3600
TOO_MANY_REQUESTS = 429
# the account or car has no such endpoint: asking again won't change it
UNSUPPORTED_STATUSES = (400, 403, 404)
# same trips request within this delay: answered from memory
TRIPS_CACHE_S = 600
MAX_TRIPS_PAGES = 10


def parse_rights(response) -> dict:
    """{service code: {"name", "mqtt_services"}} of a rights answer, its services are grouped per telematic unit."""
    features = {}
    units = response.get("services") if isinstance(response, dict) else None
    if not isinstance(units, dict):
        return features
    for services in units.values():
        for service in services if isinstance(services, list) else []:
            if not isinstance(service, dict) or not service.get("code") or service["code"] in features:
                continue
            mqtt_services = service.get("mqtt_services")
            features[service["code"]] = {"name": service.get("name"),
                                         "mqtt_services": mqtt_services if isinstance(mqtt_services, list) else []}
    return features


def maintenance_values(response) -> Optional[dict]:
    if not isinstance(response, dict):
        return None
    values = {"mileage": response.get("mileageBeforeMaintenance"),
              # the specification spells it "daysBeforeMaintenace", the API as the brand apps read it
              "days": response.get("daysBeforeMaintenance", response.get("daysBeforeMaintenace")),
              "updated_at": response.get("updatedAt") or response.get("createdAt")}
    if values["mileage"] is None and values["days"] is None:
        return None
    return values


@dataclass
class _Lookup:
    """One endpoint for one car: its last answer and when to ask again."""
    value: Optional[dict] = None
    next_at: float = 0
    unsupported: bool = False

    def due(self, now) -> bool:
        return not self.unsupported and now >= self.next_at


@dataclass
class _CarServices:
    maintenance_href: Optional[str] = None
    maintenance: _Lookup = field(default_factory=_Lookup)
    rights: _Lookup = field(default_factory=_Lookup)


class VehicleServices:
    def __init__(self, api: Callable[[], ExtraApi], clock=time.time):
        self.api = api
        self.clock = clock
        self._cars = {}
        self._trips_cache = {}
        self._lock = threading.Lock()

    def _car(self, vin) -> _CarServices:
        with self._lock:
            return self._cars.setdefault(vin, _CarServices())

    def set_maintenance_href(self, vin, href):
        if href:
            self._car(vin).maintenance_href = href

    def refresh(self, car):
        """Ask for what is due. Returns False after a 429, then nothing else should be asked for now."""
        services = self._car(car.vin)
        now = self.clock()
        if services.maintenance_href and services.maintenance.due(now):
            if not self._lookup(car.vin, "maintenance", services.maintenance,
                                lambda api: api.get_maintenance(services.maintenance_href), maintenance_values):
                return False
        if services.rights.due(now):
            if not self._lookup(car.vin, "rights", services.rights, lambda api: api.get_rights(car.vin),
                                parse_rights):
                return False
        return True

    def _lookup(self, vin, name, lookup: _Lookup, request, parse) -> bool:
        # pylint: disable=too-many-arguments,too-many-positional-arguments
        now = self.clock()
        lookup.next_at = now + RETRY_DELAY
        try:
            value = parse(request(self.api()))
        except ApiException as ex:
            if ex.status == TOO_MANY_REQUESTS:
                logger.warning("Too many requests to the API, %s of %s asked for later", name, vin)
                return False
            if ex.status in UNSUPPORTED_STATUSES:
                logger.info("No %s for %s from the API (HTTP %s), not asking again", name, vin, ex.status)
                lookup.unsupported = True
            else:
                logger.warning("Can't get the %s of %s: HTTP %s %s", name, vin, ex.status, ex.reason)
            return True
        except RateLimitException as ex:
            logger.warning("Can't get the %s of %s: %s", name, vin, ex)
            return False
        except AuthenticationRequiredException:
            logger.debug("%s of %s: not connected", name, vin)
            return True
        except (HTTPError, ValueError, TypeError, AttributeError) as ex:
            logger.warning("Can't get the %s of %s: %s", name, vin, ex)
            return True
        if value is None:
            # an empty answer: the car has no such data, don't poll it
            logger.info("No %s data for %s, not asking again", name, vin)
            lookup.unsupported = True
            return True
        lookup.value = value
        lookup.next_at = now + REFRESH_PERIOD
        logger.debug("%s of %s: %s", name, vin, value)
        return True

    def maintenance(self, vin) -> Optional[dict]:
        return self._car(vin).maintenance.value

    def rights(self, vin) -> Optional[dict]:
        """Services the API lists for the car, for information only: no command depends on them.

        Which code a command needs is unknown. Neither the code names nor "mqtt_services" tell it (preconditioning
        works without "RemoteServices"), and no report shows a car refusing a command next to its rights. Without
        that, a missing code proves nothing, so the car decides: it answers "not compatible" to what it doesn't cover.
        """
        return self._car(vin).rights.value

    def to_dict(self, vin) -> dict:
        return {"maintenance": self.maintenance(vin), "rights": self.rights(vin)}

    def trips(self, vehicle_id, since: Optional[datetime] = None, max_pages=MAX_TRIPS_PAGES):
        """Trips the car recorded, as Stellantis lists them: {"trips": [...], "more": bool}. Raises ApiException."""
        key = (vehicle_id, since, max_pages)
        now = self.clock()
        cached = self._trips_cache.get(key)
        if cached and now - cached[0] < TRIPS_CACHE_S:
            return cached[1]
        trips, more = self.api().get_trips(vehicle_id, since, max_pages)
        result = {"trips": trips, "more": more}
        with self._lock:
            # one entry per request: they only differ by the first date asked for
            self._trips_cache = {k: v for k, v in self._trips_cache.items() if now - v[0] < TRIPS_CACHE_S}
            self._trips_cache[key] = (now, result)
        return result
