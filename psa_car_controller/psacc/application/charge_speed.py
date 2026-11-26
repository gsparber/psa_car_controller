"""Charging power from the charging rate the car sends.

The rate is km of range per hour. The car computes it from the power with a fixed energy per km: on a Mokka-e
64 km/h was about 10.7 kW into the battery at any level (Oct/Nov 2026, from the levels of 15 charges). The energy
a km takes according to the range shown (battery x level / range) is not that one: at a low level the range is
small, and 64 km/h at 22 % with 44 km came out as 14.7 kW, more than the 11 kW the car can take at all. So the
energy per km of rate is learned from the levels the last charges gained while the car sent a rate."""
import logging
import math
import threading
from time import monotonic
from typing import Iterable, List, Optional, Tuple

from psa_car_controller.psacc.model.battery_curve import BatteryCurveDto
from psa_car_controller.psacc.repository.db import Database
from psa_car_controller.psacc.repository.trips import as_utc

logger = logging.getLogger(__name__)

LEARN_CHARGES = 10
# with at least this many % over them, else not learned: a level is a whole %
MIN_LEARN_LEVELS = 10
# steps between two reports further apart than this say nothing about the rate, the car didn't report in between
MAX_LEARN_GAP_MINUTES = 60
# kWh per km of rate a car can have: an electric car takes 0.12 to 0.3 kWh per km with its losses
KWH_PER_RATE_KM_RANGE = (0.08, 0.4)
# how long a learned value is used before the charges are read again
CACHE_SECONDS = 15 * 60

_cache = {}
_lock = threading.Lock()


def _positive(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def learn(curves: Iterable[List[BatteryCurveDto]], battery_kwh) -> Tuple[Optional[float], str]:
    """(kWh per km of charging rate, how it was learned): the energy of the % gained between two reports, over the km
    the rate of the first one promised for that time. A step without a new level counts too, its time is part of
    the charge: skipping it would make the power higher than it was. (None, why not) when too little is recorded."""
    if not _positive(battery_kwh):
        return None, "no battery size"
    gained = rate_km = 0.0
    charges = 0
    for curve in curves:
        used = False
        for start, end in zip(curve, curve[1:]):
            try:
                minutes = (as_utc(end.date) - as_utc(start.date)).total_seconds() / 60
                gain = end.level - start.level
            except (AttributeError, TypeError):
                continue
            if not _positive(start.rate) or not 0 < minutes <= MAX_LEARN_GAP_MINUTES or gain < 0:
                continue
            gained += gain
            rate_km += start.rate * minutes / 60
            used = True
        charges += used
    if gained < MIN_LEARN_LEVELS or rate_km <= 0:
        return None, f"not learned, {gained:.0f} % recorded"
    value = gained / 100 * battery_kwh / rate_km
    low, high = KWH_PER_RATE_KM_RANGE
    if not low <= value <= high:
        return None, f"not learned, {value:.3f} kWh per km is no car"
    return value, f"learned from {gained:.0f} % in {charges} charges"


def kwh_per_rate_km(vin, battery_kwh) -> Optional[float]:
    """The learned kWh per km of charging rate of the car, None when its charges don't tell. Read again every
    CACHE_SECONDS, so a new charge counts soon without reading the database on every page."""
    key = (vin, battery_kwh)
    now = monotonic()
    with _lock:
        cached = _cache.get(key)
        if cached and cached[0] > now:
            return cached[1]
    try:
        curves = Database.get_last_battery_curves(vin, LEARN_CHARGES)
    except Exception as e:  # pylint: disable=broad-except
        # no database, or none yet: the power is estimated from the range then
        logger.debug("energy per km of charging rate not learned: %s", e)
        curves = []
    value, source = learn(curves, battery_kwh)
    logger.debug("energy per km of charging rate: %s (%s)", value, source)
    with _lock:
        _cache[key] = (now + CACHE_SECONDS, value)
    return value


def clear_cache():
    with _lock:
        _cache.clear()


def power_kw(rate_kmh, battery_kwh, level, range_km, kwh_per_km=None) -> Optional[float]:
    """Charging power in kW from the charging rate in km/h: with the learned kWh per km of rate, else with the energy
    a km currently takes (battery size x level / range). None when a value is missing or zero."""
    if not _positive(rate_kmh):
        return None
    if _positive(kwh_per_km):
        return rate_kmh * kwh_per_km
    if not all(_positive(v) for v in (battery_kwh, level, range_km)):
        return None
    return rate_kmh * battery_kwh * level / 100 / range_km
