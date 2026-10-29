"""Compatibility of the vehicle status with the newer `energies` schema of the API.

The generated client knows the API 4.0 status, whose energy data is in `energy`. Newer API versions
deprecate it for `energies` (changelog v4.10.1). Responses seen in 2025 still carry both, with the
same values; `energies` nests the charging and battery data under `extension`:

    {"type": "Electric", "level": 63.0, "autonomy": 192, "createdAt": "...",
     "extension": {"electric": {"charging": {"status": "Disconnected", ...},
                                "battery": {"health": {"resistance": 92}, "load": {...}}}}}
    {"type": "Fuel", "level": 20.0, "autonomy": 90, "createdAt": "...",
     "extension": {"fuel": {"consumptions": {"total": 673.06, "instant": 12.9}}}}

If a response only has `energies`, it is converted to `energy`, so the status doesn't lose its
energy data the day the deprecated list is removed.
"""
import copy
import json
import logging
import threading
from typing import Optional

logger = logging.getLogger(__name__)

STATUS_RESPONSE_TYPE = "Status"
REDACTED = "***"
# keys left out of the logged raw status: the position, and ids the log filter doesn't know
_REDACTED_KEYS = {"coordinates", "_links", "id", "vin"}

_raw_status_log_lock = threading.Lock()
_raw_status_log_enabled = False
_raw_status_logged = set()


def enable_raw_status_log(enabled: bool = True):
    """Log the raw status of each vehicle once, to check which schema the API sends."""
    global _raw_status_log_enabled  # pylint: disable=global-statement
    with _raw_status_log_lock:
        _raw_status_log_enabled = enabled
        _raw_status_logged.clear()


def _redact(data):
    if isinstance(data, dict):
        return {key: REDACTED if key in _REDACTED_KEYS else _redact(value) for key, value in data.items()}
    if isinstance(data, list):
        return [_redact(value) for value in data]
    return data


def log_raw_status(data: dict):
    with _raw_status_log_lock:
        if not _raw_status_log_enabled:
            return
        vehicle = json.dumps(data.get("_links"), sort_keys=True, default=str)
        if vehicle in _raw_status_logged:
            return
        _raw_status_logged.add(vehicle)
    logger.info("Raw vehicle status (position and ids hidden, logged once per vehicle): %s",
                json.dumps(_redact(data), sort_keys=True, default=str))


def _energy_from_energies(energy: dict) -> Optional[dict]:
    """One `energies` entry in the shape of an `energy` one, None if it isn't a dict."""
    if not isinstance(energy, dict):
        return None
    result = {key: energy[key] for key in ("type", "level", "autonomy", "createdAt", "updatedAt")
              if key in energy}
    if "createdAt" in result and "updatedAt" not in result:
        # `energy` has both, with the same value; the stale status check reads updatedAt
        result["updatedAt"] = result["createdAt"]
    extension = energy.get("extension") if isinstance(energy.get("extension"), dict) else {}
    electric = extension.get("electric") if isinstance(extension.get("electric"), dict) else {}
    if isinstance(electric.get("charging"), dict):
        result["charging"] = copy.deepcopy(electric["charging"])
    battery = electric.get("battery") if isinstance(electric.get("battery"), dict) else {}
    if isinstance(battery.get("health"), dict):
        result["battery"] = {"health": copy.deepcopy(battery["health"])}
    fuel = extension.get("fuel") if isinstance(extension.get("fuel"), dict) else {}
    consumptions = fuel.get("consumptions") if isinstance(fuel.get("consumptions"), dict) else {}
    if "instant" in consumptions:
        result["consumption"] = consumptions["instant"]
    return result


def energy_from_energies(data):
    """Fill the `energy` list of a raw status from `energies` if it has none, in place."""
    if not isinstance(data, dict) or data.get("energy"):
        return data
    energies = data.get("energies")
    if not isinstance(energies, list):
        return data
    energy = [entry for entry in map(_energy_from_energies, energies) if entry is not None]
    if energy:
        logger.debug("The status has no energy list, using its energies list")
        data["energy"] = energy
    return data


def prepare_status(data):
    """Raw status from the API, ready to deserialize into the generated Status model."""
    if isinstance(data, dict):
        log_raw_status(data)
    return energy_from_energies(data)
