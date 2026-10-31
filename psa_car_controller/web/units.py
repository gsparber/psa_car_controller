"""Display units. The database, the API and the config always use metric values (km, km/h, kWh/100km); pages
convert them here, right before showing them, when the user picked miles ("length unit = mi" in config.ini)."""
import logging

logger = logging.getLogger(__name__)

KM, MI = "km", "mi"
UNITS = {KM: "Kilometres", MI: "Miles"}
KM_PER_MI = 1.609344
_ALIASES = {"km": KM, "kms": KM, "kilometer": KM, "kilometers": KM, "kilometre": KM, "kilometres": KM, "metric": KM,
            "mi": MI, "mile": MI, "miles": MI, "imperial": MI}

# trip and charge fields (Trip.get_info / Charging.get_chargings) and how they convert
TRIP_DISTANCE_FIELDS = ("distance", "measured_distance", "mileage", "speed_average")
TRIP_PER_DISTANCE_FIELDS = ("consumption_km", "consumption_fuel_km")


def normalize(unit):
    """The unit from config.ini as KM or MI; anything unknown falls back to km."""
    if isinstance(unit, str):
        value = _ALIASES.get(unit.strip().lower())
        if value:
            return value
    if unit not in (None, ""):
        logger.warning("Unknown length unit %r, using km", unit)
    return KM


def current():
    """The unit picked in config.ini; km when the config can't be read."""
    from psa_car_controller.psacc.application.car_controller import PSACarController  # pylint: disable=C0415
    try:
        return normalize(PSACarController().config.General.length_unit)
    except AttributeError:
        return KM


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def distance(km, unit):
    """A distance (or a speed in km/h, or a mileage) in the display unit; None/non numbers pass through."""
    if not _is_number(km) or normalize(unit) == KM:
        return km
    return km / KM_PER_MI


def to_km(value, unit):
    """A distance typed in the display unit back to km, e.g. for a setting stored in km."""
    if not _is_number(value) or normalize(unit) == KM:
        return value
    return value * KM_PER_MI


def per_distance(per_100km, unit):
    """A consumption per 100 km (kWh/100km, L/100km) per 100 of the display unit: 100 mi are 160.9 km, so the
    value grows."""
    if not _is_number(per_100km) or normalize(unit) == KM:
        return per_100km
    return per_100km * KM_PER_MI


def distance_label(unit):
    return normalize(unit)


def speed_label(unit):
    return "mph" if normalize(unit) == MI else "km/h"


def consumption_label(unit, energy="kWh"):
    return f"{energy}/100{normalize(unit)}"


def convert_rows(rows, unit, distance_fields=TRIP_DISTANCE_FIELDS, per_distance_fields=TRIP_PER_DISTANCE_FIELDS,
                 keep_km=()):
    """Copies of trip or charge dicts with distances and consumptions in the display unit; the input stays
    metric. The same list comes back when nothing has to change. Fields in keep_km also keep their metric value
    as "<field>_km", for filters on settings stored in km."""
    if normalize(unit) == KM or rows is None:
        return rows
    converted = []
    for row in rows:
        row = dict(row)
        for key in keep_km:
            if key in row:
                row[f"{key}_km"] = row[key]
        for key in distance_fields:
            if key in row:
                row[key] = distance(row[key], unit)
        for key in per_distance_fields:
            if key in row:
                row[key] = per_distance(row[key], unit)
        converted.append(row)
    return converted


def convert_chargings(chargings, unit):
    return convert_rows(chargings, unit, distance_fields=("mileage",), per_distance_fields=())
