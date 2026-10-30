"""Charges estimated from the battery level between two trips (T45): the positions go through the real Trips parser."""
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from dash import html

from psa_car_controller.psacc.application.charging import Charging
from psa_car_controller.psacc.model.car import Cars
from psa_car_controller.psacc.repository.db import Database
from psa_car_controller.psacc.repository.trips import Trips
from psa_car_controller.web import app as web_app
from psa_car_controller.web.view import insights_data
from tests.utils import car, get_new_test_db, latitude, longitude

START = datetime(2026, 1, 5, 8, 0, 0, tzinfo=timezone.utc)
STEP = timedelta(minutes=10)
CARS = Cars([car])


def get_views():
    with patch("sys.argv", ["psa-car-controller"]):  # the app singleton parses the command line
        # run alone: the integration tests set the app up otherwise, so this file must sort after them
        if web_app.app is None:
            web_app.config_flask("test", "/", False, "127.0.0.1", 0, view="psa_car_controller.web.view.api")
            web_app.dash_app.layout = html.Div()  # dash refuses requests without one
        # pylint: disable=import-outside-toplevel
        from psa_car_controller.web.view import api, charge
    return api, charge


def record_point(date, mileage, level, vin=car.vin):
    Database.record_position(None, vin, mileage, latitude, longitude, 32, date, level, None, True, 10)


def drive(start, mileage, level):
    """A 20 km trip using 2 %; returns its last point."""
    for step in range(3):
        record_point(start + step * STEP, mileage + step * 10, level - step)
    return start + 2 * STEP, mileage + 20, level - 2


def two_trips(gap=timedelta(hours=8), rise=15, moved=0):
    """A trip ending at 78 % on km 120, then one starting after the gap at 78 % + rise. Returns the gap's bounds."""
    end_at, mileage, level = drive(START, 100, 80)
    stop_at = end_at + gap
    drive(stop_at, mileage + moved, level + rise)
    return end_at, stop_at


def add_real_charge(start_at, stop_at, vin=car.vin):
    conn = Database.get_db()
    conn.execute("INSERT INTO battery(start_at,stop_at,VIN,start_level,end_level,kw,price) VALUES(?,?,?,?,?,?,?)",
                 (start_at, stop_at, vin, 40, 80, 20.7, 4.29))
    conn.commit()


class EstimatedChargingTest(unittest.TestCase):
    def setUp(self):
        get_new_test_db()
        self.addCleanup(get_new_test_db)

    def estimated(self):
        return Charging.get_estimated_chargings(CARS)

    def test_rise_between_trips_gives_an_estimated_charge(self):
        end_at, stop_at = two_trips()
        self.assertEqual([{"start_at": end_at, "stop_at": stop_at, "VIN": car.vin, "start_level": 78, "end_level": 93,
                           "mileage": 120, "co2": None, "kw": None, "price": None, "charging_mode": None,
                           "estimated": True}], self.estimated())

    def test_no_rise_gives_nothing(self):
        two_trips(rise=-1)
        self.assertEqual([], self.estimated())

    def test_gap_limit(self):
        two_trips(gap=timedelta(hours=72))
        self.assertEqual(1, len(self.estimated()))
        get_new_test_db()
        two_trips(gap=timedelta(hours=72, minutes=1))
        self.assertEqual([], self.estimated())

    def test_minimum_rise_parked(self):
        two_trips(rise=2)
        self.assertEqual([], self.estimated())
        get_new_test_db()
        two_trips(rise=3)
        self.assertEqual(1, len(self.estimated()))

    def test_minimum_rise_when_the_car_moved_in_between(self):
        # regeneration can do more than a parked car, e.g. trip without record down a mountain
        two_trips(gap=timedelta(hours=3), rise=5, moved=20)
        self.assertEqual([], self.estimated())
        get_new_test_db()
        two_trips(gap=timedelta(hours=3), rise=6, moved=20)
        self.assertEqual(1, len(self.estimated()))

    def test_unknown_level_gives_nothing(self):
        for step in range(3):
            record_point(START + step * STEP, 100 + step * 10, 80 - step)
        for step in range(3):
            record_point(START + timedelta(hours=8) + step * STEP, 120 + step * 10, None)
        self.assertEqual([], self.estimated())

    def test_overlap_with_a_recorded_charge_gives_nothing(self):
        end_at, stop_at = two_trips()
        hour = timedelta(hours=1)
        for start, stop in ((end_at + hour, stop_at - hour),  # inside
                            (end_at - hour, end_at + hour),  # ends in the gap
                            (stop_at - hour, stop_at + hour),  # starts in the gap
                            (end_at - hour, stop_at + hour),  # around the gap
                            (stop_at, stop_at + hour),  # touching
                            (end_at - hour, end_at),
                            (end_at + hour, None)):  # still running
            with self.subTest(start=start, stop=stop):
                add_real_charge(start, stop)
                self.assertEqual([], self.estimated())
                Database.get_db().execute("DELETE FROM battery")

    def test_charges_outside_the_gap_or_of_another_car_dont_matter(self):
        end_at, stop_at = two_trips()
        hour = timedelta(hours=1)
        add_real_charge(end_at - 2 * hour, end_at - hour)
        add_real_charge(stop_at + hour, stop_at + 2 * hour)
        add_real_charge(end_at + hour, stop_at - hour, vin="OTHER")
        self.assertEqual(1, len(self.estimated()))

    def test_recorded_charge_without_timezone(self):
        # rows written by old versions
        end_at, _ = two_trips()
        naive = {"start_at": (end_at + timedelta(hours=1)).replace(tzinfo=None), "stop_at": None}
        trips = Trips.get_trips(CARS)
        self.assertEqual([], Charging.estimate_chargings(trips[car.vin], [naive]))

    def test_estimates_stay_out_of_the_recorded_charges(self):
        end_at, stop_at = two_trips()
        add_real_charge(end_at - timedelta(hours=2), end_at - timedelta(hours=1))
        add_real_charge(stop_at + timedelta(hours=1), stop_at + timedelta(hours=2))
        before_rows = insights_data.charge_rows(Charging.get_chargings())
        before_totals = insights_data.charge_totals(before_rows)
        estimated = self.estimated()
        self.assertEqual(1, len(estimated))

        self.assertEqual(2, len(Charging.get_chargings()))
        self.assertNotIn("estimated", Charging.get_chargings()[0])
        self.assertEqual(2, Database.get_db().execute("SELECT COUNT(*) FROM battery").fetchone()[0])
        self.assertEqual(0, Database.get_db().execute("SELECT COUNT(*) FROM battery_curve").fetchone()[0])
        self.assertEqual(stop_at + timedelta(hours=1), Database.get_last_charge(car.vin).start_at)
        self.assertEqual([], Database.get_all_charge_without_price(Database.get_db()))
        # even when handed to the totals by mistake they count for nothing, not for a charge of 0 kWh
        self.assertEqual(before_rows, insights_data.charge_rows(Charging.get_chargings() + estimated))
        self.assertEqual(2, before_totals["count"])
        self.assertEqual(before_totals, insights_data.charge_totals(
            insights_data.charge_rows(Charging.get_chargings() + estimated)))

    def test_trips_api_answer_is_unchanged(self):
        two_trips()
        trips = Trips.get_trips(CARS)
        self.assertEqual(78, trips[car.vin][0].end_level)
        info = trips[car.vin][0].get_info()
        self.assertNotIn("start_level", info)
        self.assertNotIn("end_level", info)

    def api_get(self, path):
        api = get_views()[0]
        with patch.object(api, "APP", MagicMock(myp=MagicMock(vehicles_list=CARS))):
            return api.app.test_client().get(path).get_json()

    def test_api(self):
        _, stop_at = two_trips()
        add_real_charge(stop_at + timedelta(hours=1), stop_at + timedelta(hours=2))
        chargings = self.api_get("/vehicles/chargings")
        self.assertEqual(1, len(chargings))
        self.assertEqual(4.29, chargings[0]["price"])
        self.assertNotIn("estimated", chargings[0])

        estimated = self.api_get("/vehicles/chargings/estimated")
        self.assertEqual(1, len(estimated))
        self.assertTrue(estimated[0]["estimated"])
        for key in ("kw", "price", "co2", "charging_mode"):
            self.assertIsNone(estimated[0][key])
        self.assertNotIn("duration_min", estimated[0])
        self.assertEqual(78, estimated[0]["start_level"])
        self.assertEqual(93, estimated[0]["end_level"])

    def test_api_without_data(self):
        self.assertEqual([], self.api_get("/vehicles/chargings/estimated"))

    def recent_text(self):
        charge_view = get_views()[1]
        with patch.object(charge_view, "get_car", return_value=car), \
                patch.object(charge_view, "app", side_effect=AttributeError):
            return str(charge_view._recent(car.vin))  # pylint: disable=protected-access

    def test_charge_page_marks_estimates_without_inventing_values(self):
        _, stop_at = two_trips()
        text = self.recent_text()
        self.assertIn("Estimated", text)
        self.assertIn("78 → 93 %", text)
        self.assertNotIn("kWh", text)
        self.assertNotIn("in progress", text)
        self.assertNotIn(" min", text)
        add_real_charge(stop_at + timedelta(hours=1), stop_at + timedelta(hours=2))
        text = self.recent_text()
        self.assertIn("Estimated", text)
        self.assertIn("20.7 kWh", text)

    def test_charge_page_survives_a_failing_estimate(self):
        add_real_charge(START, START + STEP)
        with patch.object(Charging, "get_estimated_chargings", side_effect=ValueError("Unknown car type")):
            self.assertNotIn("Estimated", self.recent_text())


class ExistingDatabaseTest(unittest.TestCase):
    """The estimates need no schema change: a database of an older version opens as it did and gets them."""

    def setUp(self):
        Database.close_db()
        self.addCleanup(get_new_test_db)
        tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(tmp.cleanup)
        self.db_file = os.path.join(tmp.name, "info.db")
        # the migrations of init_db write a backup in the working directory, and fetch altitudes on the internet
        for name in ("backup", "add_altitude_to_db"):
            patcher = patch.object(Database, name)
            patcher.start()
            self.addCleanup(patcher.stop)

    def create_old_database(self):
        conn = sqlite3.connect(self.db_file)
        conn.execute("""CREATE TABLE position (Timestamp DATETIME PRIMARY KEY, VIN TEXT, longitude REAL, latitude REAL,
                        mileage REAL, level INTEGER, moving BOOLEAN, temperature INTEGER);""")
        # no CHECK(stop_at > start_at) yet
        conn.execute("""CREATE TABLE battery (start_at DATETIME PRIMARY KEY, stop_at DATETIME, VIN TEXT,
                        start_level INTEGER, end_level INTEGER, co2 INTEGER, kw INTEGER, price INTEGER,
                        charging_mode TEXT, mileage REAL);""")
        conn.execute("""CREATE TABLE battery_curve (start_at DATETIME, VIN TEXT, date DATETIME, level INTEGER,
                        UNIQUE(start_at, VIN, level));""")
        for trip_start, first_km, first_level in ((START, 100, 80), (START + timedelta(hours=8), 120, 90)):
            for step in range(3):
                date = (trip_start + step * STEP).strftime("%Y-%m-%d %H:%M:%S+00:00")
                conn.execute("INSERT INTO position VALUES (?,?,?,?,?,?,?,?)",
                             (date, car.vin, longitude, latitude, first_km + step * 10, first_level - step, True, 10))
        conn.execute("INSERT INTO battery(start_at,stop_at,VIN,start_level,end_level,kw,price) VALUES(?,?,?,?,?,?,?)",
                     ("2025-12-01 10:00:00+00:00", "2025-12-01 11:00:00+00:00", car.vin, 40, 80, 20.7, 4.29))
        conn.commit()
        conn.close()

    def test_old_database(self):
        self.create_old_database()
        Database.get_db(self.db_file, force_new_conn=True)
        real = Charging.get_chargings()
        self.assertEqual(1, len(real))
        self.assertEqual(20.7, real[0]["kw"])
        estimated = Charging.get_estimated_chargings(CARS)
        self.assertEqual([(78, 90)], [(c["start_level"], c["end_level"]) for c in estimated])
        self.assertEqual(1, Database.get_db().execute("SELECT COUNT(*) FROM battery").fetchone()[0])

    def test_schema_is_not_touched(self):
        Database.get_db(self.db_file, force_new_conn=True)
        schema = [tuple(row) for row in Database.get_db().execute("SELECT name, sql FROM sqlite_master ORDER BY name")]
        Charging.get_estimated_chargings(CARS)
        self.assertEqual(schema, [tuple(row) for row in
                                  Database.get_db().execute("SELECT name, sql FROM sqlite_master ORDER BY name")])
        self.assertFalse([row for row in schema if "estimated" in (row[1] or "")])


if __name__ == '__main__':
    unittest.main()
