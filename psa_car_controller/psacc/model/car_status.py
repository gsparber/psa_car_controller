import logging
from datetime import datetime, timezone

from psa_car_controller.psa.connected_car_api import Battery
from psa_car_controller.psa.connected_car_api.models.energy import Energy
from psa_car_controller.psa.connected_car_api.models.energy_charging import EnergyCharging
from psa_car_controller.psa.connected_car_api.models.geometry import Geometry
from psa_car_controller.psa.connected_car_api.models.kinetic import Kinetic
from psa_car_controller.psa.connected_car_api.models.position import Position
from psa_car_controller.psa.connected_car_api.models.position_properties import PositionProperties
from psa_car_controller.psa.connected_car_api.models.status import Status
from psa_car_controller.psa.connected_car_api.models.vehicle_odometer import VehicleOdometer

logger = logging.getLogger(__name__)


# pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
class CarStatus(Status):
    def __init__(self, embedded=None, links=None, battery=None, doors_state=None, energy=None, environment=None,
                 ignition=None, kinetic=None, last_position=None, preconditionning=None, privacy=None, safety=None,
                 service=None, timed_odometer=None, updated_at=None):  # noqa: E501
        super().__init__(embedded, links, battery, doors_state, energy, environment, ignition, kinetic, last_position,
                         preconditionning, privacy, safety, service, timed_odometer, updated_at)
        self.correct(False)

    def correct(self, electric_car):
        try:
            if len(self.last_position.geometry.coordinates) < 2:
                raise AttributeError()
            if len(self.last_position.geometry.coordinates) < 3:
                # set altitude none
                self.last_position.geometry.coordinates.append(None)
        except (AttributeError, TypeError):
            self.last_position = Position(geometry=Geometry(coordinates=[None, None, None], type="Point"),
                                          properties=PositionProperties(updated_at=None))
        if self.kinetic is None:
            self.kinetic = Kinetic()
        # always put electric energy first
        if len(self._energy) == 2 and self._energy[0].type != 'Electric':
            self._energy = self._energy[::-1]

        if self.timed_odometer is None:
            self.timed_odometer = VehicleOdometer()
        if electric_car:
            self.get_energy("Fuel").level = None
        if self.battery is None:
            self.battery = Battery()

    def get_updated_at(self):
        return status_updated_at(self)

    def is_moving(self):
        try:
            return self.kinetic.moving
        except AttributeError:
            logger.error("kinetic not available from api")
            return None

    def get_energy(self, energy_type) -> Energy:
        for energy in self._energy:
            if energy.type == energy_type:
                return energy
        return Energy(charging=EnergyCharging())


def status_updated_at(status: Status):
    """Date of a status from the API: its updatedAt, else the newest date of its parts."""
    updated_at = getattr(status, "_updated_at", None)
    if isinstance(updated_at, datetime):
        return updated_at
    dates = [energy.updated_at for energy in status.energy or []]
    dates.append(getattr(getattr(status.last_position, "properties", None), "updated_at", None))
    dates.append(getattr(status.timed_odometer, "updated_at", None))
    dates = [date for date in dates if isinstance(date, datetime)]
    try:
        return max(dates, default=None)
    except TypeError:  # naive and aware dates can't be compared
        return None


def status_age_seconds(status: Status, now=None):
    """Seconds since the date of a status, None if it has no date. The car's times are UTC."""
    updated_at = status_updated_at(status)
    if updated_at is None:
        return None
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    # a clock running behind the car's must not make the data look like it comes from the future
    return max(0, int(((now or datetime.now(timezone.utc)) - updated_at).total_seconds()))


def _number(value):
    return f"{value:g}" if isinstance(value, float) else str(value)


def _age_text(minutes: int) -> str:
    if minutes < 120:
        return f"{minutes} min"
    if minutes < 48 * 60:
        return f"{minutes // 60} h"
    return f"{minutes // (24 * 60)} days"


def status_key(status) -> tuple:
    """What status_summary tells, without its age: equal for the same status answered again."""
    for energy in getattr(status, "energy", None) or []:
        if getattr(energy, "type", None) == "Electric":
            charging = energy.charging
            return (energy.level, getattr(charging, "status", None), getattr(charging, "charging_rate", None),
                    getattr(charging, "remaining_time", None), energy.updated_at)
    return (status_updated_at(status),)


def status_summary(status) -> str:
    """Battery and charging of a status and how old its battery level is, for the event log."""
    parts = []
    electric = None
    for energy in getattr(status, "energy", None) or []:
        if getattr(energy, "type", None) == "Electric":
            electric = energy
    if electric is not None:
        parts.append(f"battery {_number(electric.level)} %")
        charging = electric.charging
        if charging is not None and charging.status is not None:
            parts.append(f"charging {charging.status}")
            if charging.charging_rate:
                parts.append(f"rate {charging.charging_rate}")
            if charging.remaining_time:
                parts.append(f"remaining {charging.remaining_time}")
        updated_at = electric.updated_at
    else:
        updated_at = status_updated_at(status)
    if isinstance(updated_at, datetime):
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        age = _age_text(max(0, int((datetime.now(timezone.utc) - updated_at).total_seconds() // 60)))
        parts.append(f"level from {age} ago" if electric is not None else f"data from {age} ago")
    return ", ".join(parts) or "no battery data"
