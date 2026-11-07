import logging
import os

import ruamel.yaml
from typing import List, Optional

from psa_car_controller.psacc.model.car_model import ElecModel, CarModel, DEFAULT_BATTERY_POWER, DEFAULT_FUEL_CAPACITY
from psa_car_controller.psacc.utils.utils import Singleton

MODELS_FILE = os.path.dirname(os.path.realpath(__file__)) + "/../resources/car_models.yml"
logger = logging.getLogger(__name__)


class CarModelRepository(metaclass=Singleton):
    def __init__(self):
        with open(MODELS_FILE, "r", encoding="utf-8") as f:
            models = f.read()
        yaml = ruamel.yaml.YAML()
        yaml.register_class(ElecModel)
        yaml.register_class(CarModel)
        self.models: List[CarModel] = yaml.load(models)

    def find_known_model_by_vin(self, vin) -> Optional[CarModel]:
        for car_model in self.models:
            if car_model.match(vin):
                return car_model
        # The 9th letter of a VIN is a check digit, it changes from car to car of the same model: the list has the
        # e-3008 of 2024 eight times for that reason. Without a model for the whole prefix, one that differs only
        # there is taken (the same models in the list differ by at most 0.5 kWh then).
        for car_model in self.models:
            if car_model.match_but_check_digit(vin):
                logger.info("Car model of %s… found without its check digit: %s", vin[:8], car_model.name)
                return car_model
        return None

    def find_model_by_vin(self, vin) -> CarModel:
        if vin != "vin":
            car_model = self.find_known_model_by_vin(vin)
            if car_model is not None:
                return car_model
            logger.warning("Can't get car model, please report an issue on github with your car model"
                           " and first ten letter of your VIN : %s", vin[:10])
        return CarModel("unknown", DEFAULT_BATTERY_POWER, DEFAULT_FUEL_CAPACITY)

    def find_model_by_name(self, name) -> CarModel:
        for car_model in self.models:
            if car_model.name == name:
                return car_model
        return None
