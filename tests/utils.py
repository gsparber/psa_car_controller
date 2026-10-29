# flake8: noqa

import importlib
import os
import time
from datetime import timedelta, datetime
from unittest.mock import MagicMock, patch

import pytz
from dash import html
from deepdiff import DeepDiff

from psa_car_controller.psa.RemoteClient import RemoteClient
from psa_car_controller.psacc.application.charging import Charging
from psa_car_controller.psacc.model.car import Cars, Car
from psa_car_controller.psacc.repository.db import Database
from psa_car_controller.web import app as web_app
from psa_car_controller.web.tools import jobs

DATA_DIR = os.path.dirname(os.path.realpath(__file__)) + "/data/"


def get_web_view(name):
    """Import a page of the web app. On import, pages register Dash callbacks, so the Dash app has to exist first,
    and create the app singleton, which parses the command line: that must not be the one of the test runner."""
    module = f"psa_car_controller.web.view.{name}"
    with patch("sys.argv", ["psa-car-controller"]):
        if web_app.app is None:
            web_app.config_flask("test", "/", False, "127.0.0.1", 0, view=module)
            web_app.dash_app.layout = html.Div()  # dash refuses requests without one
        return importlib.import_module(module)



def isolate_jobs(test_case):
    """Background jobs of a test stay in a registry of their own."""
    registry = jobs.JobRegistry()
    patcher = patch.object(jobs, "JOBS", registry)
    patcher.start()
    test_case.addCleanup(patcher.stop)
    return registry


def wait_for_job(kind, registry=None, timeout=10):
    registry = registry or jobs.JOBS
    deadline = time.monotonic() + timeout
    while registry.active(kind) is not None:
        if time.monotonic() > deadline:
            raise TimeoutError(f"{kind} job still running")
        time.sleep(0.005)


latitude = 47.2183
longitude = -1.55362
date3 = datetime.utcnow().replace(2021, 3, 1, 12, 00, 00, 00, tzinfo=pytz.UTC)
date2 = date3 - timedelta(minutes=20)
date1 = date3 - timedelta(minutes=40)
date0 = date3 - timedelta(minutes=60)
date4 = date3 + timedelta(minutes=1)
duration_min = (date3 - date0).seconds / 60
duration_str = str(date3 - date0)

vehicule_list = Cars()
vehicule_list.extend(
    [Car("VR3UHZKX", "vid", "Peugeot"), Car("VXXXXX", "XXXX", "Peugeot", label="SUV 3008 Hybrid 225")])
car = vehicule_list[0]


def get_new_test_db():
    Database.close_db()
    conn = Database.get_db(force_new_conn=True, db_file="")
    return conn


def record_position():
    Database.record_position(None, car.vin, 11, latitude, longitude - 0.05, None, date0, 40, None, False, None)
    Database.record_position(None, car.vin, 20, latitude, longitude, 32, date1, 35, None, False, None)
    Database.record_position(None, car.vin, 30, latitude, longitude, 42, date2, 30, None, False, None)
    Database.record_position(None, car.vin, None, latitude, longitude, 42, date2, 30, None, False, None)


def record_charging():
    Charging.record_charging(car, "InProgress", date0, 50, latitude, longitude, "FR", "slow", 20, 60, 123456789.1)
    Charging.record_charging(car, "InProgress", date1, 75, latitude, longitude, "FR", "slow", 20, 60, 123456789.1)
    Charging.record_charging(car, "InProgress", date2, 85, latitude, longitude, "FR", "slow", 20, 60, 123456789.1)
    Charging.record_charging(car, "InProgress", date3, 90, latitude, longitude, "FR", "slow", 20, 60, 123456789.1)
    with patch("psa_car_controller.psacc.application.ecomix.Ecomix.get_data_france", return_value=50.0):
        Charging.record_charging(car, "Stopped", date4, 91, latitude, longitude, "FR", "slow", 20, 60, 123456789.1)


def get_date(offset):
    return date3 + timedelta(minutes=60 * offset)


def get_rc() -> RemoteClient:
    account_info = MagicMock()
    account_info.realm = ""
    return RemoteClient(account_info, Cars(), None, None)


def compare_dict(result, expected):
    diff = DeepDiff(expected, result)
    if diff != {}:
        raise AssertionError(str(diff))
    return True
