"""A charge nothing saw end: the next status can come days later, and a car that drove isn't charging any more."""
import re
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from psa_car_controller.psacc.application.charging import Charging
from psa_car_controller.psacc.application.ecomix import Ecomix
from psa_car_controller.psacc.repository.db import Database
from psa_car_controller.web.view import insights_data
from tests.test_unknown_values import CLIENTSIDE
from tests.utils import car, get_new_test_db, latitude, longitude

START = datetime(2026, 1, 5, 8, 0, 0, tzinfo=timezone.utc)
HOUR = timedelta(hours=1)
KM = 2000


def status(name, delay, level, mileage=KM):
    Charging.record_charging(car, name, START + delay, level, latitude, longitude, "FR", "slow", 20, 60, mileage)


def rows():
    return Charging.get_chargings()


def curve_points():
    return Database.get_db().execute("SELECT start_at, level FROM battery_curve ORDER BY date").fetchall()


class LateStatusTest(unittest.TestCase):
    """A status that arrives after the one closing its charge isn't another charge."""

    def setUp(self):
        get_new_test_db()
        self.addCleanup(get_new_test_db)
        for name in ("get_data_from_co2_signal", "get_co2_per_kw"):
            patcher = patch.object(Ecomix, name, return_value=50.0)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_older_status_is_no_new_charge(self):
        status("InProgress", 0 * HOUR, 50)
        status("InProgress", HOUR / 4, 53)
        status("Stopped", 2 * HOUR, 72)
        status("InProgress", HOUR / 4, 53)
        status("Stopped", 2.1 * HOUR, 72)
        charges = rows()
        self.assertEqual([(START, START + 2 * HOUR, 50, 72)],
                         [(c["start_at"], c["stop_at"], c["start_level"], c["end_level"]) for c in charges])

    def test_next_charge_still_starts(self):
        status("InProgress", 0 * HOUR, 50)
        status("Stopped", 2 * HOUR, 72)
        status("InProgress", 2 * HOUR, 72)
        status("Stopped", 4 * HOUR, 90)
        self.assertEqual([START, START + 2 * HOUR], [c["start_at"] for c in rows()])

    def test_stop_without_timezone(self):
        status("InProgress", 0 * HOUR, 50)
        status("Stopped", 2 * HOUR, 72)
        conn = Database.get_db()
        conn.execute("UPDATE battery SET stop_at=?", ((START + 2 * HOUR).replace(tzinfo=None),))
        conn.commit()
        status("InProgress", HOUR, 60)
        self.assertEqual(1, len(rows()))


class StaleChargeTest(unittest.TestCase):
    def setUp(self):
        get_new_test_db()
        self.addCleanup(get_new_test_db)
        for name in ("get_data_from_co2_signal", "get_co2_per_kw"):
            patcher = patch.object(Ecomix, name, return_value=50.0)
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)

    def assert_charge(self, row, duration, start_level, end_level):
        self.assertEqual(duration, row["stop_at"] - row["start_at"])
        self.assertEqual((start_level, end_level), (row["start_level"], row["end_level"]))
        self.assertAlmostEqual((end_level - start_level) / 100 * car.battery_power, row["kw"])

    def test_status_after_a_drive_doesnt_end_the_charge(self):
        # 216 h, and the level of the next status had a drive of 359 km in it
        status("InProgress", 0 * HOUR, 50)
        status("InProgress", 2 * HOUR, 70)
        status("Stopped", 216 * HOUR, 69, KM + 359)
        self.assertEqual(1, len(rows()))
        self.assert_charge(rows()[0], 2 * HOUR, 50, 70)
        self.assertEqual(START + 2 * HOUR, self.get_co2_per_kw.call_args.args[1])

    def test_nothing_seen_but_the_start_leaves_no_charge(self):
        # same as a charge of 1 %: it says nothing, and the levels of the trips can still reveal it (T45)
        status("InProgress", 0 * HOUR, 50)
        status("Stopped", 216 * HOUR, 69, KM + 359)
        self.assertEqual([], rows())
        status("InProgress", 217 * HOUR, 30, KM + 359)  # and doesn't stay open either
        self.assertEqual([(START + 217 * HOUR, None)], [(row["start_at"], row["stop_at"]) for row in rows()])

    def test_charge_after_a_drive_is_another_charge(self):
        # all of them went in one charge, as no status in between said otherwise
        status("InProgress", 0 * HOUR, 50)
        status("InProgress", 2 * HOUR, 70)
        status("InProgress", 216 * HOUR, 30, KM + 359)
        status("InProgress", 218 * HOUR, 50, KM + 359)
        status("Stopped", 219 * HOUR, 55, KM + 359)
        self.assertEqual(2, len(rows()))
        self.assert_charge(rows()[0], 2 * HOUR, 50, 70)
        self.assert_charge(rows()[1], 3 * HOUR, 30, 55)
        self.assertEqual([START, START, START + 216 * HOUR, START + 216 * HOUR],
                         [point["start_at"] for point in curve_points()])

    def test_long_ac_charge_stays(self):
        # 41 kWh with the least a charging station offers
        for hour in range(33):
            status("InProgress", hour * HOUR, 10 + hour * 2)
        status("Stopped", 33 * HOUR, 100)
        self.assert_charge(rows()[0], 33 * HOUR, 10, 100)
        self.assertAlmostEqual(0.9 * car.battery_power / 33, insights_data.charge_totals(
            insights_data.charge_rows(rows()))["avg_power"])

    def test_no_status_for_days_without_a_drive_keeps_the_energy(self):
        # the car charged, only when it ended isn't known
        status("InProgress", 0 * HOUR, 50)
        status("InProgress", 72 * HOUR, 70)
        self.assertEqual(1, len(rows()))
        status("Stopped", 216 * HOUR, 90)
        self.assert_charge(rows()[0], 216 * HOUR, 50, 90)

    def test_moved_on_a_parking_lot_is_no_drive(self):
        status("InProgress", 0 * HOUR, 50)
        status("Stopped", 2 * HOUR, 70, KM + 0.9)
        self.assert_charge(rows()[0], 2 * HOUR, 50, 70)

    def test_unknown_mileage_is_no_drive(self):
        for start_mileage, stop_mileage in ((None, KM + 359), (KM, None)):
            with self.subTest(start_mileage=start_mileage, stop_mileage=stop_mileage):
                status("InProgress", 0 * HOUR, 50, start_mileage)
                status("Stopped", 2 * HOUR, 70, stop_mileage)
                self.assert_charge(rows()[0], 2 * HOUR, 50, 70)
                conn = Database.get_db()
                conn.execute("DELETE FROM battery")
                conn.execute("DELETE FROM battery_curve")
                conn.commit()


class AveragePowerTest(unittest.TestCase):
    NORMAL = {"start_at": START, "stop_at": START + 2 * HOUR, "kw": 11.5}

    def avg_power(self, *chargings):
        return insights_data.charge_totals(insights_data.charge_rows(list(chargings)))["avg_power"]

    def test_stored_charge_of_days_is_left_out(self):
        self.assertAlmostEqual(5.75, self.avg_power(self.NORMAL, {**self.NORMAL, "stop_at": START + 216 * HOUR,
                                                                  "kw": 8.74}))

    def test_slow_charge_stays(self):
        self.assertAlmostEqual(1.25, self.avg_power({**self.NORMAL, "stop_at": START + 33 * HOUR, "kw": 41.25}))

    def test_energy_of_a_left_out_charge_still_counts(self):
        charge = {**self.NORMAL, "stop_at": START + 216 * HOUR, "kw": 8.74}
        totals = insights_data.charge_totals(insights_data.charge_rows([self.NORMAL, charge]))
        self.assertAlmostEqual(20.24, totals["energy"])

    def test_only_implausible_is_unknown(self):
        self.assertIsNone(self.avg_power({**self.NORMAL, "stop_at": START + 216 * HOUR, "kw": 8.74}))

    def test_same_limit_as_the_card(self):
        limit = re.search(r"const MIN_CHARGE_KW = ([\d.]+)", CLIENTSIDE.read_text(encoding="utf-8"))
        self.assertEqual(insights_data.MIN_CHARGE_KW, float(limit.group(1)))
