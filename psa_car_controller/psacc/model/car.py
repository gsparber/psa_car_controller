import json
import logging
import threading
from copy import copy
from psa_car_controller.common.sensitive_filter import mask_value
from psa_car_controller.common.utils import atomic_write
from .car_model import DEFAULT_BATTERY_POWER, DEFAULT_FUEL_CAPACITY, DEFAULT_MAX_ELEC_CONSUMPTION, \
    DEFAULT_MAX_FUEL_CONSUMPTION
from .car_status import CarStatus
from ..repository.car_model import CarModelRepository

logger = logging.getLogger(__name__)

# failed status requests in a row after which the kept status is reported as outdated
EMPTY_STATUS_LIMIT = 3
# what a car saved while its model was unknown has in cars.json, unchanged since 2022
UNKNOWN_MODEL_VALUES = {"battery_power": DEFAULT_BATTERY_POWER, "fuel_capacity": DEFAULT_FUEL_CAPACITY,
                        "max_elec_consumption": DEFAULT_MAX_ELEC_CONSUMPTION,
                        "max_fuel_consumption": DEFAULT_MAX_FUEL_CONSUMPTION}


def _replace_unknown_model_values(data):
    """Drop the values a saved car got from the "unknown" model, so that Car() takes them from the model found by VIN.

    Nothing marks a value as edited by the user (docs/abrp.md sends users to cars.json), so only a record
    that is exactly the unknown model is changed. Car() replaces the label by the name of the model found by VIN,
    hence a corrected car doesn't match anymore on the next start.
    """
    if not isinstance(data, dict) or not isinstance(data.get("vin"), str):
        return data
    repository = CarModelRepository()
    model = repository.find_known_model_by_vin(data["vin"])
    if model is None:
        return data
    label_is_a_model = repository.find_model_by_name(data.get("label")) is not None
    if label_is_a_model and data["label"] != model.name:  # the user has chosen another model
        return data
    if not label_is_a_model and not data.get("abrp_name") \
            and all(data.get(field) == value for field, value in UNKNOWN_MODEL_VALUES.items()):
        if any(getattr(model, field) != value for field, value in UNKNOWN_MODEL_VALUES.items()):
            logger.info("cars.json: %s was saved with the values of the unknown model, now it is recognized as %s: %s",
                        mask_value(data["vin"]), model.name,
                        ", ".join(f"{field} {value} -> {getattr(model, field)}"
                                  for field, value in UNKNOWN_MODEL_VALUES.items()))
            return {key: value for key, value in data.items() if key not in UNKNOWN_MODEL_VALUES}
        return data
    # Car() has renamed the car on a previous start, or the user has set abrp_name: from there 46 kWh can be
    # a leftover as well as the usable capacity entered on purpose
    if data.get("battery_power") == DEFAULT_BATTERY_POWER != model.battery_power \
            and data.get("fuel_capacity") in (DEFAULT_FUEL_CAPACITY, model.fuel_capacity) \
            and data.get("max_elec_consumption") == DEFAULT_MAX_ELEC_CONSUMPTION \
            and data.get("max_fuel_consumption") == DEFAULT_MAX_FUEL_CONSUMPTION:
        logger.warning("cars.json: %s has battery_power %s while its model %s has %s: this can be a value from the"
                       " time the model was unknown and it distorts the statistics. It is not changed because it may"
                       " be intentional: if it is wrong, set battery_power to %s in cars.json",
                       mask_value(data["vin"]), DEFAULT_BATTERY_POWER, model.name, model.battery_power,
                       model.battery_power)
    return data


def describe_cars(cars) -> str:
    """For the logs: enough to recognize the cars, without their ids and preconditioning programs."""
    return ", ".join(f"{mask_value(car.vin)} ({car.label})" for car in cars)


# pylint: disable=too-many-arguments,too-many-positional-arguments
class Car:
    def __init__(self, vin, vehicle_id, brand, label=None, battery_power=None, fuel_capacity=None,
                 max_elec_consumption=None, max_fuel_consumption=None, abrp_name=None, precond_programs=None):
        self.vin = vin
        model = None
        if label is not None:
            model = CarModelRepository().find_model_by_name(label)
        if model is None:
            model = CarModelRepository().find_model_by_vin(self.vin)
            label = model.name
        self.vehicle_id = vehicle_id
        self.label = label
        self.brand = brand
        self._status = None
        self._status_failures = 0
        self.abrp_name = abrp_name or model.abrp_name
        self.battery_power = battery_power or model.battery_power
        self.fuel_capacity = fuel_capacity or model.fuel_capacity
        self.max_elec_consumption = max_elec_consumption or model.max_elec_consumption  # kwh/100Km
        self.max_fuel_consumption = max_fuel_consumption or model.max_fuel_consumption  # L/100Km
        # last preconditioning programs reported by the car, kept across restarts: a /ThermalPrecond
        # command always carries the four programs and overwrites the ones stored in the car
        self.precond_programs = precond_programs

    def set_model_name(self, name):
        self.label = name

    def is_electric(self) -> bool:
        return self.fuel_capacity == 0 and self.battery_power > 0

    def is_thermal(self) -> bool:
        return self.fuel_capacity > 0 and self.battery_power == 0

    def is_hybrid(self) -> bool:
        return self.fuel_capacity > 0 and self.battery_power > 0

    def has_battery(self):
        return self.battery_power > 0

    def has_fuel(self):
        return self.fuel_capacity > 0

    def get_status(self):
        if self.status is not None:
            return self.status
        logger.error("status of %s is None", self.vin)
        raise ValueError("status of {} is None".format(self.vin))

    @classmethod
    def from_json(cls, data: dict):
        return cls(**_replace_unknown_model_values(data))

    def to_dict(self):
        car_dict = copy(self.__dict__)
        car_dict.pop("_status")
        car_dict.pop("_status_failures")
        return car_dict

    def __str__(self):
        return str(self.to_dict())

    def get_abrp_name(self):
        if self.abrp_name is not None:
            return self.abrp_name
        raise ValueError("ABRP model is not set")

    @property
    def status(self) -> CarStatus:
        return self._status

    @status.setter
    def status(self, value: CarStatus):
        self._status = value
        if self._status is not None and self.status.__class__ != CarStatus:
            self._status.__class__ = CarStatus
            self._status.correct(self.is_electric())

    @property
    def status_failures(self) -> int:
        """Failed status requests in a row, the status is the last one received before them."""
        return self._status_failures

    @property
    def status_outdated(self) -> bool:
        return self._status_failures >= EMPTY_STATUS_LIMIT

    def status_request_failed(self) -> int:
        self._status_failures += 1
        return self._status_failures

    def status_request_succeeded(self):
        self._status_failures = 0

    def get_charge_speed(self, diff_elvel, duration_in_sec) -> float:
        duration_in_hour = duration_in_sec / 3600
        charged_kw = self.battery_power * diff_elvel / 100
        kw_hour = charged_kw / duration_in_hour
        return kw_hour


class Cars(list):
    _lock = threading.RLock()

    def __init__(self, *args):
        list.__init__(self, *args)
        self.config_filename = "cars.json"

    def get_car_by_vin(self, vin) -> Car:
        for car in self:
            if car.vin == vin:
                return car
        return None

    def get_car_by_id(self, vehicle_id) -> Car:
        for car in self:
            if car.vehicle_id == vehicle_id:
                return car
        return None

    def add(self, car: Car):
        with self._lock:  # requests can list the cars at the same time: no car twice
            car_with_same_vin = self.get_car_by_vin(car.vin)
            if not car_with_same_vin:
                self.append(car)
            elif car_with_same_vin.vehicle_id != car.vehicle_id:
                logger.warning("Vehicle ID changed !")
                car_with_same_vin.vehicle_id = car.vehicle_id

    @classmethod
    def from_json(cls, data: list):
        cars = list(map(Car.from_json, data))
        return cls(cars)

    def __str__(self):
        return str(list(map(str, self)))

    def save_cars(self, name=None):
        if name is None:
            name = self.config_filename
        with self._lock:  # read and write in one go: a slower save must not put older values back
            config_str = json.dumps(self, default=lambda car: car.to_dict(), sort_keys=True, indent=4)
            atomic_write(name, config_str.encode("utf-8"))

    @staticmethod
    def load_cars(name=None):
        if name is None:
            name = Cars().config_filename
        try:
            with open(name, "r", encoding="utf-8") as file:
                json_str = file.read()
                cars = Cars.from_json(json.loads(json_str))
                cars.config_filename = name
                cars.save_cars()
                return cars
        except (FileNotFoundError, TypeError) as ex:
            logger.debug(ex)
            return Cars()
