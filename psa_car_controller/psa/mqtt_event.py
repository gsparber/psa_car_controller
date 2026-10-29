"""Vehicle events pushed over mqtt (MQTT_EVENT_TOPIC) and how they update the rest api status.

Each event is a full snapshot of the car, not a delta. Events arrive out of order and repeat old
values with a fresh date (the battery level stayed hours behind the api), so neither the arrival
order nor the event date tells how fresh a value is: only a value which changed between two
events in a row is known to be newer than the api status. The approach and the meaning of the
codes come from the measurements of the Home Assistant integration stellantis_vehicles (MIT,
https://github.com/andreadegiovine/homeassistant-stellantis-vehicles, mqtt_event.py and base.py).
Battery level and range stay api only: the mqtt values lag behind the api and spike.
"""
import logging
import threading
from datetime import datetime, timezone

from psa_car_controller.psa.connected_car_api.models.doors_state import DoorsState
from psa_car_controller.psa.connected_car_api.models.energy_charging import EnergyCharging
from psa_car_controller.psacc.model.car_status import status_updated_at

logger = logging.getLogger(__name__)

EVENT_DATE_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
# change with every event, not with the state of the car
BOOKKEEPING_KEYS = ("date", "obj_counter", "reason", "signal_quality")
# hmi_state; 2 and 4 are seen around the start of a charge but their meaning is unknown
CHARGING_STATUS = {0: "Disconnected", 1: "InProgress", 3: "Stopped"}
# doors_locking_state; 0 and 7 are short steps while unlocking and locking
LOCKED, UNLOCKED, UNLOCKING, LOCKING = 1, 3, 0, 7
LOCK_STATE = {LOCKED: "Locked", UNLOCKED: "Unlocked"}
# precond_state.status
AIR_CONDITIONING_STATUS = {0: "Disabled", 1: "Enabled", 2: "Finished"}
# doors_opening_state indexes; 5 is never set and 6 has no api counterpart
DOOR_IDENTIFIERS = ("Driver", "Passenger", "RearLeft", "RearRight", "Trunk")
# the values compared between two events, by part of the api status they overwrite
GROUPS = ("charging", "doors", "door_opening", "preconditioning")

# results of VehicleEvents.add()
CHANGED = "changed"
DUPLICATE = "duplicate"
OUT_OF_ORDER = "out of order"


def parse_event_date(value):
    try:
        return datetime.strptime(value, EVENT_DATE_FORMAT).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def parse_event(payload: dict) -> dict:
    """The values of a vehicle event which are used, a missing or null part gives None values."""
    charging = payload.get("charging_state") or {}
    precond = payload.get("precond_state") or {}
    doors = payload.get("doors_state") or {}
    cable_detected = charging.get("cable_detected")
    opening = doors.get("doors_opening_state") or []
    return {
        "date": parse_event_date(payload.get("date")),
        "counter": payload.get("obj_counter"),
        "content": {key: value for key, value in payload.items() if key not in BOOKKEEPING_KEYS},
        # cable_detected is inverted: 0 when plugged
        "charging": (charging.get("hmi_state"), None if cable_detected is None else cable_detected == 0),
        "doors": doors.get("doors_locking_state"),
        "door_opening": {identifier: bool(opening[index]) for index, identifier in enumerate(DOOR_IDENTIFIERS)
                         if index < len(opening)},
        "preconditioning": precond.get("status"),
        "programs": precond.get("programs") or None,
    }


def _as_utc(date):
    if isinstance(date, datetime) and date.tzinfo is None:
        return date.replace(tzinfo=timezone.utc)
    return date if isinstance(date, datetime) else None


def rest_charging_status(status):
    """Charging status of the electric energy as the api reported it, before any mqtt overlay."""
    charging = status.get_energy('Electric').charging
    return getattr(charging, "_rest_status", charging.status)


def _electric_energy(status):
    for energy in getattr(status, "energy", None) or []:
        if energy.type == "Electric":
            return energy
    return None


class VehicleEvents:
    """The events of one car: their order, what changed and when."""

    def __init__(self, vin):
        self.vin = vin
        self.last = None
        self.last_key = None
        # by group: date of the last event which changed its values, None while unknown
        self.changed_at = {}
        # last settled lock state, used when the api doesn't report any
        self.lock_state = None
        self._lock = threading.Lock()

    def add(self, payload: dict) -> str:
        event = parse_event(payload)
        with self._lock:
            # obj_counter starts again at 1 with each session of the car, the date orders sessions.
            # Without date or counter the event can't be ordered, so it is taken rather than lost.
            if event["date"] is not None and event["counter"] is not None:
                key = (event["date"], event["counter"])
                if self.last_key is not None and key < self.last_key:
                    logger.debug("ignore mqtt event of %s from %s (%s), older than the last one (%s)",
                                 self.vin, event["date"], event["counter"], self.last_key)
                    return OUT_OF_ORDER
                self.last_key = key
            if self.last is not None and event["content"] == self.last["content"]:
                return DUPLICATE
            for group in GROUPS:
                if self.last is None:
                    # nothing to compare with: the values may be old ones repeated, the api decides
                    self.changed_at[group] = None
                elif event[group] != self.last[group]:
                    self.changed_at[group] = event["date"]
            self._store_lock_state(event["doors"])
            self.last = event
            return CHANGED

    def _store_lock_state(self, locking_state):
        if locking_state in LOCK_STATE:
            self.lock_state = LOCK_STATE[locking_state]
        elif locking_state is not None and locking_state not in (UNLOCKING, LOCKING):
            logger.warning("unknown doors_locking_state %s, please report it on github", locking_state)

    def _is_newer(self, group, rest_date):
        changed_at = self.changed_at.get(group)
        if changed_at is None:
            return False
        rest_date = _as_utc(rest_date)
        return rest_date is None or changed_at > rest_date

    def apply(self, status):
        """Overwrite the parts of an api status which changed on mqtt since the api reported them."""
        if status is None:
            return
        with self._lock:
            if self.last is None:
                return
            status_date = status_updated_at(status)
            self._apply_charging(status, status_date)
            self._apply_doors(status, status_date)
            self._apply_preconditioning(status, status_date)

    def _apply_charging(self, status, status_date):
        electric = _electric_energy(status)
        if electric is None or not self._is_newer("charging", electric.created_at or electric.updated_at
                                                  or status_date):
            return
        if electric.charging is None:
            electric.charging = EnergyCharging()
        charging = electric.charging
        hmi_state, plugged = self.last["charging"]
        charging_status = CHARGING_STATUS.get(hmi_state)
        if charging_status and charging_status != charging.status:
            if not hasattr(charging, "_rest_status"):
                # the delayed wakeup of RemoteClient._fix_not_updated_api still needs to know it
                charging._rest_status = charging.status  # pylint: disable=protected-access
            charging.status = charging_status
        if plugged is not None:
            charging.plugged = plugged

    def _apply_doors(self, status, status_date):
        doors_state = status.doors_state
        if doors_state is None or not doors_state.locked_state or getattr(doors_state, "_mqtt_lock_state", False):
            # some cars never report the lock state on the api: the last one seen on mqtt is all there is
            if self.lock_state is not None:
                if doors_state is None:
                    doors_state = status.doors_state = DoorsState()
                doors_state.locked_state = [self.lock_state]
                doors_state._mqtt_lock_state = True  # pylint: disable=protected-access
        elif self._is_newer("doors", doors_state.updated_at or status_date):
            # only when it differs, so that a finer api value (SuperLocked...) is kept
            locking_state = self.last["doors"]
            if locking_state == LOCKED and "Unlocked" in doors_state.locked_state:
                doors_state.locked_state = ["Locked"]
            elif locking_state == UNLOCKED and "Unlocked" not in doors_state.locked_state:
                doors_state.locked_state = ["Unlocked"]
        if doors_state is not None and doors_state.opening \
                and self._is_newer("door_opening", doors_state.updated_at or status_date):
            door_opening = self.last["door_opening"]
            for opening in doors_state.opening:
                if opening.identifier in door_opening:
                    opening.state = "Open" if door_opening[opening.identifier] else "Closed"

    def _apply_preconditioning(self, status, status_date):
        air_conditioning = getattr(status.preconditionning, "air_conditioning", None)
        if air_conditioning is None or not self._is_newer(
                "preconditioning", air_conditioning.created_at or air_conditioning.updated_at or status_date):
            return
        air_conditioning_status = AIR_CONDITIONING_STATUS.get(self.last["preconditioning"])
        if air_conditioning_status:
            air_conditioning.status = air_conditioning_status
