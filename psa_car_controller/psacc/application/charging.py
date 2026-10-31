import logging
from datetime import datetime, timedelta
from sqlite3 import IntegrityError

from typing import List

from .battery_charge_curve import BatteryChargeCurve
from .ecomix import Ecomix
from .trip_parser import TripParser

from psa_car_controller.psacc.repository.config_repository import ElectricityPriceConfig
from psa_car_controller.psacc.repository.db import Database
from psa_car_controller.psacc.repository.trips import MIN_SLOW_GAP_KM, Trips, as_utc
from ..model.car import Car, Cars
from ..model.charge import Charge

logger = logging.getLogger(__name__)

# over a longer gap with km in it the levels no longer say what happened in between. Without km it can only be a charge
MAX_ESTIMATED_CHARGE_GAP = timedelta(hours=72)


class Charging:
    elec_price: ElectricityPriceConfig = ElectricityPriceConfig()

    @staticmethod
    def get_chargings() -> List[dict]:
        charge_db = Database.get_all_charge()
        charge_list = list(map(dict, charge_db))
        Charging._calculated_fields(charge_list)
        return charge_list

    @staticmethod
    def _overlaps_charge(start, stop, chargings: List[dict]) -> bool:
        # a charge without stop_at can still be running. Touching counts: the status that ends a charge can carry the
        # date of the first position of the next trip
        for charge in chargings:
            charge_start, charge_stop = as_utc(charge["start_at"]), as_utc(charge["stop_at"])
            if charge_start is None or charge_start > stop:
                continue
            # one started before a trip that ended isn't running any more, nothing just ended it
            if charge_start >= start if charge_stop is None else charge_stop >= start:
                return True
        return False

    @staticmethod
    def estimate_chargings(trips: Trips, chargings: List[dict]) -> List[dict]:
        """Charges the battery level between two trips of a car reveals and chargings (its recorded ones) don't cover.

        They have the fields of a recorded charge, but start_at and stop_at only frame the charge (end of the trip
        before it, start of the trip after it, or the dates of a drive in a gap) and what was charged, its price and
        its co2 aren't known: None.
        """
        estimated = []
        for start_at, stop_at, start_level, end_level, distance, mileage in Charging._level_frames(trips):
            if start_level is None or end_level is None:
                continue
            start, stop = as_utc(start_at), as_utc(stop_at)
            if stop - start > MAX_ESTIMATED_CHARGE_GAP and distance > MIN_SLOW_GAP_KM:
                continue
            # same margin as for the trips: regeneration or temperature change the level by a few percent
            if not TripParser.is_recharging(start_level - end_level, distance):
                continue
            if Charging._overlaps_charge(start, stop, chargings):
                continue
            estimated.append({"start_at": start_at, "stop_at": stop_at, "VIN": trips[0].car.vin,
                              "start_level": start_level, "end_level": end_level,
                              "mileage": mileage, "co2": None, "kw": None, "price": None,
                              "charging_mode": None, "estimated": True})
        return estimated

    @staticmethod
    def _level_frames(trips: Trips):
        """Where a charge can hide: between two trips, and inside a drive in a gap, which can have both on the way."""
        for index, trip in enumerate(trips):
            # mileage is known to 0.1 km, the rest of the difference is float noise
            start_mileage = round(trip.mileage - trip.distance, 1)
            if index > 0:
                before = trips[index - 1]
                yield (before.end_at, trip.start_at, before.end_level, trip.start_level,
                       round(start_mileage - before.mileage, 1), before.mileage)
            if trip.estimated:
                yield trip.start_at, trip.end_at, trip.start_level, trip.end_level, trip.distance, start_mileage

    @staticmethod
    def get_estimated_chargings(cars: Cars) -> List[dict]:
        """Always computed from the positions, so these rows can't leak into anything reading the battery table."""
        chargings = list(map(dict, Database.get_all_charge()))
        estimated = []
        for vin, trips in Trips.get_trips(cars).items():
            estimated.extend(Charging.estimate_chargings(trips, [c for c in chargings if c["VIN"] == vin]))
        return sorted(estimated, key=lambda charge: as_utc(charge["start_at"]))

    @staticmethod
    def get_battery_curve(conn, charge, car) -> List[BatteryChargeCurve]:
        battery_curves_dto = Database.get_battery_curve(conn, charge.start_at, charge.stop_at, charge.vin)
        battery_curves = BatteryChargeCurve.dto_to_battery_curve(car, charge, battery_curves_dto)
        return battery_curves

    @staticmethod
    def set_charge_price(charge, conn, car):
        battery_curves = Charging.get_battery_curve(conn, charge, car)
        charge.price = Charging.elec_price.get_price(charge, battery_curves)

    @staticmethod
    def set_default_price(cars: Cars):
        if Charging.elec_price.is_enable():
            conn = Database.get_db()
            charge_list = Database.get_all_charge_without_price(conn)
            for charge in charge_list:
                Charging.set_charge_price(charge, conn, cars.get_car_by_vin(charge.vin))
                Database.set_chargings_price(conn, charge)
            conn.close()

    # pylint: disable=too-many-arguments
    @staticmethod
    def update_chargings(conn, charge: Charge, car):
        Charging.set_charge_price(charge, conn, car)
        Database.update_charge(charge)
        with conn.write_lock:
            Database.clean_battery(conn)
            conn.commit()

    @staticmethod
    def is_charge_ended(charge: 'Charge'):
        return not charge or charge.stop_at

    @staticmethod
    def _drove_since_start(charge: Charge, mileage) -> bool:
        # same margin as for the trips: a car moved on a parking lot, or rounding
        return charge.mileage is not None and mileage is not None and mileage - charge.mileage > MIN_SLOW_GAP_KM

    @staticmethod
    def _close_charge(conn, charge: Charge, car, charge_date, level, latitude,
                      # pylint: disable=too-many-positional-arguments
                      longitude, country_code, mileage):
        """Network inside, so not under the write lock."""
        stop_at = charge_date
        if Charging._drove_since_start(charge, mileage):
            # The status came after a drive: neither its date nor its level is from this charge, the last seen ones are
            curve = Database.get_battery_curve(conn, charge.start_at, charge_date, charge.vin)
            if curve:
                stop_at, level = curve[-1].date, curve[-1].level
                if stop_at <= charge.start_at:
                    # Only its start was seen, the table refuses such an end. The levels of the trips can reveal it.
                    charge.stop_at = stop_at
                    Database.delete_charge(conn, charge)
                    return
        charge.end_level = level
        charge.co2 = Ecomix.get_co2_per_kw(charge.start_at, stop_at, latitude, longitude, country_code)
        charge.kw = (level - charge.start_level) / 100 * car.battery_power
        charge.stop_at = stop_at
        charge.mileage = mileage
        Charging.update_chargings(conn, charge, car)

    @staticmethod
    def record_charging(car: Car, charging_status, charge_date: datetime, level, latitude,
                        # pylint: disable=too-many-locals,too-many-positional-arguments
                        longitude, country_code, charging_mode, charging_rate, autonomy, mileage):
        conn = Database.get_db()
        charge_date = charge_date.replace(microsecond=0)
        if charging_status == "InProgress":
            last_charge = Database.get_last_charge(car.vin)
            if last_charge and last_charge.stop_at and as_utc(charge_date) < as_utc(last_charge.stop_at):
                # a status that arrives late, from before that charge ended: it would start a copy of it
                logger.debug("status older than the end of the last charge")
                conn.close()
                return
            if not Charging.is_charge_ended(last_charge) and Charging._drove_since_start(last_charge, mileage):
                # no status in between saw that charge end, this is another one
                Charging._close_charge(conn, last_charge, car, charge_date, level, latitude, longitude, country_code,
                                       mileage)
            with conn.write_lock:
                if Charging.is_charge_ended(last_charge):
                    try:
                        conn.execute(
                            "INSERT INTO battery(start_at,start_level,charging_mode,VIN,mileage) VALUES(?,?,?,?,?)",
                            (charge_date,
                             level,
                             charging_mode,
                             car.vin,
                             mileage))
                    except IntegrityError:
                        logger.debug("battery record already stored")
                    start_at = charge_date
                else:
                    start_at = last_charge.start_at
                try:
                    conn.execute(
                        "INSERT INTO battery_curve(start_at,VIN,date,level,rate,autonomy) VALUES(?,?,?,?,?,?)",
                        (start_at, car.vin, charge_date, level, charging_rate, autonomy))
                except IntegrityError:
                    # todo add charging rate to unique constraint
                    logger.debug("level already stored")
                conn.commit()  # before the network request below, the charge doesn't wait for it
            Ecomix.get_data_from_co2_signal(latitude, longitude, country_code)
        else:
            try:
                last_charge = Database.get_last_charge(car.vin)
                charge_just_finished = last_charge.stop_at is None
            except (TypeError, AttributeError):
                logger.debug("battery table is probably empty :", exc_info=True)
                charge_just_finished = False
            if charge_just_finished:
                Charging._close_charge(conn, last_charge, car, charge_date, level, latitude, longitude, country_code,
                                       mileage)
        conn.close()

    @staticmethod
    def _calculated_fields(charge_list: list):
        for c in charge_list:
            if c.get("stop_at") and c.get("start_at"):
                c.update(
                    {
                        "duration_min": (c.get("stop_at") - c.get("start_at")).total_seconds()
                        / 60,
                        "duration_str": str((c.get("stop_at") - c.get("start_at"))),
                    }
                )
