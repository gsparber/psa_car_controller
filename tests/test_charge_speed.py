"""Charging power from the charging rate: learned from the levels of the last charges, not from the range shown."""
import math
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from psa_car_controller.psacc.application import charge_speed
from psa_car_controller.psacc.application.charging import Charging
from psa_car_controller.psacc.application.ecomix import Ecomix
from psa_car_controller.psacc.model.battery_curve import BatteryCurveDto
from psa_car_controller.psacc.repository.db import Database
from psa_car_controller.web.view import status_text
from tests.utils import car, get_new_test_db, latitude, longitude

START = datetime(2026, 10, 21, 8, 23, 20, tzinfo=timezone.utc)
MOKKA_KWH = 46


def mokka_charge_of_21_oct(start=START):
    """The first hour of a charge of a Mokka-e at an 11 kW station (21 Oct 2026): (minutes, level, rate in km/h,
    range in km). The station billed 29.75 kWh for 37 -> 100 %."""
    points = [(0, 37, 64, 82), (14.8, 43, 64, 98), (28.7, 49, 64, 114), (42.7, 54, 56, 126), (56.9, 58, 50, 138),
              (70.7, 61, 42, 148)]
    return [BatteryCurveDto(start + timedelta(minutes=minutes), level, rate, autonomy)
            for minutes, level, rate, autonomy in points]


class TestLearn(unittest.TestCase):

    def test_power_of_the_mokka_is_11_kw_at_64_km_h(self):
        value, source = charge_speed.learn([mokka_charge_of_21_oct()], MOKKA_KWH)
        # 24 % = 11.04 kWh over 64 km/h * 42.7 min + 56 * 14.2 + 50 * 13.8 = 70.3 km of rate
        self.assertAlmostEqual(0.157, value, places=3)
        self.assertEqual("learned from 24 % in 1 charges", source)
        self.assertLess(charge_speed.power_kw(64, MOKKA_KWH, 22, 44, value), 11)

    def test_from_the_range_it_was_more_than_the_car_can_take(self):
        # 3 Nov 2026: 64 km/h at 22 % with 44 km
        self.assertAlmostEqual(14.7, charge_speed.power_kw(64, MOKKA_KWH, 22, 44), places=1)

    def test_a_report_without_a_new_level_counts_its_time(self):
        start = datetime(2026, 11, 1, tzinfo=timezone.utc)
        # 12 % in an hour at 60 km/h, the level reported twice after a wakeup
        curve = [BatteryCurveDto(start + timedelta(minutes=minutes), level, 60, 200)
                 for minutes, level in ((0, 40), (15, 43), (16, 43), (30, 46), (45, 49), (60, 52))]
        value, _ = charge_speed.learn([curve], 50)
        self.assertAlmostEqual(0.12 * 50 / 60, value)

    def test_not_learned_from_too_little(self):
        value, source = charge_speed.learn([mokka_charge_of_21_oct()[:2]], MOKKA_KWH)
        self.assertIsNone(value)
        self.assertEqual("not learned, 6 % recorded", source)

    def test_gaps_and_missing_rates_dont_count(self):
        start = datetime(2026, 11, 1, tzinfo=timezone.utc)
        curve = [BatteryCurveDto(start, 20, 40, 100), BatteryCurveDto(start + timedelta(minutes=90), 50, None, 250),
                 BatteryCurveDto(start + timedelta(minutes=100), 55, 0, 270),
                 BatteryCurveDto(start + timedelta(minutes=110), 60, 0, 290)]
        self.assertEqual((None, "not learned, 0 % recorded"), charge_speed.learn([curve], MOKKA_KWH))

    def test_no_car_takes_that_much(self):
        start = datetime(2026, 11, 1, tzinfo=timezone.utc)
        # 20 % in 20 minutes where the rate was 6 km/h
        curve = [BatteryCurveDto(start + timedelta(minutes=minutes), 20 + minutes, 6, 100) for minutes in range(21)]
        value, source = charge_speed.learn([curve], MOKKA_KWH)
        self.assertIsNone(value)
        self.assertIn("is no car", source)

    def test_without_a_battery_size(self):
        self.assertIsNone(charge_speed.learn([mokka_charge_of_21_oct()], 0)[0])


class TestPower(unittest.TestCase):

    def test_learned_value_wins(self):
        self.assertAlmostEqual(64 * 0.16, status_text.charge_power_kw(64, MOKKA_KWH, 22, 44, 0.16))

    def test_from_the_range_without_one(self):
        self.assertAlmostEqual(7.0, status_text.charge_power_kw(35, 50, 50, 125, None))

    def test_unknown_values(self):
        for rate in (None, 0, math.nan, math.inf, True):
            self.assertIsNone(charge_speed.power_kw(rate, 50, 50, 125, 0.16))
        self.assertIsNone(charge_speed.power_kw(64, 50, None, 125))
        self.assertIsNone(charge_speed.power_kw(64, 50, 50, 0, math.nan))


class TestFromTheDatabase(unittest.TestCase):

    def setUp(self):
        get_new_test_db()
        self.addCleanup(get_new_test_db)

    def store(self, curve):
        conn = Database.get_db()
        conn.execute("INSERT INTO battery(start_at, VIN, start_level) VALUES(?,?,?)", (curve[0].date, car.vin, 37))
        conn.executemany("INSERT INTO battery_curve(start_at, VIN, date, level, rate, autonomy) VALUES(?,?,?,?,?,?)",
                         [(curve[0].date, car.vin, p.date, p.level, p.rate, p.autonomy) for p in curve])
        conn.commit()

    def test_learned_and_kept(self):
        self.assertIsNone(charge_speed.kwh_per_rate_km(car.vin, MOKKA_KWH))
        self.store(mokka_charge_of_21_oct())
        # still the value read before, for CACHE_SECONDS
        self.assertIsNone(charge_speed.kwh_per_rate_km(car.vin, MOKKA_KWH))
        charge_speed.clear_cache()
        self.assertAlmostEqual(0.157, charge_speed.kwh_per_rate_km(car.vin, MOKKA_KWH), places=3)

    def test_without_a_database(self):
        with patch.object(Database, "get_last_battery_curves", side_effect=OSError("no database")):
            self.assertIsNone(charge_speed.kwh_per_rate_km(car.vin, MOKKA_KWH))


class TestStaleEndLevel(unittest.TestCase):
    """A Mokka-e on 14 Oct 2026: 18 % at the start, 32 % at 11:42 UTC, then no level any more; the status ending the
    charge at 13:32 still said 32 %, where the station billed 19.45 kWh."""
    START = datetime(2026, 10, 14, 11, 10, 48, tzinfo=timezone.utc)

    def setUp(self):
        get_new_test_db()
        self.addCleanup(get_new_test_db)
        for name in ("get_data_from_co2_signal", "get_co2_per_kw"):
            patcher = patch.object(Ecomix, name, return_value=50.0)
            patcher.start()
            self.addCleanup(patcher.stop)

    def status(self, name, minutes, level, rate, autonomy):
        Charging.record_charging(car, name, self.START + timedelta(minutes=minutes), level, latitude, longitude, "AT",
                                 "Slow", rate, autonomy, 47776.5)

    def charge(self):
        return Charging.get_chargings()[-1]

    def test_end_level_from_the_rate_of_the_last_report(self):
        # 8 % recorded, too little to learn from: the power comes from the range, 56 km/h * 46 kWh * 26 % / 70 km
        self.status("InProgress", 0, 18, 64, 40)
        self.status("InProgress", 32, 26, 56, 70)
        self.status("Disconnected", 142, 26, 0, 70)
        kw = 56 * car.battery_power * 26 / 100 / 70
        self.assertEqual(round(26 + kw * 110 / 60 / car.battery_power * 100), self.charge()["end_level"])

    def test_learned_value_is_used(self):
        older = mokka_charge_of_21_oct(self.START - timedelta(days=7))
        conn = Database.get_db()
        conn.execute("INSERT INTO battery(start_at, stop_at, VIN, start_level, end_level) VALUES(?,?,?,?,?)",
                     (older[0].date, older[-1].date, car.vin, 37, 61))
        conn.executemany("INSERT INTO battery_curve(start_at, VIN, date, level, rate, autonomy) VALUES(?,?,?,?,?,?)",
                         [(older[0].date, car.vin, p.date, p.level, p.rate, p.autonomy) for p in older])
        conn.commit()
        self.status("InProgress", 0, 18, 64, 40)
        self.status("InProgress", 32, 32, 56, 70)
        self.status("Disconnected", 142, 32, 0, 70)
        kwh_per_km = charge_speed.kwh_per_rate_km(car.vin, car.battery_power)
        self.assertIsNotNone(kwh_per_km)
        expected = round(32 + 56 * kwh_per_km * 110 / 60 / car.battery_power * 100)
        self.assertEqual(expected, self.charge()["end_level"])
        self.assertAlmostEqual((expected - 18) / 100 * car.battery_power, self.charge()["kw"])

    def test_a_recent_level_stays(self):
        self.status("InProgress", 0, 18, 64, 40)
        self.status("InProgress", 32, 32, 56, 70)
        self.status("Disconnected", 50, 32, 0, 70)
        self.assertEqual(32, self.charge()["end_level"])

    def test_a_new_level_stays(self):
        self.status("InProgress", 0, 18, 64, 40)
        self.status("InProgress", 32, 32, 56, 70)
        self.status("Disconnected", 142, 57, 0, 125)
        self.assertEqual(57, self.charge()["end_level"])


if __name__ == '__main__':
    unittest.main()
