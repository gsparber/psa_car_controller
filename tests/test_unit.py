# flake8: noqa
import bz2
import contextlib
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import reverse_geocode
import ruamel.yaml
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from cryptography.x509 import load_pem_x509_certificate
from dateutil.tz import tzutc
from pytz import UTC

from psa_car_controller import psa
from psa_car_controller.common import utils
from psa_car_controller.common.mylogger import my_logger

from psa_car_controller.common.utils import parse_hour, RateLimitException, rate_limit, \
    validate_preconditioning_programs
from psa_car_controller.psa.otp.otp import load_otp, save_otp
from psa_car_controller.psa.setup.apk_parser import ApkParser
from psa_car_controller.psa.setup.app_decoder import get_content_from_apk, GITHUB_USER, GITHUB_REPO
from psa_car_controller.psa.setup.github import github_file_need_to_be_downloaded
from psa_car_controller.psa.connected_car_api import ApiClient
from psa_car_controller.psacc.application.charge_control import ChargeControls
from psa_car_controller.psacc.application.charging import Charging
from psa_car_controller.psacc.application.ecomix import Ecomix
from psa_car_controller.psacc.application.psa_client import PSAClient
from psa_car_controller.psacc.model.car import Car, Cars, UNCERTAIN_POWERTRAIN_MODELS
from psa_car_controller.psacc.model.car_model import CarModel, ElecModel
from psa_car_controller.psacc.model.trip import Trip
from psa_car_controller.psacc.repository import config_repository
from psa_car_controller.psacc.repository.car_model import CarModelRepository
from psa_car_controller.psacc.repository.config_repository import ConfigRepository
from psa_car_controller.psacc.repository.db import Database
from psa_car_controller.psacc.repository.trips import Trips
from psa_car_controller.psacc.utils.utils import get_temp
from tests.data.car_status import FUEL_CAR_STATUS, ELECTRIC_CAR_STATUS, ELECTRIC_CAR_STATUS_V2
from tests.github_fixture import FakeApk, FakeGithub, build_apk_archive
from tests.utils import DATA_DIR, record_position, latitude, longitude, date0, date1, date2, date3, record_charging, \
    vehicule_list, get_new_test_db, get_date, date4, compare_dict, duration_min, duration_str
from psa_car_controller.web.figures import get_figures, get_battery_curve_fig, get_altitude_fig

REAL_APK_ARCHIVE = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "mypeugeot.apk.bz2")
dummy_value = 0


def callback_test():
    global dummy_value
    dummy_value += 1


class TestUnit(unittest.TestCase):
    def __init__(self, methodName='runTest'):
        super().__init__(methodName)
        self.test_online = os.environ.get("TEST_ONLINE", "0") == "1"
        self.vehicule_list = vehicule_list

    def test_car(self):
        car1 = Car("VRAAAAAAA", "1sdfdksnfk222", "Peugeot", "208", 46, 0)
        car2 = Car("VR3UHZKX", "1sdfdksnfk222", "Peugeot")
        cars = Cars([car1, car2])
        cars.save_cars(name=DATA_DIR + "test_car.json")
        Cars.load_cars(name=DATA_DIR + "test_car.json")

    def test_otp_config(self):
        otp_config = load_otp(filename=DATA_DIR + "otp_test.bin")
        assert otp_config is not None
        save_otp(otp_config, filename=DATA_DIR + "otp_test2.bin")

    def test_mypsacc(self):
        if self.test_online:
            myp = PSAClient.load_config("./config/config.json")
            myp.manager.refresh_token_now()
            myp.save_config()
            myp.get_vehicles()
            car = myp.vehicles_list[0]
            myp.abrp.abrp_enable_vin.add(car.vin)
            myp.get_vehicle_info(myp.vehicles_list[0].vin)
            myp.abrp.call(car, 22.1)

    def test_open_weather_api(self):
        if self.test_online:
            myp = PSAClient.load_config("./config/config.json")
            assert isinstance(get_temp(str(latitude), str(longitude), myp.weather_api), float)

    def test_car_model(self):
        assert CarModelRepository().find_model_by_vin("VR3UHZKXZL").name == "e-208"
        assert CarModelRepository().find_model_by_vin("VR3UKZKXZM").name == "e-2008"
        assert CarModelRepository().find_model_by_vin("VXKUHZKXZL").name == "corsa-e"
        assert CarModelRepository().find_model_by_vin("VXKUKZKW0S").name == "Mokka-e 2024+"

    def test_car_model_e_c3_2026(self):
        model = CarModelRepository().find_model_by_vin("VR7CBZYA4T1234567")
        assert model.name == "Citroën e-C3 IV"
        assert model.battery_power == 44
        assert model.abrp_name == "citroen:ec3:24:44"

    def test_car_model_e_c3_2024(self):
        model = CarModelRepository().find_model_by_vin("VR7CBZYAZR1234567")
        assert model.name == "Citroën e-C3 IV (2024)"
        assert model.battery_power == 44

    def test_car_model_e_3008_gt_2026(self):
        model = CarModelRepository().find_model_by_vin("VR3KCZKZXT1234567")
        assert model.name == "E-3008 GT"
        assert model.battery_power == 73
        assert CarModelRepository().find_model_by_name("E-3008 GT").reg == "VR3KCZKZXT"

    def test_car_model_former_regex_entries(self):
        expected = {
            "VF3CCHMRPJ": ("208(2017-2019)", 0, 44),
            "VR3ENZKUXR": ("E-Partner 50 kWh (2024)", 50, 0),
            "VF7VEAHXKL": ("Spacetourer Vanster 2021", 0, 70),
            "VR3KCZKZ4R": ("e-5008 Single Motor 73 kWh", 73, 0),
        }
        for prefix, (name, battery_power, fuel_capacity) in expected.items():
            car = Car(prefix + "1234567", "vehicle_id", "brand")
            self.assertEqual(car.label, name)
            self.assertEqual(car.battery_power, battery_power)
            self.assertEqual(car.fuel_capacity, fuel_capacity)
            self.assertGreater(car.max_elec_consumption, 0)
            self.assertIsNotNone(car.max_fuel_consumption)

    def test_car_model_reg_is_a_plain_prefix(self):
        for model in CarModelRepository().models:
            self.assertRegex(model.reg, r"^[A-Z0-9]+$",
                             f"{model.name}: reg is compared with startswith, not as a regex")

    def test_every_car_model_creates_a_car(self):
        for model in CarModelRepository().models:
            with self.subTest(model=model.name, reg=model.reg):
                car = Car(model.reg.ljust(17, "0"), "vehicle_id", "brand")
                self.assertEqual(car.label, model.name)
                self.assertEqual(car.battery_power, model.battery_power)
                self.assertEqual(car.fuel_capacity, model.fuel_capacity)
                self.assertIsNotNone(car.max_elec_consumption)
                self.assertIsNotNone(car.max_fuel_consumption)

    def test_car_model_identity_fields_are_explicit(self):
        # the defaults keep the app running, but a guessed battery or tank size would give wrong statistics
        for model in CarModelRepository().models:
            for field in ("name", "reg", "battery_power", "fuel_capacity"):
                self.assertIn(field, vars(model), f"{model.name}: {field} missing in car_models.yml")

    def test_car_model_without_limits_uses_unknown_model_defaults(self):
        expected = {
            "VR7BAHNSBP": (70, 30),  # thermal
            "VR3F3DGY2R": (70, 30),  # hybrid
            "VR3FMZKW3R": (70, 0),  # ElecModel
            "VR3FNZKW4R": (70, 30),  # electric, but a CarModel entry
        }
        for prefix, limits in expected.items():
            car = Car(prefix + "1234567", "vehicle_id", "brand")
            self.assertEqual((car.max_elec_consumption, car.max_fuel_consumption), limits, car.label)

    def test_car_model_yaml_entry_with_only_a_name(self):
        yaml = ruamel.yaml.YAML()
        yaml.register_class(ElecModel)
        yaml.register_class(CarModel)
        thermal, electric = yaml.load("- !CarModel\n  name: a\n- !ElecModel\n  name: b\n")
        self.assertFalse(thermal.match("VIN"))
        for model, max_fuel, fuel_capacity in ((thermal, 30, 0), (electric, 0, 0)):
            with patch.object(CarModelRepository, "find_model_by_vin", return_value=model):
                car = Car("VIN", "vehicle_id", "brand")
            self.assertEqual(car.label, model.name)
            self.assertEqual(car.battery_power, 46)
            self.assertEqual(car.fuel_capacity, fuel_capacity)
            self.assertIsNone(car.abrp_name)
            self.assertEqual((car.max_elec_consumption, car.max_fuel_consumption), (70, max_fuel))

    def test_elec_model_constructor(self):
        model = ElecModel("name", 40)
        self.assertEqual((model.max_elec_consumption, model.max_fuel_consumption), (70, 0))

    def test_load_cars_json_of_a_model_without_limits(self):
        base = {"vin": "VR7BAHNSBP1234567", "vehicle_id": "id", "brand": "C", "label": "C4 Shine 2023"}
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "cars.json")
            for stored, limits in (({}, (70, 30)),
                                   ({"max_elec_consumption": 0, "max_fuel_consumption": 0}, (70, 30)),
                                   ({"max_elec_consumption": None, "max_fuel_consumption": None}, (70, 30)),
                                   ({"max_elec_consumption": 55, "max_fuel_consumption": 12}, (55, 12))):
                with self.subTest(stored=stored):
                    with open(path, "w", encoding="utf-8") as file:
                        json.dump([{**base, **stored}], file)
                    car = Cars.load_cars(path)[0]
                    self.assertEqual((car.max_elec_consumption, car.max_fuel_consumption), limits)

    UNKNOWN_CAR = {"vin": "VR7BAHNSBP1234567", "vehicle_id": "id", "brand": "C", "label": "unknown", "abrp_name": None,
                   "battery_power": 46, "fuel_capacity": 0, "max_elec_consumption": 70, "max_fuel_consumption": 30,
                   "precond_programs": {"program1": {}}}
    CAR_VALUES = ("label", "abrp_name", "battery_power", "fuel_capacity", "max_elec_consumption",
                  "max_fuel_consumption")

    @staticmethod
    def _load_cars_json(path, **stored):
        with open(path, "w", encoding="utf-8") as file:
            json.dump([{**TestUnit.UNKNOWN_CAR, **stored}], file)
        return Cars.load_cars(path)[0]

    def _car_values(self, car):
        return tuple(getattr(car, field) for field in self.CAR_VALUES)

    def test_cars_json_of_unknown_model_gets_the_recognized_model(self):
        logger_name = "psa_car_controller.psacc.model.car"
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "cars.json")
            for vin, label, expected in (
                    ("VR7BAHNSBP1234567", "unknown", ("C4 Shine 2023", None, 0, 50, 70, 30)),
                    ("VR3F3DGY2R1234567", "unknown", ("408 GT Hybrid 225", None, 12.4, 40, 70, 30)),
                    ("VR3KCZKZ8R1234567", "unknown", ("e-5008", "peugeot:e5008:25:77", 73, 0, 70, 0)),
                    # short_label of the app replaced "unknown" in app_decoder
                    ("VR7BAHNSBP1234567", "C4", ("C4 Shine 2023", None, 0, 50, 70, 30)),
                    ("VR7BAHNSBP1234567", None, ("C4 Shine 2023", None, 0, 50, 70, 30))):
                with self.subTest(vin=vin, label=label):
                    with self.assertLogs(logger_name, level="INFO") as logs:
                        car = self._load_cars_json(path, vin=vin, label=label)
                    self.assertEqual(self._car_values(car), expected)
                    self.assertEqual(car.precond_programs, {"program1": {}})
                    self.assertEqual(len(logs.records), 1)
                    self.assertEqual(logs.records[0].levelname, "INFO")
                    self.assertNotIn(vin, logs.output[0])
                    self.assertIn("***" + vin[-4:], logs.output[0])
                    with open(path, encoding="utf-8") as file:
                        saved = json.load(file)[0]
                    self.assertEqual(tuple(saved[field] for field in self.CAR_VALUES), expected)
                    # second start: nothing to correct, no log, nothing changes
                    with self.assertNoLogs(logger_name, level="INFO"):
                        again = Cars.load_cars(path)[0]
                    self.assertEqual(self._car_values(again), expected)

    def test_cars_json_correction_is_stable_for_every_model(self):
        logger_name = "psa_car_controller.psacc.model.car"
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "cars.json")
            for model in CarModelRepository().models:
                if model.reg == "VXKUPHNEKP":  # only warns, see test_cars_json_of_uncertain_powertrain_only_warns
                    continue
                with self.subTest(model=model.name, reg=model.reg):
                    vin = model.reg.ljust(17, "0")
                    car = self._load_cars_json(path, vin=vin)
                    self.assertEqual((car.battery_power, car.fuel_capacity), (model.battery_power, model.fuel_capacity))
                    self.assertEqual((car.max_elec_consumption, car.max_fuel_consumption),
                                     (model.max_elec_consumption, model.max_fuel_consumption))
                    with self.assertNoLogs(logger_name, level="INFO"):
                        again = Cars.load_cars(path)[0]
                    # abrp_name: a missing one is filled from the first model with that name, also without correction
                    self.assertEqual(self._car_values(again)[2:], self._car_values(car)[2:])
                    self.assertEqual(again.label, car.label)

    def test_cars_json_with_user_values_is_not_changed(self):
        logger_name = "psa_car_controller.psacc.model.car"
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "cars.json")
            for stored, values in (
                    # one value that isn't from the unknown model is enough, the label is still replaced as before
                    ({"battery_power": 50}, ("C4 Shine 2023", None, 50, 50, 70, 30)),
                    ({"battery_power": 46.5}, ("C4 Shine 2023", None, 46.5, 50, 70, 30)),
                    ({"battery_power": 12, "label": "C4 Shine 2023", "fuel_capacity": 45},
                     ("C4 Shine 2023", None, 12, 45, 70, 30))):
                with self.subTest(stored=stored):
                    with self.assertNoLogs(logger_name, level="INFO"):
                        car = self._load_cars_json(path, **stored)
                    self.assertEqual(self._car_values(car), values)

    def test_cars_json_of_still_unknown_or_same_values_is_not_changed(self):
        logger_name = "psa_car_controller.psacc.model.car"
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "cars.json")
            with self.assertNoLogs(logger_name, level="INFO"):
                car = self._load_cars_json(path, vin="ZZZZZZZZZZ1234567")
            self.assertEqual(self._car_values(car), ("unknown", None, 46, 0, 70, 30))
            with patch.object(CarModelRepository(), "models",
                              [CarModel("same", 46, 0, reg="VR7BAHNSBP", abrp_name="code")]):
                with self.assertNoLogs(logger_name, level="INFO"):
                    car = self._load_cars_json(path)
            self.assertEqual(self._car_values(car), ("same", "code", 46, 0, 70, 30))

    def test_cars_json_with_ambiguous_values_only_warns(self):
        logger_name = "psa_car_controller.psacc.model.car"
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "cars.json")
            for vin, stored, values in (
                    # label replaced by a previous start, the tank came from the model
                    ("VR3F3DGY2R1234567", {"label": "408 GT Hybrid 225", "fuel_capacity": 40},
                     ("408 GT Hybrid 225", None, 46, 40, 70, 30)),
                    # 0 kWh in car_models.yml, but a mild hybrid with a 48 V battery
                    ("VR3KAHPY1R1234567", {"label": "3008-III", "fuel_capacity": 53},
                     ("3008-III", None, 46, 53, 70, 30)),
                    ("VR3FPHPY0S1234567", {"label": "408 GT 1.2 Petrol", "fuel_capacity": 52},
                     ("408 GT 1.2 Petrol", None, 46, 52, 70, 30)),
                    ("VR7ARHPYEP1234567", {"label": "C5 Aircross Hybrid E Series", "fuel_capacity": 53},
                     ("C5 Aircross Hybrid E Series", None, 46, 53, 70, 30)),
                    ("VR3KCZKZ8R1234567", {"label": "e-5008", "abrp_name": "peugeot:e5008:25:77"},
                     ("e-5008", "peugeot:e5008:25:77", 46, 0, 70, 30)),
                    # abrp_name set by hand, as docs/abrp.md describes
                    ("VR3KCZKZ8R1234567", {"abrp_name": "own"}, ("e-5008", "own", 46, 0, 70, 30))):
                with self.subTest(stored=stored):
                    with self.assertLogs(logger_name, level="INFO") as logs:
                        car = self._load_cars_json(path, vin=vin, **stored)
                    self.assertEqual(self._car_values(car), values)
                    self.assertEqual([record.levelname for record in logs.records], ["WARNING"])
                    self.assertNotIn(vin, logs.output[0])
                    self.assertIn("***" + vin[-4:], logs.output[0])

    def test_cars_json_of_combustion_car_with_46_kwh_is_corrected(self):
        logger_name = "psa_car_controller.psacc.model.car"
        vin = self.UNKNOWN_CAR["vin"]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "cars.json")
            for stored, expected in (
                    # label replaced by a previous start, the tank came from the model
                    ({"label": "C4 Shine 2023", "fuel_capacity": 50}, ("C4 Shine 2023", None, 0, 50, 70, 30)),
                    ({"label": "C4 Shine 2023"}, ("C4 Shine 2023", None, 0, 50, 70, 30)),
                    # the other values are the user's, they stay
                    ({"fuel_capacity": 45}, ("C4 Shine 2023", None, 0, 45, 70, 30)),
                    ({"max_elec_consumption": 55}, ("C4 Shine 2023", None, 0, 50, 55, 30)),
                    ({"max_fuel_consumption": 12}, ("C4 Shine 2023", None, 0, 50, 70, 12)),
                    ({"label": "C4 Shine 2023", "abrp_name": "own", "fuel_capacity": 50},
                     ("C4 Shine 2023", "own", 0, 50, 70, 30)),
                    ({"battery_power": 46.0, "label": "C4 Shine 2023"}, ("C4 Shine 2023", None, 0, 50, 70, 30))):
                with self.subTest(stored=stored):
                    with self.assertLogs(logger_name, level="INFO") as logs:
                        car = self._load_cars_json(path, **stored)
                    self.assertEqual(self._car_values(car), expected)
                    self.assertEqual(car.precond_programs, {"program1": {}})
                    self.assertTrue(car.is_thermal())
                    self.assertFalse(car.is_hybrid())
                    self.assertEqual([record.levelname for record in logs.records], ["INFO"])
                    self.assertNotIn(vin, logs.output[0])
                    self.assertIn("***" + vin[-4:], logs.output[0])
                    self.assertIn("charges already in the database", logs.output[0])
                    with open(path, encoding="utf-8") as file:
                        saved = json.load(file)[0]
                    self.assertEqual(tuple(saved[field] for field in self.CAR_VALUES), expected)
                    # second start: nothing to correct, no log, the file stays as it is
                    with open(path, encoding="utf-8") as file:
                        content = file.read()
                    with self.assertNoLogs(logger_name, level="INFO"):
                        again = Cars.load_cars(path)[0]
                    self.assertEqual(self._car_values(again), expected)
                    with open(path, encoding="utf-8") as file:
                        self.assertEqual(file.read(), content)

    def test_cars_json_model_without_a_battery_value(self):
        for battery_power, expected in ((None, 0), (0, 0)):
            with self.subTest(battery_power=battery_power), tempfile.TemporaryDirectory() as tmp:
                models = [CarModel("thermal", battery_power, 50, reg="VR7BAHNSBP")]
                with patch.object(CarModelRepository(), "models", models):
                    car = self._load_cars_json(os.path.join(tmp, "cars.json"), label="thermal", fuel_capacity=50)
                self.assertEqual(car.battery_power, expected)
                self.assertTrue(car.is_thermal())
        # a key missing in car_models.yml falls back to 46 kWh, that's not "no battery"
        yaml = ruamel.yaml.YAML()
        yaml.register_class(CarModel)
        models = yaml.load("- !CarModel\n  name: thermal\n  fuel_capacity: 50\n  reg: VR7BAHNSBP\n")
        with tempfile.TemporaryDirectory() as tmp, patch.object(CarModelRepository(), "models", models):
            car = self._load_cars_json(os.path.join(tmp, "cars.json"), label="thermal", fuel_capacity=50)
        self.assertEqual(car.battery_power, 46)

    def test_cars_json_correction_of_46_kwh_for_every_model(self):
        def engine_code(model):
            return model.reg[5:8]

        models = CarModelRepository().models
        self.assertEqual(len(models), 153)
        corrected = []
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "cars.json")
            for model in models:
                with self.subTest(model=model.name, reg=model.reg):
                    car = self._load_cars_json(path, vin=model.reg.ljust(17, "0"), label=model.name,
                                               fuel_capacity=model.fuel_capacity)
                    if car.battery_power != 46:
                        corrected.append(model)
                        self.assertEqual(car.battery_power, 0)
                        self.assertTrue(car.is_thermal())
        self.assertEqual(len(corrected), 51)
        with_battery = {engine_code(model) for model in models if model.battery_power}
        for model in corrected:
            with self.subTest(corrected=model.name, reg=model.reg):
                self.assertIs(type(model), CarModel)
                self.assertEqual(model.battery_power, 0)
                self.assertGreater(model.fuel_capacity, 0)
                self.assertNotRegex(model.name, "(?i)hybrid|phev|mhev|plug|tense|electric|^e-")
                # same engine as a car with a battery: a mild hybrid (MILD_HYBRID_ENGINE_CODES) or wrong data
                self.assertNotIn(engine_code(model), with_battery)
        without_battery = [model.reg for model in models if not model.battery_power]
        self.assertEqual(sorted(set(without_battery) - {model.reg for model in corrected}),
                         ["VR3FPHPY0S", "VR3KAHPY1R", "VR7ARHPYEP", "VXKUPHNEKP"])

    def test_car_models_with_a_wrong_powertrain(self):
        # the 1.2 Hybrid 136 (engine code HPY) can't be charged from outside, the 13.2 kWh are from the plug-in hybrid
        for reg, name, tank in (("VR7ARHPYEP", "C5 Aircross Hybrid E Series", 53),
                                ("VR3KAHPY1R", "3008-III", 53),
                                ("VR3FPHPY0S", "408 GT 1.2 Petrol", 52),
                                # HNE is the 1.2 PureTech 100 petrol engine, was an ElecModel "Vauxhall Corsa Electric"
                                ("VXKUPHNEKP", "Vauxhall Corsa (2023)", 44)):
            with self.subTest(reg=reg):
                model = CarModelRepository().find_model_by_vin(reg + "1234567")
                self.assertIs(type(model), CarModel)
                self.assertEqual((model.name, model.battery_power, model.fuel_capacity), (name, 0, tank))
                self.assertIsNone(model.abrp_name)
                car = Car(reg + "1234567", "id", "brand")
                self.assertTrue(car.is_thermal())
                self.assertFalse(car.is_hybrid())
                self.assertFalse(car.has_battery())

    def test_cars_json_of_a_car_model_with_a_wrong_powertrain(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "cars.json")
            # stored values win over the model, also 13.2 kWh: as before only cars.json changes that
            car = self._load_cars_json(path, vin="VR7ARHPYEP1234567", label="C5 Aircross Hybrid E Series",
                                       battery_power=13.2, fuel_capacity=53)
            self.assertEqual(self._car_values(car), ("C5 Aircross Hybrid E Series", None, 13.2, 53, 70, 30))
            self.assertTrue(car.is_hybrid())

    def test_cars_json_of_uncertain_powertrain_only_warns(self):
        logger_name = "psa_car_controller.psacc.model.car"
        vin = "VXKUPHNEKP1234567"
        name = "Vauxhall Corsa (2023)"
        abrp_name = "vauxhall:corsae:20:50"
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "cars.json")
            for stored, expected, is_electric in (
                    # saved with the old entry, an ElecModel: no tank from the new entry, a real Corsa-e stays electric
                    ({"label": "Vauxhall Corsa Electric", "abrp_name": abrp_name, "max_fuel_consumption": 0},
                     (name, abrp_name, 46, 0, 70, 0), True),
                    # exactly the unknown model, the values are the same as the ones of a Corsa-e
                    ({}, (name, None, 46, 0, 70, 30), True),
                    # label already replaced by a previous start
                    ({"label": name, "abrp_name": abrp_name}, (name, abrp_name, 46, 0, 70, 30), True),
                    ({"label": name, "fuel_capacity": 44}, (name, None, 46, 44, 70, 30), False),
                    # other values than the defaults warn too, the question is the same
                    ({"label": name, "fuel_capacity": 44, "max_fuel_consumption": 12},
                     (name, None, 46, 44, 70, 12), False)):
                with self.subTest(stored=stored):
                    with self.assertLogs(logger_name, level="INFO") as logs:
                        car = self._load_cars_json(path, vin=vin, **stored)
                    self.assertEqual(self._car_values(car), expected)
                    self.assertEqual(car.is_electric(), is_electric)
                    self.assertEqual(car.precond_programs, {"program1": {}})
                    self.assertEqual([record.levelname for record in logs.records], ["WARNING"])
                    self.assertNotIn(vin, logs.output[0])
                    self.assertIn("***" + vin[-4:], logs.output[0])
                    # says what to check and what to do in both cases
                    self.assertIn("petrol", logs.output[0])
                    self.assertIn("set battery_power to 0", logs.output[0])
                    self.assertIn('set label to "corsa-e"', logs.output[0])
                    # next start: the same warning until the user decides, the file stays as it is
                    with open(path, encoding="utf-8") as file:
                        content = file.read()
                    with self.assertLogs(logger_name, level="INFO") as logs:
                        again = Cars.load_cars(path)[0]
                    self.assertEqual(self._car_values(again), expected)
                    self.assertEqual([record.levelname for record in logs.records], ["WARNING"])
                    with open(path, encoding="utf-8") as file:
                        self.assertEqual(file.read(), content)

    def test_cars_json_of_uncertain_powertrain_after_the_user_decided(self):
        logger_name = "psa_car_controller.psacc.model.car"
        vin = "VXKUPHNEKP1234567"
        name = "Vauxhall Corsa (2023)"
        self.assertEqual(UNCERTAIN_POWERTRAIN_MODELS, {"VXKUPHNEKP": "corsa-e"})
        model = CarModelRepository().find_model_by_name("corsa-e")
        self.assertIs(type(model), ElecModel)
        self.assertEqual(model.battery_power, 46)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "cars.json")
            for stored, expected, is_electric in (
                    # the advice of the warning for each case, whatever fuel_capacity the first starts left
                    ({"label": "corsa-e", "max_fuel_consumption": 0},
                     ("corsa-e", "opel:corsae:20:50", 46, 0, 70, 0), True),
                    ({"label": "corsa-e", "fuel_capacity": 0, "abrp_name": "own"},
                     ("corsa-e", "own", 46, 0, 70, 30), True),
                    ({"label": name, "battery_power": 0}, (name, None, 0, 44, 70, 30), False),
                    ({"label": name, "battery_power": 0, "fuel_capacity": 44}, (name, None, 0, 44, 70, 30), False)):
                with self.subTest(stored=stored):
                    with self.assertNoLogs(logger_name, level="INFO"):
                        car = self._load_cars_json(path, vin=vin, **stored)
                        again = Cars.load_cars(path)[0]
                    self.assertEqual(self._car_values(car), expected)
                    self.assertEqual(self._car_values(again), expected)
                    self.assertEqual(car.is_electric(), is_electric)
                    self.assertEqual(car.is_thermal(), not is_electric)
            # a new car without saved values gets the entry of the VIN, the two with the same engine are still corrected
            self.assertTrue(Car(vin, "id", "brand").is_thermal())
            for sibling, label in (("VXKUPHNEKM1234567", "Vauxhall Corsa (2020)"),
                                   ("VXKUPHNEKR1234567", "Opel Corsa Facelift (2024)")):
                with self.subTest(sibling=sibling):
                    car = self._load_cars_json(path, vin=sibling, label=label, fuel_capacity=44)
                    self.assertEqual((car.battery_power, car.fuel_capacity), (0, 44))

    def test_cars_json_with_another_chosen_model_is_not_changed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "cars.json")
            with self.assertNoLogs("psa_car_controller.psacc.model.car", level="INFO"):
                car = self._load_cars_json(path, label="408 GT Hybrid 225", fuel_capacity=40)
            self.assertEqual((car.label, car.battery_power, car.fuel_capacity), ("408 GT Hybrid 225", 46, 40))

    def test_default_limits_only_discard_outlier_trips(self):
        def is_kept(car, consumption_km=0, consumption_fuel_km=0):
            trip = Trip()
            trip.car = car
            trip.speed_average = 50
            trip.consumption_km = consumption_km
            trip.consumption_fuel_km = consumption_fuel_km
            return Trips().check_and_append(trip)

        electric = Car("VR3FMZKW3R1234567", "vehicle_id", "brand")
        thermal = Car("VR7BAHNSBP1234567", "vehicle_id", "brand")
        self.assertTrue(is_kept(electric, consumption_km=35))
        self.assertTrue(is_kept(electric, consumption_km=70))
        self.assertFalse(is_kept(electric, consumption_km=71))
        self.assertTrue(is_kept(thermal, consumption_fuel_km=15))
        self.assertFalse(is_kept(thermal, consumption_fuel_km=31))

    def test_c02_signal_cache(self):
        start = datetime.utcnow().replace(tzinfo=UTC) - timedelta(minutes=30)
        end = datetime.utcnow().replace(tzinfo=UTC)
        Ecomix._cache = {'FR': [[start - timedelta(days=1), 100],
                                [start + timedelta(minutes=1), 10],
                                [start + timedelta(minutes=2), 20],
                                [start + timedelta(minutes=3), 30]]}
        assert Ecomix.get_co2_from_signal_cache(start, end, "FR") == 20
        Ecomix.clean_cache()
        self.assertEqual(3, len(Ecomix._cache["FR"]))

    def test_c02_signal(self):
        if self.test_online:
            PSAClient.load_config("./config/config.json")
            def_country = "FR"
            Ecomix.get_data_from_co2_signal(latitude, longitude, def_country)
            res = Ecomix.get_co2_from_signal_cache(datetime.utcnow().replace(tzinfo=UTC) - timedelta(minutes=5),
                                                   datetime.utcnow().replace(tzinfo=UTC), def_country)
            self.assertIsInstance(res, (int, float))

    def test_charge_control(self):
        with tempfile.TemporaryDirectory() as tmp:
            charge_control = ChargeControls()
            charge_control.file_name = os.path.join(tmp, "test_charge_control.json")
            charge_control.save_config(force=True)

    def test_battery_curve(self):
        vin = self.vehicule_list[0].vin
        get_new_test_db()
        record_charging()
        charge = Database.get_last_charge(vin)
        self.assertEqual(charge.stop_at, date4)
        res = Database.get_battery_curve(Database.get_db(), date0, date4, vin)
        self.assertEqual(4, len(res))

    def test_set_charge_price(self):
        vin = self.vehicule_list[0].vin
        get_new_test_db()
        record_charging()
        charge = Database.get_last_charge(vin)
        charge.price = 42
        conn = Database.get_db()
        Database.set_chargings_price(conn, charge)
        conn.close()
        res = Database.get_charge(vin, charge.start_at)
        self.assertEqual(charge.price, res.price)

    def test_sdk(self):
        api = ApiClient()
        status: psa.connected_car_api.models.status.Status = api._ApiClient__deserialize(FUEL_CAR_STATUS, "Status")
        geocode_res = reverse_geocode.search([(status.last_position.geometry.coordinates[:2])[::-1]])[0]
        get_new_test_db()
        car = Car("XX", "vid", "Peugeot")
        car.status = status
        myp = PSAClient.load_config(DATA_DIR + "config.json")
        myp.record_info(car)
        assert geocode_res["country_code"] == "DE"
        assert "features" in json.loads(Database.get_recorded_position())
        # electric should be first
        assert car.status.energy[0].type == 'Electric'

    @patch("psa_car_controller.psacc.repository.db.Database.record_position")
    def test_electric_record_info(self, mock_db):
        api = ApiClient()
        status: psa.connected_car_api.models.status.Status = api._ApiClient__deserialize(ELECTRIC_CAR_STATUS, "Status")
        get_new_test_db()
        car = self.vehicule_list[0]
        car.status = status
        myp = PSAClient.load_config(DATA_DIR + "config.json")
        myp.record_info(car)
        db_record_position_arg = mock_db.call_args_list[0][0]
        expected_result = (None, 'VR3UHZKX', 3196.5, 47.274, -1.59008, 30,
                           datetime(2022, 3, 26, 11, 2, 54, tzinfo=tzutc()),
                           59.0,
                           None,
                           True,
                           None)
        self.assertEqual(db_record_position_arg, expected_result)

    @patch("psa_car_controller.psacc.repository.db.Database.record_battery_soh")
    @patch("psa_car_controller.psacc.repository.db.Database.record_position")
    def test_electric_record_info_v2(self, mock_db, moock_soh):
        api = ApiClient()
        status: psa.connected_car_api.models.status.Status = api._ApiClient__deserialize(
            ELECTRIC_CAR_STATUS_V2, "Status")
        get_new_test_db()
        car = self.vehicule_list[0]
        car.status = status
        myp = PSAClient.load_config(DATA_DIR + "config.json")
        myp.record_info(car)
        db_record_position_arg = mock_db.call_args_list[0][0]
        expected_result = (None, 'VR3UHZKX', 3196.5, 47.274, -1.59008, 30,
                           datetime(2022, 3, 26, 11, 2, 54, tzinfo=tzutc()),
                           59.0,
                           None,
                           True,
                           None)
        self.assertEqual(db_record_position_arg, expected_result)
        self.assertEqual(
            ('VR3UHZKX',
             datetime(
                 2022,
                 3,
                 26,
                 11,
                 2,
                 54,
                 tzinfo=tzutc()),
             90),
            moock_soh.call_args_list[0][0])

    def test_record_position_charging(self):
        get_new_test_db()
        config_repository.CONFIG_FILENAME = DATA_DIR + "config.ini"
        car = self.vehicule_list[0]
        record_position()
        utils.get_positions = MagicMock(
            return_value=[{'dataset': 'srtm30m', 'elevation': 40.0, 'location': {'lat': 47.2183, 'lng': -1.60362}}])
        Database.add_altitude_to_db(Database.get_db())
        data = json.loads(Database.get_recorded_position())
        assert data["features"][1]["geometry"]["coordinates"] == [float(longitude), float(latitude)]
        trips = Trips.get_trips(self.vehicule_list)[car.vin]
        trip = trips[0]
        map(trip.add_temperature, [10, 13, 15])
        res = trip.get_info()
        assert compare_dict(res, {'consumption_km': 24.21052631578947,
                                  'start_at': date0,
                                  'end_at': date2,
                                  'consumption_by_temp': None,
                                  'positions': {'lat': [latitude], 'long': [longitude]},
                                  'duration': 40.0, 'speed_average': 28.5, 'distance': 19.0, 'mileage': 30.0,
                                  'altitude_diff': 2, 'id': 1, 'consumption': 4.6, 'estimated': False})

        Charging.elec_price = ConfigRepository.read_config(DATA_DIR + "config.ini").Electricity_config
        start_level = 40
        end_level = 85
        mileage = 123456789.1
        Charging.record_charging(
            car,
            "InProgress",
            date0,
            start_level,
            latitude,
            longitude,
            None,
            "slow",
            20,
            60,
            mileage)
        Charging.record_charging(car, "InProgress", date1, 70, latitude, longitude, "FR", "slow", 20, 60, mileage)
        Charging.record_charging(car, "InProgress", date1, 70, latitude, longitude, "FR", "slow", 20, 60, mileage)
        Charging.record_charging(car, "InProgress", date2, 80, latitude, longitude, "FR", "slow", 20, 60, mileage)
        # periods of a quarter of an hour, from 11:00 to 12:00
        eco2mix = MagicMock(text="<liste>" + "".join(f'<valeur periode="{period}">{period - 4}</valeur>'
                                                     for period in range(40, 52)) + "</liste>")
        with patch("requests.get", return_value=eco2mix) as get:
            Charging.record_charging(car, "Stopped", date3, end_level, latitude, longitude, "FR", "slow", 20, 60,
                                     mileage)
        self.assertIn("eco2mix.rte-france.com", get.call_args.args[0])
        chargings = Charging.get_chargings()
        co2 = chargings[0]["co2"]
        assert isinstance(co2, float)
        self.assertEqual(41.5, co2)
        assert compare_dict(chargings, [{'start_at': date0,
                                         'stop_at': date3,
                                         'duration_str': duration_str,
                                         'duration_min': duration_min,
                                         'VIN': 'VR3UHZKX',
                                         'start_level': 40,
                                         'end_level': 85,
                                         'co2': co2,
                                         'kw': 20.7,
                                         'price': 4.29,
                                         'charging_mode': 'slow',
                                         'mileage': 123456789.1}])
        assert get_figures(car)
        row = {"start_at": date0.strftime('%Y-%m-%dT%H:%M:%S.000Z'),
               "stop_at": date3.strftime('%Y-%m-%dT%H:%M:%S.000Z'), "start_level": start_level, "end_level": end_level}
        battery_curve_fix = get_battery_curve_fig(row, car)
        assert battery_curve_fix is not None
        # The clientside JS rewrites start_at/stop_at to millisecond timestamps (date.getTime()),
        # so the callback receives ints, not ISO strings.
        row_ms = {"start_at": int(date0.timestamp() * 1000),
                  "stop_at": int(date3.timestamp() * 1000), "start_level": start_level, "end_level": end_level}
        battery_curve_fix_ms = get_battery_curve_fig(row_ms, car)
        assert battery_curve_fix_ms is not None
        assert get_altitude_fig(trip) is not None

    def test_fuel_car(self):
        get_new_test_db()
        config_repository.CONFIG_FILENAME = DATA_DIR + "config.ini"
        car = self.vehicule_list[1]
        Database.record_position(None, car.vin, 11, latitude, longitude, 22, date0, 40, 30, False, None)
        Database.record_position(None, car.vin, 20, latitude, longitude, 22, date1, 35, 29, False, None)
        Database.record_position(None, car.vin, 30, latitude, longitude, 22, date2, 30, 28, False, None)
        trips = Trips.get_trips(self.vehicule_list)
        res = trips[car.vin].get_trips_as_dict()
        assert compare_dict(res, [{'consumption_km': 6.947368421052632,
                                   'start_at': date0,
                                   'end_at': date2,
                                   'consumption_by_temp': None,
                                   'positions': {'lat': [latitude],
                                                 'long': [longitude]},
                                   'duration': 40.0,
                                   'speed_average': 28.5,
                                   'distance': 19.0,
                                   'mileage': 30.0,
                                   'altitude_diff': 0,
                                   'id': 1,
                                   'consumption': 1.32,
                                   'estimated': False,
                                   'consumption_fuel_km': 4.53}])

    def test_none_mileage(self):
        get_new_test_db()
        config_repository.CONFIG_FILENAME = DATA_DIR + "config.ini"
        car = self.vehicule_list[1]
        Database.record_position(None, car.vin, None, latitude, longitude, 22, get_date(1), 40, 30, False, None)
        Database.record_position(None, car.vin, None, latitude, longitude, 22, get_date(2), 35, 29, False, None)
        Database.record_position(None, car.vin, None, latitude, longitude, 22, get_date(3), 30, 28, False, None)
        start = get_date(4)
        end = get_date(6)
        Database.record_position(None, car.vin, 11, latitude, longitude, 22, start, 40, 30, False, None)
        Database.record_position(None, car.vin, 20, latitude, longitude, 22, get_date(5), 35, 29, False, None)
        Database.record_position(None, car.vin, 30, latitude, longitude, 22, end, 30, 28, False, None)
        trips = Trips.get_trips(self.vehicule_list)
        res = trips[car.vin].get_trips_as_dict()
        assert compare_dict(res, [{'consumption_km': 6.947368421052632,
                                   'start_at': start,
                                   'end_at': end,
                                   'consumption_by_temp': None,
                                   'positions': {'lat': [latitude],
                                                 'long': [longitude]},
                                   'duration': 120.0,
                                   'speed_average': 9.5,
                                   'distance': 19.0,
                                   'mileage': 30.0,
                                   'altitude_diff': 0,
                                   'id': 1,
                                   'consumption': 1.32,
                                   'estimated': False,
                                   'consumption_fuel_km': 4.53}])

    def test_elec_consumption_none_level(self):
        from psa_car_controller.psacc.application.trip_parser import TripParser
        start = {5: 50, 8: None}
        end = {5: None, 8: None}
        assert TripParser.get_elec_consumption(start, end) == [0, 0]

    def test_thermal_consumption_none_level(self):
        from psa_car_controller.psacc.application.trip_parser import TripParser
        start = {5: None, 8: 80}
        end = {5: None, 8: None}
        assert TripParser.get_thermal_consumption(start, end) == [0, 0]

    def test_db_callback(self):
        old_dummy_value = dummy_value
        get_new_test_db()
        Database.set_db_callback(callback_test)
        assert old_dummy_value == dummy_value
        Database.record_position(None, "xx", 11, latitude, longitude - 0.05, None, date0, 40, None, False, None)
        assert old_dummy_value != dummy_value

    def test_parse_hour(self):
        expected_res = [[2, 0, 0], [3, 14, 0], [0, 0, 2], [0, 30, 0]]
        assert expected_res == [parse_hour(h) for h in ["PT2H", "PT3H14", "PT2S", "PT30M"]]

    def test_rate_limit(self):
        @rate_limit(2, 10)
        def test_fct():
            pass

        test_fct()
        test_fct()
        try:
            test_fct()
            raise Exception("It should have raise RateLimitException")
        except RateLimitException:
            pass

    def test_parse_apk(self):
        filename = "mypeugeot.apk"
        github = FakeGithub({filename + ".bz2": build_apk_archive()})
        # the apk, its archive and certs/ end up in the working directory
        with tempfile.TemporaryDirectory() as tmp, contextlib.chdir(tmp), \
                patch("requests.get", side_effect=github.get), \
                patch("psa_car_controller.psa.setup.apk_parser.APK", FakeApk):
            apk_parser = get_content_from_apk(filename, "FR")
            self.assertEqual("https://brandid.example", apk_parser.host_brandid_prod)
            self.assertEqual("fr_FR", apk_parser.culture)
            self.assertEqual("client-id", apk_parser.client_id)
            self.assertEqual("client-secret", apk_parser.client_secret)
            self.assertEqual("AP_FR_ESP", apk_parser.site_code)
            self.assertEqual("1.2.3", apk_parser.version)
            with open("certs/private.pem", "rb") as f:
                self.assertIsNotNone(load_pem_private_key(f.read(), None))
            with open("certs/public.pem", "rb") as f:
                self.assertIsNotNone(load_pem_x509_certificate(f.read()))
            assert github_file_need_to_be_downloaded(GITHUB_USER, GITHUB_REPO, "", filename + ".bz2") is False

    def test_parse_real_apk(self):
        """FakeApk replaces androguard everywhere else: this one reads the format of the real application.

        The archive weighs 31 MB and holds the credentials of the application, so it isn't part of the repository.
        It's the file github.com/flobz/psa_apk publishes, read from PSACC_TEST_APK or the root of the repository.
        """
        archive = os.environ.get("PSACC_TEST_APK", REAL_APK_ARCHIVE)
        if not os.path.isfile(archive):
            self.skipTest(f"real apk not available: {archive}")
        with tempfile.TemporaryDirectory() as tmp, contextlib.chdir(tmp):
            with bz2.open(archive, "rb") as compressed, open("mypeugeot.apk", "wb") as apk:
                shutil.copyfileobj(compressed, apk)
            apk_parser = ApkParser("mypeugeot.apk", "FR")
            apk_parser.retrieve_content_from_apk()
            self.assert_real_apk_content(apk_parser)

    def assert_real_apk_content(self, apk_parser):
        self.assertRegex(apk_parser.host_brandid_prod, "^https?://")
        self.assertRegex(apk_parser.culture, "^[a-z]{2}_[A-Z]{2}$")
        self.assertEqual("AP_FR_ESP", apk_parser.site_code)
        for value in (apk_parser.client_id, apk_parser.client_secret, apk_parser.version):
            self.assertIsInstance(value, str)
            self.assertTrue(value)
        with open("certs/private.pem", "rb") as f:
            self.assertIsNotNone(load_pem_private_key(f.read(), None))
        with open("certs/public.pem", "rb") as f:
            self.assertIsNotNone(load_pem_x509_certificate(f.read()))

    def test_parse_apk_online(self):
        """Run every night by .github/workflows/apk-format.yml, to notice a new format of the published application."""
        if not self.test_online:
            self.skipTest("set TEST_ONLINE=1 to use github.com/flobz/psa_apk")
        filename = "mypeugeot.apk"
        with tempfile.TemporaryDirectory() as tmp, contextlib.chdir(tmp):
            self.assert_real_apk_content(get_content_from_apk(filename, "FR"))
            assert github_file_need_to_be_downloaded(GITHUB_USER, GITHUB_REPO, "", filename + ".bz2") is False

    def test_file_need_to_be_updated(self):
        filename = "mypeugeot.apk.bz2"
        github = FakeGithub({filename: b"the content published on github"})
        with tempfile.TemporaryDirectory() as tmp, contextlib.chdir(tmp), \
                patch("requests.get", side_effect=github.get):
            with open(filename, "w") as f:
                f.write(" ")
            assert github_file_need_to_be_downloaded(GITHUB_USER, GITHUB_REPO, "", filename) is True
            # same content as on github: nothing to download
            with open(filename, "wb") as f:
                f.write(b"the content published on github")
            assert github_file_need_to_be_downloaded(GITHUB_USER, GITHUB_REPO, "", filename) is False

    def test_validate_preconditioning_programs(self):
        valid = {
            "program1": {"day": [0, 0, 0, 0, 0, 0, 0], "hour": 34, "minute": 0, "on": 0},
            "program2": {"day": [1, 1, 1, 1, 1, 0, 0], "hour": 7, "minute": 30, "on": 1},
            "program3": {"day": [0, 0, 0, 0, 0, 1, 1], "hour": 23, "minute": 59, "on": 0},
            "program4": {"day": [0, 0, 0, 0, 0, 0, 0], "hour": 34, "minute": 0, "on": 0},
        }
        self.assertEqual(valid, validate_preconditioning_programs(valid))
        # A missing program is rejected.
        missing = {k: v for k, v in valid.items() if k != "program4"}
        self.assertRaises(ValueError, validate_preconditioning_programs, missing)
        # Day list must have exactly 7 flags.
        bad_day = json.loads(json.dumps(valid))
        bad_day["program1"]["day"] = [0, 0, 0]
        self.assertRaises(ValueError, validate_preconditioning_programs, bad_day)
        # Hour out of range (and not the 34 sentinel) is rejected.
        bad_hour = json.loads(json.dumps(valid))
        bad_hour["program2"]["hour"] = 25
        self.assertRaises(ValueError, validate_preconditioning_programs, bad_hour)
        # on must be 0 or 1.
        bad_on = json.loads(json.dumps(valid))
        bad_on["program2"]["on"] = 2
        self.assertRaises(ValueError, validate_preconditioning_programs, bad_on)
        # A bool is not a valid int for hour.
        bad_bool = json.loads(json.dumps(valid))
        bad_bool["program2"]["hour"] = True
        self.assertRaises(ValueError, validate_preconditioning_programs, bad_bool)
        self.assertRaises(ValueError, validate_preconditioning_programs, [])

    def test_car_model_duplication(self):
        car_models = CarModelRepository().models
        vins = []
        for model in car_models:
            self.assertFalse(model.reg in vins, f"duplicate vin prefix {model.reg}")
            for vin in vins:
                self.assertFalse(vin in model.reg, f"duplicate vin with shorter prefix {vin}")
            vins.append(model.reg)


if __name__ == '__main__':
    my_logger(handler_level=os.environ.get("DEBUG_LEVEL", 20))
    unittest.main()
