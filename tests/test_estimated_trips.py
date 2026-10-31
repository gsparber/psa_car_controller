"""A drive in a gap (T85): its km count, what the positions can't tell stays unknown and out of the averages."""
import json
import random
import unittest
from datetime import datetime, timedelta, timezone

from psa_car_controller.psacc.application.charging import Charging
from psa_car_controller.psacc.model.car import Car, Cars
from psa_car_controller.psacc.repository.db import Database
from psa_car_controller.psacc.repository.trips import Trips
from psa_car_controller.web import figures, units
from psa_car_controller.web.view import insights_data
from tests.utils import car, get_new_test_db, latitude, longitude, vehicule_list

START = datetime(2026, 1, 5, 8, 0, 0, tzinfo=timezone.utc)
MIN = timedelta(minutes=1)
HOUR = timedelta(hours=1)
CARS = Cars([car])
HYBRID = vehicule_list[1]
FIRST_DRIVE = ((0 * MIN, 1000, 80), (10 * MIN, 1015, 76), (20 * MIN, 1030, 72))
GAP_END = 12 * HOUR + 20 * MIN
PARKED = 20 * HOUR


def record(*points, vehicle=car):
    for point in points:
        delay, mileage, level = point[:3]
        Database.record_position(None, vehicle.vin, mileage, latitude, longitude, 32, START + delay, level,
                                 point[3] if len(point) > 3 else None, False, 10)


class EstimatedTripTestCase(unittest.TestCase):
    def setUp(self):
        get_new_test_db()
        self.addCleanup(get_new_test_db)

    def trips(self, *points, vehicle=car):
        record(*points, vehicle=vehicle)
        return Trips.get_trips(Cars([vehicle])).get(vehicle.vin, [])

    def summary(self, *points):
        return [(trip.distance, trip.estimated) for trip in self.trips(*points)]


class WhichTripsTest(EstimatedTripTestCase):
    def test_gap_after_a_trip(self):
        self.assertEqual([(30, False), (30, True)],
                         self.summary(*FIRST_DRIVE, (GAP_END, 1060, 60), (GAP_END + PARKED, 1060, 60)))

    def test_gap_at_the_very_start(self):
        self.assertEqual([(30, True)], self.summary((0 * HOUR, 1000, 80), (12 * HOUR, 1030, 70), (24 * HOUR, 1030, 70)))

    def test_gap_with_only_two_positions(self):
        self.assertEqual([(30, True)], self.summary((0 * HOUR, 1000, 80), (12 * HOUR, 1030, 70)))

    def test_gap_as_the_last_pair_after_a_restart(self):
        self.assertEqual([(30, True)], self.summary((0 * HOUR, 1000, 80), (12 * HOUR, 1000, 80), (24 * HOUR, 1030, 70)))

    def test_gap_at_the_start_isnt_merged_with_the_next_drive(self):
        # was 50 km in 12 h 20 min, with the time parked in it
        trips = self.trips((0 * HOUR, 1000, 80), (12 * HOUR, 1030, 70), (12 * HOUR + 10 * MIN, 1040, 66),
                           (12 * HOUR + 20 * MIN, 1050, 62), (30 * HOUR, 1050, 62))
        self.assertEqual([(30, True), (20, False)], [(trip.distance, trip.estimated) for trip in trips])
        self.assertAlmostEqual(20 / 60, trips[1].duration)

    def test_gap_after_a_restart_isnt_merged_with_the_next_drive(self):
        self.assertEqual([(30, True), (20, False)],
                         self.summary((0 * HOUR, 1000, 80), (12 * HOUR, 1000, 80), (24 * HOUR, 1030, 70),
                                      (24 * HOUR + 10 * MIN, 1040, 66), (24 * HOUR + 20 * MIN, 1050, 62),
                                      (40 * HOUR, 1050, 62)))

    def test_slow_gap_over_the_limit_is_a_drive(self):
        # 5 km in 30 h: 0.17 km/h. The mileage doesn't go up on its own
        self.assertEqual([(30, False), (5, True)],
                         self.summary(*FIRST_DRIVE, (30 * HOUR, 1035, 71), (50 * HOUR, 1035, 71)))

    def test_slow_gap_within_the_limit_stays_a_stop(self):
        self.assertEqual([(30, False)], self.summary(*FIRST_DRIVE, (30 * HOUR, 1031, 72), (50 * HOUR, 1031, 72)))

    def test_recorded_drive_isnt_estimated(self):
        self.assertEqual([(60, False)], self.summary(*FIRST_DRIVE, (2 * HOUR, 1060, 60), (20 * HOUR, 1060, 60)))
        get_new_test_db()
        self.assertEqual([(32, False)], self.summary((0 * MIN, 1000, 75), (28 * MIN, 1032, 58)))

    def test_parked_and_charged_in_a_gap_stay_no_trip(self):
        self.assertEqual([], self.summary((0 * HOUR, 1000, 75), (12 * HOUR, 1000, 74), (24 * HOUR, 1000, 74)))
        get_new_test_db()
        self.assertEqual([], self.summary((0 * HOUR, 1000, 30), (12 * HOUR, 1000, 80), (24 * HOUR, 1000, 79)))

    def test_charge_and_km_within_the_gap_limit_is_a_drive(self):
        self.assertEqual([(30, False), (32, True), (18, False)],
                         self.summary(*FIRST_DRIVE, (80 * MIN, 1062, 90), (90 * MIN, 1071, 88), (100 * MIN, 1080, 86)))

    def test_charge_and_km_as_the_last_pair_is_a_drive(self):
        self.assertEqual([(30, False), (32, True)], self.summary(*FIRST_DRIVE, (80 * MIN, 1062, 90)))

    def test_charge_and_km_at_the_very_start_is_a_drive(self):
        self.assertEqual([(32, True), (18, False)],
                         self.summary((0 * MIN, 1000, 50), (60 * MIN, 1032, 90), (70 * MIN, 1041, 88),
                                      (80 * MIN, 1050, 86), (20 * HOUR, 1050, 86)))

    def test_charge_and_a_car_moved_on_a_parking_lot_stay_no_trip(self):
        self.assertEqual([(30, False), (18, False)],
                         self.summary(*FIRST_DRIVE, (80 * MIN, 1031, 90), (90 * MIN, 1040, 88), (100 * MIN, 1049, 86)))

    def test_small_rise_on_the_way_stays_in_the_trip(self):
        self.assertEqual([(62, False)], self.summary(*FIRST_DRIVE, (40 * MIN, 1062, 77)))

    def test_refuel_and_km_within_the_gap_limit_is_a_drive(self):
        trips = self.trips((0 * MIN, 1000, 40, 50), (10 * MIN, 1015, 40, 49), (20 * MIN, 1030, 40, 48),
                           (80 * MIN, 1060, 40, 90), (90 * MIN, 1075, 40, 89), (100 * MIN, 1090, 40, 88),
                           vehicle=HYBRID)
        self.assertEqual([(30, False), (30, True), (30, False)], [(trip.distance, trip.estimated) for trip in trips])

    def test_no_trip_overlaps_and_no_km_counts_twice(self):
        rand = random.Random(85)
        for _ in range(150):
            get_new_test_db()
            delay, mileage, level = 0 * HOUR, 1000, 80
            points = []
            for _ in range(rand.randint(2, 12)):
                points.append((delay, mileage, level))
                delay += rand.choice([5 * MIN, 20 * MIN, 3 * HOUR, 14 * HOUR, 40 * HOUR])
                mileage += rand.choice([0, 0, 1, 5, 30])
                level = min(100, max(5, level + rand.choice([-8, -3, 0, 2, 25])))
            record(*points)
            trips = Trips.get_trips(CARS).get(car.vin, [])
            self.assertLessEqual(sum(trip.distance for trip in trips), mileage - 1000, points)
            for before, after in zip(trips, trips[1:]):
                self.assertLessEqual(before.end_at, after.start_at, points)
                self.assertLessEqual(before.mileage, after.mileage - after.distance + 1e-6, points)


class ValuesTest(EstimatedTripTestCase):
    def gap_info(self, level=60):
        trips = self.trips(*FIRST_DRIVE, (GAP_END, 1060, level), (GAP_END + PARKED, 1060, level))
        return trips[1].get_info()

    def test_duration_and_speed_are_unknown(self):
        info = self.gap_info()
        self.assertIsNone(info["duration"])
        self.assertIsNone(info["speed_average"])
        self.assertIs(True, info["estimated"])

    def test_distance_mileage_frame_and_consumption_stay(self):
        info = self.gap_info()
        self.assertEqual(30, info["distance"])
        self.assertEqual(1060, info["mileage"])
        self.assertEqual(START + 20 * MIN, info["start_at"])
        self.assertEqual(START + GAP_END, info["end_at"])
        self.assertAlmostEqual(12 * car.battery_power / 100, info["consumption"])
        self.assertAlmostEqual(100 * 12 * car.battery_power / 100 / 30, info["consumption_km"])

    def test_recorded_trip_keeps_its_values(self):
        info = self.trips(*FIRST_DRIVE, (GAP_END, 1060, 60))[0].get_info()
        self.assertEqual(20, info["duration"])
        self.assertEqual(90, info["speed_average"])
        self.assertIs(False, info["estimated"])

    def test_level_that_rose_or_stayed_tells_no_consumption(self):
        for level in (75, 72, 85):
            with self.subTest(level=level):
                get_new_test_db()
                info = self.gap_info(level)
                self.assertEqual(30, info["distance"])
                self.assertIsNone(info["consumption"])
                self.assertIsNone(info["consumption_km"])

    def test_small_rise_on_a_recorded_trip_still_counts_0(self):
        info = self.trips((0 * MIN, 1000, 70), (10 * MIN, 1010, 71), (20 * MIN, 1020, 72))[0].get_info()
        self.assertEqual(0, info["consumption"])

    def test_refuelled_hybrid(self):
        trips = self.trips((0 * MIN, 1000, 40, 50), (10 * MIN, 1015, 40, 49), (20 * MIN, 1030, 40, 48),
                           (GAP_END, 1060, 40, 90), (GAP_END + PARKED, 1060, 40, 90), vehicle=HYBRID)
        self.assertEqual([(30, False), (30, True)], [(trip.distance, trip.estimated) for trip in trips])
        info = trips[1].get_info()
        self.assertIsNone(info["consumption_fuel_km"])
        self.assertIsNone(info["consumption_km"])
        self.assertEqual(0, trips[1].consumption_fuel)

    def test_hybrid_on_fuel(self):
        trips = self.trips((0 * MIN, 1000, 40, 50), (10 * MIN, 1015, 40, 49), (20 * MIN, 1030, 40, 48),
                           (GAP_END, 1060, 40, 45), (GAP_END + PARKED, 1060, 40, 45), vehicle=HYBRID)
        self.assertEqual(round(100 * 3 * HYBRID.fuel_capacity / 100 / 30, 2), trips[1].get_info()["consumption_fuel_km"])


class ChargeInTheGapTest(EstimatedTripTestCase):
    POINTS = (*FIRST_DRIVE, (GAP_END, 1060, 85), (GAP_END + 20 * MIN, 1080, 75), (GAP_END + PARKED, 1080, 75))

    def test_estimated_once_from_the_trip_itself(self):
        record(*self.POINTS)
        estimated = Charging.get_estimated_chargings(CARS)
        self.assertEqual([(72, 85)], [(charge["start_level"], charge["end_level"]) for charge in estimated])
        self.assertEqual(START + 20 * MIN, estimated[0]["start_at"])
        self.assertEqual(START + GAP_END, estimated[0]["stop_at"])
        self.assertEqual(1030, estimated[0]["mileage"])

    def test_charge_and_km_within_the_gap_limit(self):
        record(*FIRST_DRIVE, (80 * MIN, 1062, 90), (90 * MIN, 1071, 88), (100 * MIN, 1080, 86))
        trips = Trips.get_trips(CARS)[car.vin]
        info = trips[1].get_info()
        self.assertIsNone(info["consumption_km"])
        self.assertIsNone(info["duration"])
        # the km of this trip must not change the average of the others
        recorded = insights_data.trip_totals(insights_data.trip_rows([trips[0], trips[2]]))
        totals = insights_data.trip_totals(insights_data.trip_rows(trips))
        self.assertEqual(recorded["consumption"], totals["consumption"])
        self.assertEqual(80, totals["distance"])
        estimated = Charging.get_estimated_chargings(CARS)
        self.assertEqual([(72, 90)], [(charge["start_level"], charge["end_level"]) for charge in estimated])
        conn = Database.get_db()
        conn.execute("INSERT INTO battery(start_at,stop_at,VIN,start_level,end_level) VALUES(?,?,?,?,?)",
                     (START + 30 * MIN, START + 70 * MIN, car.vin, 72, 90))
        conn.commit()
        self.assertEqual([], Charging.get_estimated_chargings(CARS))

    def test_recorded_charge_isnt_estimated(self):
        record(*self.POINTS)
        conn = Database.get_db()
        conn.execute("INSERT INTO battery(start_at,stop_at,VIN,start_level,end_level) VALUES(?,?,?,?,?)",
                     (START + HOUR, START + 3 * HOUR, car.vin, 72, 85))
        conn.commit()
        self.assertEqual(3, len(Trips.get_trips(CARS)[car.vin]))
        self.assertEqual([], Charging.get_estimated_chargings(CARS))

    def test_small_rise_is_no_charge(self):
        record(*FIRST_DRIVE, (GAP_END, 1060, 75), (GAP_END + PARKED, 1060, 75))
        self.assertEqual([], Charging.get_estimated_chargings(CARS))


class RecordedChargeInTheGapTest(EstimatedTripTestCase):
    """The level alone looks like a drive: only the battery table tells the car charged on the way."""
    FRAME_START = START + 20 * MIN
    FRAME_END = START + GAP_END
    POINTS = (*FIRST_DRIVE, (GAP_END, 1389, 7), (GAP_END + PARKED, 1389, 7))

    def add_charge(self, start_at, stop_at, vin=car.vin):
        conn = Database.get_db()
        conn.execute("INSERT INTO battery(start_at,stop_at,VIN,start_level,end_level) VALUES(?,?,?,?,?)",
                     (start_at, stop_at, vin, 40, 79))
        conn.commit()

    def gap_trip(self, *charge):
        self.add_charge(*charge)
        trips = Trips.get_trips(CARS)[car.vin]
        self.assertEqual([(30, False), (359, True)], [(trip.distance, trip.estimated) for trip in trips])
        Database.get_db().execute("DELETE FROM battery")
        return trips[1]

    def test_charge_on_the_way_makes_the_consumption_unknown(self):
        record(*self.POINTS)
        for charge in ((self.FRAME_START + HOUR, self.FRAME_START + 3 * HOUR),  # inside
                       (self.FRAME_START - HOUR, self.FRAME_START + HOUR),  # ends in the gap
                       (self.FRAME_END - HOUR, self.FRAME_END + HOUR),  # starts in the gap
                       (self.FRAME_START - HOUR, self.FRAME_END + HOUR),  # around the gap
                       (self.FRAME_START - HOUR, self.FRAME_START),  # ends where the gap starts
                       (self.FRAME_END, self.FRAME_END + HOUR),  # starts where the gap ends
                       (self.FRAME_START + HOUR, None),  # not ended
                       (self.FRAME_END, None)):
            with self.subTest(charge=charge):
                info = self.gap_trip(*charge).get_info()
                self.assertEqual((None, None), (info["consumption"], info["consumption_km"]))
                self.assertEqual((359, 1389), (info["distance"], info["mileage"]))

    def test_recorded_without_timezone(self):
        record(*self.POINTS)
        naive = (self.FRAME_START + HOUR).replace(tzinfo=None)
        self.assertFalse(self.gap_trip(naive, naive + HOUR).consumption_known)

    def test_other_charges_leave_the_consumption(self):
        record(*self.POINTS)
        for charge in ((self.FRAME_START - 2 * HOUR, self.FRAME_START - HOUR),  # before
                       (self.FRAME_END + HOUR, self.FRAME_END + 2 * HOUR),  # after
                       (self.FRAME_START - HOUR, None)):  # an old one nothing ended
            with self.subTest(charge=charge):
                expected = 65 * car.battery_power / 359
                self.assertAlmostEqual(expected, self.gap_trip(*charge).get_info()["consumption_km"])

    def test_charge_of_another_car_leaves_the_consumption(self):
        record(*self.POINTS)
        self.assertTrue(self.gap_trip(self.FRAME_START + HOUR, self.FRAME_START + 2 * HOUR, "another").consumption_known)

    def test_recorded_trips_keep_their_consumption(self):
        record(*self.POINTS)
        self.add_charge(START - HOUR, self.FRAME_END + HOUR)  # a stored charge over days holds trips too
        trip = Trips.get_trips(CARS)[car.vin][0]
        self.assertAlmostEqual(8 * car.battery_power / 30, trip.get_info()["consumption_km"])

    def test_too_high_a_consumption_doesnt_lose_the_km(self):
        # 40 % over 2 km would be filtered, with a charge on the way it tells nothing and the km count
        record(*FIRST_DRIVE, (GAP_END, 1032, 32), (GAP_END + PARKED, 1032, 32))
        self.assertEqual([30], [trip.distance for trip in Trips.get_trips(CARS)[car.vin]])
        self.add_charge(self.FRAME_START + HOUR, self.FRAME_START + 2 * HOUR)
        self.assertEqual([30, 2], [round(trip.distance) for trip in Trips.get_trips(CARS)[car.vin]])

    def test_average_of_the_insights(self):
        record(*self.POINTS)
        self.add_charge(self.FRAME_START + HOUR, self.FRAME_START + 3 * HOUR)
        totals = insights_data.trip_totals(insights_data.trip_rows(Trips.get_trips(CARS)[car.vin]))
        self.assertEqual(389, totals["distance"])
        self.assertAlmostEqual(100 * (8 * car.battery_power / 100) / 30, totals["consumption"])

    def test_isnt_estimated_as_well(self):
        record(*self.POINTS)
        self.add_charge(self.FRAME_START + HOUR, self.FRAME_START + 3 * HOUR)
        self.assertEqual([], Charging.get_estimated_chargings(CARS))


class TooLowConsumptionTest(EstimatedTripTestCase):
    """The level fell by 1 % over 359 km: however it got there, that's no measured consumption."""
    FAR = (*FIRST_DRIVE, (GAP_END, 1389, 71), (GAP_END + PARKED, 1389, 71))

    def gap_trip(self, km, drop, vehicle=car, altitude=32, fuel=None):
        level = 80 - drop
        for delay, mileage, value, height in ((0 * HOUR, 1000, 80, 32), (12 * HOUR, 1000 + km, level, altitude)):
            Database.record_position(None, vehicle.vin, mileage, latitude, longitude, height, START + delay, value,
                                     fuel and fuel[delay != 0 * HOUR], False, 10)
        trips = Trips.get_trips(Cars([vehicle]))[vehicle.vin]
        self.assertEqual([(km, True)], [(trip.distance, trip.estimated) for trip in trips])
        return trips[0].get_info()

    def test_unknown_without_any_charge_recorded(self):
        record(*self.FAR)
        info = Trips.get_trips(CARS)[car.vin][1].get_info()
        self.assertEqual((None, None), (info["consumption"], info["consumption_km"]))
        self.assertEqual((359, 1389), (info["distance"], info["mileage"]))

    def test_average_of_the_insights(self):
        # 100 km at 18.4 kWh/100 km, then the charge ends where the gap starts: was 4.11
        record((0 * MIN, 1000, 90), (30 * MIN, 1050, 70), (60 * MIN, 1100, 50), (60 * MIN + 221 * HOUR, 1459, 49),
               (60 * MIN + 240 * HOUR, 1459, 49))
        conn = Database.get_db()
        conn.execute("INSERT INTO battery(start_at,stop_at,VIN,start_level,end_level) VALUES(?,?,?,?,?)",
                     (START - HOUR, START + 60 * MIN, car.vin, 40, 50))
        conn.commit()
        totals = insights_data.trip_totals(insights_data.trip_rows(Trips.get_trips(CARS)[car.vin]))
        self.assertEqual(459, totals["distance"])
        self.assertAlmostEqual(40 * car.battery_power / 100, totals["consumption"])

    def test_frugal_city_drives_stay(self):
        # (km, drop): 7 to 15 kWh/100 km, the short ones are all rounding
        for km, drop in ((3, 1), (5, 1), (9, 1), (10, 1), (15, 1), (20, 3), (30, 5), (40, 9), (100, 22)):
            with self.subTest(km=km, drop=drop):
                get_new_test_db()
                self.assertAlmostEqual(100 * (drop * car.battery_power / 100) / km,
                                       self.gap_trip(km, drop)["consumption_km"])

    def test_no_rounding_makes_a_possible_consumption_unknown(self):
        # 5 kWh/100 km is well below any real drive, and the levels can show one percent less than it took
        for km in range(2, 120):
            true_drop = 5 * km / car.battery_power
            for drop in {int(true_drop), int(true_drop) + 1} - {0}:
                with self.subTest(km=km, drop=drop):
                    get_new_test_db()
                    self.assertIsNotNone(self.gap_trip(km, drop)["consumption_km"])

    def test_impossible_even_with_the_rounding(self):
        for km, drop in ((21, 1), (60, 1), (359, 1), (400, 10)):
            with self.subTest(km=km, drop=drop):
                get_new_test_db()
                self.assertIsNone(self.gap_trip(km, drop)["consumption_km"])

    def test_long_descent_stays(self):
        self.assertAlmostEqual(100 * (1 * car.battery_power / 100) / 60,
                               self.gap_trip(60, 1, altitude=32 - 600)["consumption_km"])

    def test_small_descent_doesnt_explain_it(self):
        self.assertIsNone(self.gap_trip(60, 1, altitude=32 - 599)["consumption_km"])

    def test_limit_follows_the_model(self):
        ami = Car("VR79AZ2CAR", "ami", "Citroen")
        self.assertEqual(6, ami.max_elec_consumption)
        # 3.6 kWh/100 km would be nothing for a car of 1.6 t
        self.assertAlmostEqual(100 * (20 * ami.battery_power / 100) / 30, self.gap_trip(30, 20, ami)["consumption_km"])
        get_new_test_db()
        self.assertIsNone(self.gap_trip(359, 1, ami)["consumption_km"])

    def test_hybrid_on_fuel_can_use_little_electricity(self):
        info = self.gap_trip(359, 1, HYBRID, fuel=(50, 20))
        self.assertAlmostEqual(100 * (1 * HYBRID.battery_power / 100) / 359, info["consumption_km"])
        self.assertIsNotNone(info["consumption_fuel_km"])

    def test_hybrid_charged_or_refuelled_on_the_way(self):
        record((0 * HOUR, 1000, 40, 50), (12 * HOUR, 1060, 50, 45), (24 * HOUR, 1060, 50, 45), vehicle=HYBRID)
        info = Trips.get_trips(Cars([HYBRID]))[HYBRID.vin][0].get_info()
        self.assertEqual((None, None), (info["consumption_km"], info["consumption_fuel_km"]))
        get_new_test_db()
        record((0 * HOUR, 1000, 60, 50), (12 * HOUR, 1060, 50, 90), (24 * HOUR, 1060, 50, 90), vehicle=HYBRID)
        info = Trips.get_trips(Cars([HYBRID]))[HYBRID.vin][0].get_info()
        self.assertEqual((None, None), (info["consumption_km"], info["consumption_fuel_km"]))

    def test_hybrid_small_rise_keeps_the_fuel(self):
        # the engine charges the battery on the way: not a charge
        record((0 * HOUR, 1000, 40, 50), (12 * HOUR, 1060, 43, 45), (24 * HOUR, 1060, 43, 45), vehicle=HYBRID)
        info = Trips.get_trips(Cars([HYBRID]))[HYBRID.vin][0].get_info()
        self.assertEqual(round(100 * 5 * HYBRID.fuel_capacity / 100 / 60, 2), info["consumption_fuel_km"])

    def test_recorded_trip_isnt_touched(self):
        record((0 * MIN, 1000, 80), (30 * MIN, 1040, 80), (60 * MIN, 1080, 79), (24 * HOUR, 1080, 79))
        info = Trips.get_trips(CARS)[car.vin][0].get_info()
        self.assertIs(False, info["estimated"])
        self.assertAlmostEqual(100 * (1 * car.battery_power / 100) / 80, info["consumption_km"])


class TotalsTest(EstimatedTripTestCase):
    def rows(self, *points):
        return insights_data.trip_rows(self.trips(*points))

    def test_unknown_consumption_doesnt_lower_the_average(self):
        rows = self.rows(*FIRST_DRIVE, (GAP_END, 1060, 72), (GAP_END + PARKED, 1060, 72))
        totals = insights_data.trip_totals(rows)
        self.assertEqual(60, totals["distance"])
        self.assertEqual(2, totals["count"])
        self.assertAlmostEqual(8 * car.battery_power / 100, totals["energy"])
        self.assertAlmostEqual(100 * (8 * car.battery_power / 100) / 30, totals["consumption"])

    def test_only_unknown_consumption_is_no_total_of_0(self):
        totals = insights_data.trip_totals(self.rows((0 * HOUR, 1000, 72), (12 * HOUR, 1030, 72),
                                                     (24 * HOUR, 1030, 72)))
        self.assertEqual(30, totals["distance"])
        self.assertIsNone(totals["energy"])
        self.assertIsNone(totals["consumption"])

    def test_rows_of_trip_objects_dont_show_the_frame(self):
        rows = self.rows(*FIRST_DRIVE, (GAP_END, 1060, 60), (GAP_END + PARKED, 1060, 60))
        self.assertEqual([False, True], [row["estimated"] for row in rows])
        self.assertIsNone(rows[1]["duration"])
        self.assertIsNone(rows[1]["speed_average"])
        self.assertEqual(20, rows[0]["duration"])

    def test_charts_divide_by_the_measured_km(self):
        rows = self.rows(*FIRST_DRIVE, (GAP_END, 1060, 72), (GAP_END + PARKED, 1060, 72))
        rows = [{**row, "start_at": START} for row in rows]
        local = insights_data.local_series(rows, "consumption", "7d", divisor_key="measured_distance",
                                           now=START + HOUR)
        self.assertEqual(30, sum(row[2] for row in local["rows"]))
        self.assertAlmostEqual(8 * car.battery_power / 100, sum(row[1] for row in local["rows"]))

    def test_measured_km_in_miles(self):
        rows = self.rows(*FIRST_DRIVE, (GAP_END, 1060, 60), (GAP_END + PARKED, 1060, 60))
        converted = units.convert_rows(rows, units.MI)
        self.assertAlmostEqual(30 / units.KM_PER_MI, converted[0]["measured_distance"])


class ApiAndTableTest(EstimatedTripTestCase):
    def test_every_field_stays_and_it_is_json(self):
        trips = self.trips(*FIRST_DRIVE, (GAP_END, 1060, 72), (GAP_END + PARKED, 1060, 72))
        recorded, gap = json.loads(json.dumps(Trips.get_trips_as_dict(trips), default=str))
        self.assertEqual(set(recorded), set(gap))
        for key in ("duration", "speed_average", "consumption", "consumption_km", "distance", "mileage", "start_at",
                    "end_at", "positions", "id", "altitude_diff", "consumption_by_temp"):
            self.assertIn(key, gap)
        self.assertIs(False, recorded["estimated"])
        self.assertIs(True, gap["estimated"])
        self.assertIsNone(gap["duration"])

    def test_table_shows_unknown_as_a_dash(self):
        figures.get_figures(car)
        columns = {column["id"]: column for column in figures.table_fig.columns}
        for key in ("duration", "speed_average", "consumption_km", "consumption_fuel_km"):
            self.assertEqual("–", columns[key]["format"].to_plotly_json()["nully"], key)
        self.assertNotEqual("–", columns["distance"]["format"].to_plotly_json()["nully"])


if __name__ == '__main__':
    unittest.main()
