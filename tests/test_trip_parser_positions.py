"""Which positions make a trip (T77): nothing driven gets lost, and nothing else becomes a trip."""
import unittest
from datetime import datetime, timedelta, timezone

from psa_car_controller.psacc.application.charging import Charging
from psa_car_controller.psacc.model.car import Cars
from psa_car_controller.psacc.repository.db import Database
from psa_car_controller.psacc.repository.trips import Trips
from tests.utils import car, get_new_test_db, latitude, longitude, vehicule_list

START = datetime(2026, 1, 5, 8, 0, 0, tzinfo=timezone.utc)
MIN = timedelta(minutes=1)
HOUR = timedelta(hours=1)
CARS = Cars([car])
HYBRID = vehicule_list[1]
# The positions after the charge are too close for is_low_speed, until the third one restarts in the last but one round.
# Its level differs because clean_position deletes the middle one of three equal positions.
DRIVE_CHARGE_DRIVE = ((0 * MIN, 1000, 60), (20 * MIN, 1020, 50), (40 * MIN, 1040, 40),
                      (8 * HOUR, 1040, 80), (8 * HOUR + 2 * MIN, 1040, 80), (8 * HOUR + 4 * MIN, 1040, 81),
                      (8 * HOUR + 32 * MIN, 1072, 63))


def record(*points, vehicle=car):
    """Each point is (time after START, mileage, level) and, for the fuel of a hybrid, level_fuel at the end."""
    for point in points:
        delay, mileage, level = point[:3]
        Database.record_position(None, vehicle.vin, mileage, latitude, longitude, 32, START + delay, level,
                                 point[3] if len(point) > 3 else None, False, 10)


class TripParserTestCase(unittest.TestCase):
    def setUp(self):
        get_new_test_db()
        self.addCleanup(get_new_test_db)

    def trips(self, *points, vehicle=car):
        record(*points, vehicle=vehicle)
        return Trips.get_trips(Cars([vehicle])).get(vehicle.vin, [])

    def distances(self, *points):
        return [trip.distance for trip in self.trips(*points)]


class LastPairTest(TripParserTestCase):
    def test_drive_after_a_charge(self):
        trips = self.trips((0 * HOUR, 1000, 30), (12 * HOUR, 1000, 75), (12 * HOUR + 28 * MIN, 1032, 58))
        self.assertEqual([32], [trip.distance for trip in trips])
        trip = trips[0]
        self.assertEqual(START + 12 * HOUR, trip.start_at)
        self.assertEqual(START + 12 * HOUR + 28 * MIN, trip.end_at)
        self.assertEqual((75, 58), (trip.start_level, trip.end_level))
        self.assertAlmostEqual(28 / 60, trip.duration)
        self.assertAlmostEqual(17 * car.battery_power / 100, trip.consumption)
        self.assertEqual(1032, trip.mileage)
        self.assertEqual(1, trip.id)

    def test_drive_after_a_stop(self):
        self.assertEqual([32], self.distances((0 * HOUR, 1000, 75), (12 * HOUR, 1000, 75),
                                              (12 * HOUR + 28 * MIN, 1032, 58)))

    def test_only_two_positions(self):
        self.assertEqual([32], self.distances((0 * MIN, 1000, 75), (28 * MIN, 1032, 58)))

    def test_next_position_doesnt_count_the_drive_twice(self):
        points = [(0 * HOUR, 1000, 30), (12 * HOUR, 1000, 75), (12 * HOUR + 28 * MIN, 1032, 58)]
        self.assertEqual([32], self.distances(*points))
        self.assertEqual([32], self.distances((20 * HOUR, 1032, 58)))

    def test_running_drive_grows_instead_of_adding_a_trip(self):
        points = [(0 * HOUR, 1000, 30), (12 * HOUR, 1000, 75), (12 * HOUR + 28 * MIN, 1032, 58)]
        self.assertEqual([32], self.distances(*points))
        self.assertEqual([42], self.distances((12 * HOUR + 38 * MIN, 1042, 53)))

    def test_charged_in_the_last_pair(self):
        self.assertEqual([], self.distances((0 * HOUR, 1000, 75), (12 * HOUR, 1000, 75), (20 * HOUR, 1000, 90)))
        get_new_test_db()
        self.assertEqual([], self.distances((0 * HOUR, 1000, 30), (8 * HOUR, 1000, 75)))

    def test_charged_and_moved_in_the_last_pair(self):
        # how far it went on what can't be told, the levels aren't those of a drive
        self.assertEqual([], self.distances((0 * HOUR, 1000, 75), (12 * HOUR, 1000, 75), (13 * HOUR, 1030, 90)))

    def test_parked_positions(self):
        self.assertEqual([], self.distances((0 * HOUR, 1000, 75), (12 * HOUR, 1000, 75), (24 * HOUR, 1000, 74)))
        get_new_test_db()
        self.assertEqual([], self.distances((0 * HOUR, 1000, 75), (12 * HOUR, 1000, 74)))

    def test_positions_seconds_apart_on_the_same_mileage(self):
        # too close for is_low_speed: only the distance tells it isn't a trip
        self.assertEqual([], self.distances((0 * MIN, 1000, 75), (1 * MIN, 1000, 75)))
        get_new_test_db()
        self.assertEqual([], self.distances((0 * HOUR, 1000, 75), (12 * HOUR, 1000, 75), (12 * HOUR + MIN, 1000, 75)))

    def test_mileage_going_back(self):
        self.assertEqual([], self.distances((0 * HOUR, 1000, 75), (12 * HOUR, 1000, 75), (12 * HOUR + MIN, 999, 75)))

    def test_outliers_stay_discarded(self):
        too_much_used = ((0 * HOUR, 1000, 75), (12 * HOUR, 1000, 75), (12 * HOUR + 10 * MIN, 1001, 73))
        self.assertEqual([], self.distances(*too_much_used))
        get_new_test_db()
        too_fast = ((0 * HOUR, 1000, 75), (12 * HOUR, 1000, 75), (12 * HOUR + MIN, 1030, 70))
        self.assertEqual([], self.distances(*too_fast))

    def test_short_drive_stays_for_the_page_to_filter(self):
        # minimum_trip_length is applied where the trips are shown (filterShortTrip), as for all the other trips
        self.assertEqual([3], self.distances((0 * HOUR, 1000, 75), (12 * HOUR, 1000, 75),
                                             (12 * HOUR + 5 * MIN, 1003, 75)))

    def test_refuel_of_a_hybrid_in_the_last_pair(self):
        trips = self.trips((0 * HOUR, 1000, 40, 20), (12 * HOUR, 1000, 40, 20), (13 * HOUR, 1000, 40, 90),
                           vehicle=HYBRID)
        self.assertEqual([], trips)

    def test_drive_of_a_hybrid_in_the_last_pair(self):
        trips = self.trips((0 * HOUR, 1000, 40, 50), (12 * HOUR, 1000, 40, 50), (12 * HOUR + 28 * MIN, 1032, 40, 47),
                           vehicle=HYBRID)
        self.assertEqual([32], [trip.distance for trip in trips])
        self.assertEqual(round(3 * HYBRID.fuel_capacity / 100, 2), trips[0].consumption_fuel)

    def test_estimated_charge_before_the_last_drive(self):
        record(*DRIVE_CHARGE_DRIVE)
        self.assertEqual([40, 32], [trip.distance for trip in Trips.get_trips(CARS)[car.vin]])
        estimated = Charging.get_estimated_chargings(CARS)
        self.assertEqual([(40, 81)], [(charge["start_level"], charge["end_level"]) for charge in estimated])
        self.assertEqual(START + 40 * MIN, estimated[0]["start_at"])
        self.assertEqual(START + 8 * HOUR + 4 * MIN, estimated[0]["stop_at"])

    def test_recorded_charge_before_the_last_drive_isnt_estimated(self):
        record(*DRIVE_CHARGE_DRIVE)
        conn = Database.get_db()
        conn.execute("INSERT INTO battery(start_at,stop_at,VIN,start_level,end_level) VALUES(?,?,?,?,?)",
                     (START + HOUR, START + 7 * HOUR, car.vin, 40, 80))
        conn.commit()
        self.assertEqual(2, len(Trips.get_trips(CARS)[car.vin]))
        self.assertEqual([], Charging.get_estimated_chargings(CARS))


class DriveInGapTest(TripParserTestCase):
    """After a trip, a position over two hours later on another mileage: the car drove between two refreshes."""
    FIRST_DRIVE = ((0 * MIN, 1000, 80), (10 * MIN, 1015, 76), (20 * MIN, 1030, 72))
    GAP_END = 12 * HOUR + 20 * MIN

    def test_trip_of_its_own(self):
        trips = self.trips(*self.FIRST_DRIVE, (self.GAP_END, 1060, 60), (self.GAP_END + 8 * HOUR, 1060, 60),
                           (self.GAP_END + 16 * HOUR, 1060, 60))
        self.assertEqual([30, 30], [trip.distance for trip in trips])
        self.assertEqual([1, 2], [trip.id for trip in trips])
        trip = trips[1]
        self.assertEqual(START + 20 * MIN, trip.start_at)
        self.assertEqual(START + self.GAP_END, trip.end_at)
        self.assertEqual((72, 60), (trip.start_level, trip.end_level))
        self.assertEqual(1060, trip.mileage)
        self.assertAlmostEqual(12, trip.duration)
        self.assertAlmostEqual(12 * car.battery_power / 100, trip.consumption)

    def test_last_position(self):
        self.assertEqual([30, 30], self.distances(*self.FIRST_DRIVE, (self.GAP_END, 1060, 60)))

    def test_every_km_counts_once_when_a_drive_follows(self):
        points = (*self.FIRST_DRIVE, (self.GAP_END, 1060, 60), (self.GAP_END + 10 * MIN, 1070, 56),
                  (self.GAP_END + 20 * MIN, 1080, 52), (self.GAP_END + 12 * HOUR, 1080, 52))
        self.assertEqual([30, 30, 20], self.distances(*points))

    def test_two_gaps_in_a_row(self):
        self.assertEqual([30, 30, 25], self.distances(*self.FIRST_DRIVE, (self.GAP_END, 1060, 60),
                                                      (2 * self.GAP_END, 1085, 50),
                                                      (2 * self.GAP_END + 12 * HOUR, 1085, 50)))

    def test_no_trip_before_it_is_counted_once(self):
        self.assertEqual([30], self.distances((0 * HOUR, 1000, 80), (12 * HOUR, 1030, 70), (24 * HOUR, 1030, 70)))

    def test_parked_in_the_gap(self):
        self.assertEqual([30], self.distances(*self.FIRST_DRIVE, (self.GAP_END, 1030, 72),
                                              (self.GAP_END + 8 * HOUR, 1030, 70)))

    def test_too_slow_to_be_a_drive(self):
        # up to 1 km with no position for hours stays a stop, see test_estimated_trips for more km
        self.assertEqual([30], self.distances(*self.FIRST_DRIVE, (self.GAP_END, 1031, 72),
                                              (self.GAP_END + 8 * HOUR, 1031, 72)))

    def test_charged_in_the_gap(self):
        # T85: the km were driven, a charge on the way only hides the consumption
        self.assertEqual([30, 30, 20], self.distances(*self.FIRST_DRIVE, (self.GAP_END, 1060, 85),
                                                      (self.GAP_END + 20 * MIN, 1080, 75),
                                                      (self.GAP_END + 12 * HOUR, 1080, 75)))
        self.assertEqual([(72, 85)], [(charge["start_level"], charge["end_level"])
                                      for charge in Charging.get_estimated_chargings(CARS)])

    def test_small_rise_counts_no_consumption_and_no_charge(self):
        trips = self.trips(*self.FIRST_DRIVE, (self.GAP_END, 1060, 75), (self.GAP_END + 8 * HOUR, 1060, 75))
        self.assertEqual([30, 30], [trip.distance for trip in trips])
        self.assertEqual(0, trips[1].consumption)
        self.assertEqual([], Charging.get_estimated_chargings(CARS))

    def test_outlier_stays_discarded(self):
        self.assertEqual([30], self.distances(*self.FIRST_DRIVE, (self.GAP_END, 1031, 50),
                                              (self.GAP_END + 8 * HOUR, 1031, 50)))

    def test_shorter_gap_stays_in_the_trip(self):
        self.assertEqual([60], self.distances(*self.FIRST_DRIVE, (2 * HOUR, 1060, 60), (20 * HOUR, 1060, 60)))

    def test_charge_after_it_is_estimated_from_its_end(self):
        # 72 % at the end of the first trip against 60 % after the charge would have hidden this charge
        record(*self.FIRST_DRIVE, (self.GAP_END, 1060, 50), (self.GAP_END + 8 * HOUR, 1060, 60),
               (self.GAP_END + 8 * HOUR + 20 * MIN, 1080, 50), (self.GAP_END + 20 * HOUR, 1080, 50))
        self.assertEqual([30, 30, 20], [trip.distance for trip in Trips.get_trips(CARS)[car.vin]])
        estimated = Charging.get_estimated_chargings(CARS)
        self.assertEqual([(50, 60)], [(charge["start_level"], charge["end_level"]) for charge in estimated])
        self.assertEqual(START + self.GAP_END, estimated[0]["start_at"])

    def test_charge_after_it_was_recorded(self):
        record(*self.FIRST_DRIVE, (self.GAP_END, 1060, 50), (self.GAP_END + 8 * HOUR, 1060, 60),
               (self.GAP_END + 8 * HOUR + 20 * MIN, 1080, 50), (self.GAP_END + 20 * HOUR, 1080, 50))
        conn = Database.get_db()
        conn.execute("INSERT INTO battery(start_at,stop_at,VIN,start_level,end_level) VALUES(?,?,?,?,?)",
                     (START + self.GAP_END + HOUR, START + self.GAP_END + 7 * HOUR, car.vin, 50, 60))
        conn.commit()
        self.assertEqual([], Charging.get_estimated_chargings(CARS))

    def test_hybrid(self):
        trips = self.trips((0 * MIN, 1000, 40, 50), (10 * MIN, 1015, 40, 49), (20 * MIN, 1030, 40, 48),
                           (self.GAP_END, 1060, 40, 45), (self.GAP_END + 8 * HOUR, 1060, 40, 45), vehicle=HYBRID)
        self.assertEqual([30, 30], [trip.distance for trip in trips])
        self.assertEqual(round(3 * HYBRID.fuel_capacity / 100, 2), trips[1].consumption_fuel)


if __name__ == '__main__':
    unittest.main()
