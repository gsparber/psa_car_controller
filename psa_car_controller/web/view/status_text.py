"""How the home page shows car status values, kept free of Dash so it can be unit tested.

Adapted from angelangelov88/psa_car_controller (feature/setup-ui, web/view/status_text.py). Values
come in metric; distances and speeds are shown in the unit passed in (see web/units.py).
"""
from datetime import datetime, timezone

from psa_car_controller.common.utils import parse_hour
from psa_car_controller.web import units

PRECOND_STATES = {"Enabled": "On", "Disabled": "Off", "Finished": "Finished", "Failure": "Failed"}
PRECOND_FAILURE_CAUSES = {
    "Defect": "Fault",
    "DoorOpened": "Door open",
    "LowBattery": "Battery too low",
    "LowFuelLevel": "Fuel too low",
    "TooManyUnusedProg": "Too many unused schedules",
    "WindowsRoofOpened": "Window or roof open",
}
IGNITION_ON = ("Start", "StartUp")


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def format_int(value, unit=""):
    """Whole number with thin grouping, e.g. "12 345 km"; "–" when unknown."""
    if not _is_number(value):
        return "–"
    text = f"{round(value):,}".replace(",", " ")
    return f"{text} {unit}".strip()


def format_distance(km, unit=units.KM):
    """A distance stored in km as a whole number in the display unit, e.g. "7 671 mi"."""
    return format_int(units.distance(km, unit), units.distance_label(unit))


def format_duration(iso):
    """An ISO 8601 duration from the car ("PT2H10M") as "2 h 10 min"; None when it's zero or unreadable."""
    try:
        parts = parse_hour(iso)
    except (AttributeError, TypeError, ValueError):
        return None
    hours, minutes = parts[0], parts[1]
    if hours and minutes:
        return f"{hours} h {minutes} min"
    if hours:
        return f"{hours} h"
    if minutes:
        return f"{minutes} min"
    return None


def charging_text(plugged, status, remaining):
    """One line for the cable and the charge together. The car's "Disconnected" status only means "not charging":
    it is also reported with the cable in, so `plugged` decides between plugged in and unplugged."""
    if status == "InProgress":
        time_left = format_duration(remaining)
        return f"Charging · full in {time_left}" if time_left else "Charging"
    if status == "Finished":
        return "Charged"
    if status == "Failure":
        return "Charging failed"
    if plugged is True:
        return "Plugged in, not charging"
    if plugged is False:
        return "Unplugged"
    return None


def charge_power_kw(rate_kmh, battery_kwh, level, range_km):
    """Charging power estimated from the car's charging rate, which is range gained per hour (km/h), times the energy
    a km currently takes (battery size x level / range). None when a value is missing or zero."""
    values = (rate_kmh, battery_kwh, level, range_km)
    if not all(_is_number(v) and v > 0 for v in values):
        return None
    kwh_per_km = battery_kwh * level / 100 / range_km
    return rate_kmh * kwh_per_km


def format_power(kw):
    return f"≈{kw:.1f} kW"


def precond_text(status):
    return PRECOND_STATES.get(status)


def precond_failure_text(failure_cause):
    return PRECOND_FAILURE_CAUSES.get(failure_cause, failure_cause)


def car_state_text(ignition, moving, speed_kmh, unit=units.KM):
    """Parked, On or Driving (with the speed when the car gives one)."""
    if _is_number(speed_kmh) and speed_kmh > 0:
        return f"Driving · {round(units.distance(speed_kmh, unit))} {units.speed_label(unit)}"
    if moving is True:
        return "Driving"
    if ignition in IGNITION_ON:
        return "On"
    if ignition is not None:
        return "Parked"
    return None


def format_updated(updated_at, now=None):
    """Local time ("10:12") for today, with the date ("14 Oct 16:52") otherwise. The car's times are UTC."""
    if not isinstance(updated_at, datetime):
        return None
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    local = updated_at.astimezone()
    today = (now or datetime.now(timezone.utc)).astimezone().date()
    if local.date() == today:
        return f"{local:%H:%M}"
    return f"{local.day} {local:%b %H:%M}"


def format_age(seconds):
    """How old data is ("5 min ago"), None if unknown."""
    if not _is_number(seconds):
        return None
    minutes = int(seconds // 60)
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes} min ago"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} h ago"
    return f"{hours // 24} days ago"


def outdated_text(age_seconds):
    """Warning for a status kept because the API stopped answering."""
    age = format_age(age_seconds)
    return f"No new data from the API, last status from {age}" if age else "No new data from the API"


def position(coordinates):
    """(latitude, longitude) from the car's GeoJSON coordinates [longitude, latitude, altitude], or None."""
    try:
        longitude, latitude = coordinates[0], coordinates[1]
    except (IndexError, TypeError):
        return None
    if not (_is_number(latitude) and _is_number(longitude)):
        return None
    return latitude, longitude


def map_link_url(latitude, longitude):
    return f"https://www.google.com/maps/search/?api=1&query={latitude:.6f},{longitude:.6f}"


def maintenance_text(maintenance, unit=units.KM):
    """Distance and days left until the next service, e.g. "12 000 km · 150 days"; None when unknown.

    Both can be negative once the service is overdue."""
    if not maintenance:
        return None
    parts = []
    mileage, days = maintenance.get("mileage"), maintenance.get("days")
    if _is_number(mileage):
        parts.append(format_distance(mileage, unit) if mileage >= 0 else f"{format_distance(-mileage, unit)} overdue")
    if _is_number(days):
        unit = "day" if abs(days) == 1 else "days"
        parts.append(f"{format_int(days, unit)}" if days >= 0 else f"{format_int(-days, unit)} overdue")
    return " · ".join(parts) or None


def privacy_text(state):
    """Note for the car's private mode, None when the car shares its data."""
    if state == "Full":
        return "Private mode is on in the car: position and live data are paused"
    return None
