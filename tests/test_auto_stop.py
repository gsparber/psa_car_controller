"""The automatic stop of the charge: how close to the limit it stops, what it does when the car doesn't follow the
stop, and what the web pages and the event log show of it.

Before, the stop waited for the car to report the limit. A Mokka-e reports its level only every 12-15 minutes while
charging, about 4 % at a time (measured on 3 Nov 2026: 34, 45, 51, 55, 59, 63, 68, 72, 76 %), so it ended at
82 % instead of 80 %. A stop the car didn't follow was sent twice, then never again until the next charge (88 %).
The pages didn't tell whether a stop was on its way.
"""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from psa_car_controller.common import events
from psa_car_controller.psa.RemoteClient import RemoteClient
from psa_car_controller.psa.connected_car_api.models.energy import Energy
from psa_car_controller.psa.connected_car_api.models.energy_charging import EnergyCharging
from psa_car_controller.psa.constants import DELAYED_CHARGE, INPROGRESS, STOPPED
from psa_car_controller.psa.mqtt_request import MQTTRequest
from psa_car_controller.psa.remote_commands import RemoteCommands
from psa_car_controller.psacc.application import charge_control as charge_control_module
from psa_car_controller.psacc.application.charge_control import ChargeControl, ChargeControls, CONFIRMED, GAVE_UP, \
    RETRYING, UNCONFIRMED, WATCHING, describe_settings
from psa_car_controller.psacc.model.car import Car, Cars
from psa_car_controller.psacc.model.battery_curve import BatteryCurveDto
from psa_car_controller.psacc.model.car_status import CarStatus
from psa_car_controller.psacc.repository.db import Database
from psa_car_controller.psacc.repository.trips import as_utc
from tests.test_remote_command_ui import text
from tests.utils import get_new_test_db, get_web_view

VIN = "VR3UHZKXZLT000000"
# 0.3 % per minute, about a Mokka-e on 11 kW
SPEED = 0.3


def get_status(level, minutes_old=0, status=INPROGRESS, remaining_time="PT3H", rate=None, autonomy=None):
    date = datetime.now(timezone.utc) - timedelta(minutes=minutes_old)
    charging = EnergyCharging(status=status, next_delayed_time="PT22H", charging_mode="Slow",
                              remaining_time=remaining_time, charging_rate=rate)
    return CarStatus(links={}, updated_at=date, energy=[Energy(type="Electric", level=level, autonomy=autonomy,
                                                               updated_at=date, charging=charging)])


class AutoStopTestCase(unittest.TestCase):

    def setUp(self):
        patcher = patch.object(ChargeControl, "MQTT_TIMEOUT", 0)
        patcher.start()
        self.addCleanup(patcher.stop)
        events.EVENTS.clear()
        self.car = Car(VIN, "vehicle-id", "Opel")
        self.psacc = MagicMock()
        self.psacc.vehicles_list = Cars([self.car])
        # 3 minutes between refreshes: the level is estimated 0.5 + 1.5 minutes ahead
        self.psacc.refresh_delay = 180
        # the charges recorded before, to learn from
        self.curves = []
        patcher = patch.object(Database, "get_last_battery_curves", side_effect=lambda vin, count: self.curves)
        patcher.start()
        self.addCleanup(patcher.stop)
        # the status after the stop: what the car does with it
        self.after_stop = None
        self.psacc.get_vehicle_info.side_effect = self.answer
        self.remote = self.psacc.remote_client
        self.remote.vehicles_list = self.psacc.vehicles_list
        self.remote.commands = RemoteCommands()
        self.remote.get_charge_hour.side_effect = lambda vin: RemoteClient.get_charge_hour(self.remote, vin)
        self.remote.charge_now.side_effect = self.charge_now

    def charge_now(self, vin, now):
        RemoteClient.charge_now(self.remote, vin, now)
        request = MQTTRequest("/VehCharge", vin, {"type": DELAYED_CHARGE, "program": {"hour": 22, "minute": 0}},
                              "AP-customer")
        self.remote.commands.add(request)

    def answer(self, vin, **_):
        if self.after_stop is not None and self.stops():
            self.car.status = self.after_stop
        return self.car.status

    def stops(self):
        return self.remote.veh_charge_request.call_count

    def charge_control(self, threshold=80, estimate=True, stop_margin=0):
        # noinspection PyTypeChecker
        return ChargeControl(self.psacc, VIN, threshold, None, estimate, stop_margin)

    def report(self, cc, level, minutes_old=0, **kwargs):
        """The car reports its level, charge control runs as after each refresh."""
        self.car.status = get_status(level, minutes_old, **kwargs)
        cc.process()

    def charging_since(self, cc, levels_every_13_min, last_minutes_old):
        """Levels reported every 13 minutes, the last one last_minutes_old minutes ago: the ones before it were
        seen when they were new."""
        now = datetime.now(timezone.utc)
        count = len(levels_every_13_min)
        cc._level_points = [(now - timedelta(minutes=(count - 1 - index) * 13 + last_minutes_old), level)
                            for index, level in enumerate(levels_every_13_min[:-1])]
        self.report(cc, levels_every_13_min[-1], minutes_old=last_minutes_old)


class TestStopNearTheLimit(AutoStopTestCase):

    def setUp(self):
        super().setUp()
        # the car follows the stop: one stop each time
        self.after_stop = get_status(80, status=STOPPED)

    def test_stops_before_the_next_report_crosses_the_limit(self):
        # GIVEN 76 % reported 10 minutes ago, while the charge went up 4 % every 13 minutes
        cc = self.charge_control()
        self.charging_since(cc, [68, 72, 76], last_minutes_old=10)
        # THEN about 79 % now and 79.6 % with the lead: no stop yet
        self.assertEqual(0, self.stops())
        # WHEN 3 more minutes pass: about 80.5 % with the lead
        self.report(cc, 76, minutes_old=13)
        self.assertEqual(1, self.stops())

    def test_old_behaviour_waited_for_the_report(self):
        # the level reported says 79 %: the estimate says it is over 80 % by now
        cc = self.charge_control()
        self.charging_since(cc, [71, 75, 79], last_minutes_old=4)
        self.assertEqual(1, self.stops())

    def test_estimate_from_the_remaining_time_without_reports(self):
        # 78.5 % and 2 h to 100 %: 0.18 % per minute, 78.5 % + 12 min * 0.18 = 80.7 %
        cc = self.charge_control()
        self.report(cc, 78.5, minutes_old=10, remaining_time="PT2H")
        self.assertEqual(1, self.stops())

    def test_no_estimate_from_a_level_too_old(self):
        cc = self.charge_control()
        self.charging_since(cc, [60, 64, 68, 72], last_minutes_old=ChargeControl.MAX_ESTIMATE_MINUTES + 1)
        self.assertEqual(0, self.stops())

    def test_no_stop_far_below_the_limit(self):
        cc = self.charge_control()
        self.charging_since(cc, [40, 44, 48], last_minutes_old=12)
        self.assertEqual(0, self.stops())
        self.assertEqual(WATCHING, cc.get_state()["phase"])

    def test_estimate(self):
        cc = self.charge_control()
        self.charging_since(cc, [50, 54], last_minutes_old=5)
        # 4 % in 13 minutes, 5 + 2 minutes later
        self.assertAlmostEqual(54 + 4 / 13 * 7, cc.estimated_level(self.car.status), places=1)

    def test_estimate_is_off_by_default(self):
        # noinspection PyTypeChecker
        cc = ChargeControl(self.psacc, VIN, 80, None)
        self.charging_since(cc, [71, 75, 79], last_minutes_old=12)
        self.assertEqual(0, self.stops())
        self.report(cc, 80)
        self.assertEqual(1, self.stops())

    def test_estimate_is_in_the_debug_log(self):
        cc = self.charge_control()
        with self.assertLogs("psa_car_controller.psacc.application.charge_control", "DEBUG") as logs:
            self.charging_since(cc, [68, 72, 76], last_minutes_old=10)
        line = [message for message in logs.output if "estimate of" in message][0]
        for expected in ("79.7 %", "76 % first reported 10.0 min ago", "3 levels, 68 → 76 % in 26 min", "limit 80 %"):
            self.assertIn(expected, line)

    def test_why_there_is_no_estimate_is_in_the_debug_log(self):
        cc = self.charge_control()
        with self.assertLogs("psa_car_controller.psacc.application.charge_control", "DEBUG") as logs:
            self.report(cc, 60, remaining_time=None)
        self.assertTrue(any("estimate of" in message and "too few reports" in message for message in logs.output))

    def test_levels_of_the_last_charge_are_forgotten(self):
        cc = self.charge_control()
        self.charging_since(cc, [20, 40], last_minutes_old=0)
        self.report(cc, 40, status=STOPPED)
        self.report(cc, 76, minutes_old=10, remaining_time=None)
        # only one level of this charge and no remaining time: no estimate, the fast old charge doesn't count
        self.assertIsNone(cc.estimated_level(self.car.status))
        self.assertEqual(0, self.stops())


def e208_charge_of_10_nov():
    """The battery curve recorded on 10 Nov 2026 (e-208, 11 kW wallbox), first report of each level: (minutes after
    08:30 UTC+1, level, rate in km/h, range in km). 11 kW up to 47 %, then about 7.5 kW; the stop at 80 % was sent
    at 78 % + 9.9 min * 0.200 %/min = 80.4 % (4 levels in 15 minutes) and the car ended at 81 %."""
    points = [(0.8, 28, 64, 64), (43.9, 47, 50, 110), (44.8, 48, 50, 112), (73.9, 56, 42, 138), (74.7, 57, 50, 140),
              (96.2, 62, 42, 156), (96.9, 63, 42, 156), (114.0, 67, 42, 170), (128.1, 71, 42, 180),
              (142.2, 74, 42, 190), (143.0, 75, 42, 192), (156.2, 78, 42, 202), (168.3, 81, 0, 212)]
    start = datetime(2026, 11, 10, 7, 30, tzinfo=timezone.utc)
    return [BatteryCurveDto(start + timedelta(minutes=minutes), level, rate, autonomy)
            for minutes, level, rate, autonomy in points]


class TestSpeedOfTheCharge(AutoStopTestCase):
    """How fast the charge goes: from the charging rate the car sends, as much of it as the last charges showed."""

    def setUp(self):
        super().setUp()
        self.after_stop = get_status(80, status=STOPPED)

    def test_from_the_charging_rate(self):
        # 42 km/h, 204 km at 78 %: 2.6 km per %, 0.268 % per minute
        cc = self.charge_control()
        self.report(cc, 78, minutes_old=1, rate=42, autonomy=204)
        self.assertAlmostEqual(78 + 42 * 78 / 204 / 60 * 3, cc.estimated_level(self.car.status), places=2)

    def test_a_lower_power_counts_at_once(self):
        # GIVEN the levels of the 11 kW part: 0.44 % per minute
        cc = self.charge_control()
        self.charging_since(cc, [28, 47], last_minutes_old=0)
        # WHEN the car reports 7.5 kW: 42 km/h instead of 64
        self.report(cc, 48, rate=42, autonomy=112)
        # THEN the estimate goes with it, not with the levels
        self.assertAlmostEqual(48 + 42 * 48 / 112 / 60 * 2, cc.estimated_level(self.car.status), places=2)

    def test_from_the_levels_without_a_rate(self):
        # every 13 minutes 4 %, for more than LEVEL_WINDOW_MINUTES: the old ones go, the one before the window stays
        cc = self.charge_control()
        for level in (36, 40, 44, 48, 52, 56):
            cc._record_level(get_status(level, minutes_old=(56 - level) / 4 * 13).get_energy("Electric"))
        self.assertEqual([40, 44, 48, 52, 56], [level for _, level in cc._level_points])

    def test_the_same_level_again_isnt_a_new_one(self):
        # after a wakeup the car reports the same level twice: the first report says when it was reached
        cc = self.charge_control()
        self.assertTrue(cc._record_level(get_status(78, minutes_old=2).get_energy("Electric")))
        self.assertFalse(cc._record_level(get_status(78, minutes_old=1).get_energy("Electric")))
        self.assertEqual(1, len(cc._level_points))
        # so 2 minutes from the first report of 78 %, not 1 from the last one
        self.report(cc, 78, minutes_old=1, rate=42, autonomy=204)
        self.assertAlmostEqual(78 + 42 * 78 / 204 / 60 * 4, cc.estimated_level(self.car.status), places=1)

    def test_learned_from_the_charges_recorded(self):
        # 10 Nov: 53 % in 168 minutes, where the rate promised 58 %
        self.curves = [e208_charge_of_10_nov()]
        cc = self.charge_control()
        cc.learn_speed_factor()
        self.assertAlmostEqual(0.91, cc._speed_factor, places=2)
        self.assertEqual("learned from 53 % in 1 charges", cc._speed_factor_source)

    def test_learned_when_a_level_is_new(self):
        self.curves = [e208_charge_of_10_nov()]
        cc = self.charge_control()
        self.report(cc, 60, rate=42, autonomy=150)
        self.assertAlmostEqual(0.91, cc._speed_factor, places=2)
        self.curves = []
        self.report(cc, 60, rate=42, autonomy=150)
        self.assertAlmostEqual(0.91, cc._speed_factor, places=2)

    def test_not_learned_from_too_little(self):
        self.curves = [e208_charge_of_10_nov()[-4:]]
        cc = self.charge_control()
        cc.learn_speed_factor()
        self.assertEqual(1.0, cc._speed_factor)
        self.assertEqual("not learned, 7 % recorded", cc._speed_factor_source)

    def test_gaps_and_missing_rates_dont_count(self):
        start = datetime(2026, 11, 1, tzinfo=timezone.utc)
        self.curves = [[BatteryCurveDto(start, 20, 40, 100),
                        BatteryCurveDto(start + timedelta(minutes=90), 50, None, 250),
                        BatteryCurveDto(start + timedelta(minutes=100), 55, 0, 270),
                        BatteryCurveDto(start + timedelta(minutes=110), 60, 0, 290)]]
        cc = self.charge_control()
        cc.learn_speed_factor()
        self.assertEqual("not learned, 0 % recorded", cc._speed_factor_source)

    def test_bounded(self):
        start = datetime(2026, 11, 1, tzinfo=timezone.utc)
        # 20 % in 20 minutes where the rate promised 2 %
        self.curves = [[BatteryCurveDto(start + timedelta(minutes=minutes), 20 + minutes, 6, 100)
                        for minutes in range(21)]]
        cc = self.charge_control()
        cc.learn_speed_factor()
        self.assertEqual(ChargeControl.SPEED_FACTOR_RANGE[1], cc._speed_factor)

    def test_without_a_database(self):
        cc = self.charge_control()
        with patch.object(Database, "get_last_battery_curves", side_effect=OSError("no database")):
            cc.learn_speed_factor()
        self.assertEqual(1.0, cc._speed_factor)

    def test_the_rate_is_in_the_debug_log(self):
        cc = self.charge_control()
        with self.assertLogs("psa_car_controller.psacc.application.charge_control", "DEBUG") as logs:
            self.report(cc, 78, rate=42, autonomy=204)
        line = [message for message in logs.output if "estimate of" in message][0]
        self.assertIn("rate 42 km/h, 204 km at 78 %, x 1.00 (not learned, 0 % recorded)", line)


class TestStopOf10Nov(AutoStopTestCase):
    """The charge of 10 Nov again, refreshed every 2 minutes: 78 % first reported at 10:06:09 UTC, then the car slept.
    It charged about 0.25 % per minute, so it was at 80 % around 10:14."""

    def setUp(self):
        super().setUp()
        self.after_stop = get_status(80, status=STOPPED)
        self.psacc.refresh_delay = 120
        self.curves = [e208_charge_of_10_nov()[:-1]]

    def refresh(self, cc, minutes_after_78):
        now = datetime.now(timezone.utc)
        cc._level_points = [(now - timedelta(minutes=minutes_after_78), 78)]
        # the last report of 78 %, 1.9 minutes after the first
        self.report(cc, 78, minutes_old=minutes_after_78 - 1.9, rate=42, autonomy=204)

    def test_stops_at_80_not_81(self):
        cc = self.charge_control()
        cc.learn_speed_factor()
        # 10:11:54: 78 % + 5.75 min * 0.244 = 79.4 %, 79.8 % with the lead of 1.5 min
        self.refresh(cc, 5.75)
        self.assertEqual(0, self.stops())
        # 10:13:54: 80.3 % with the lead, the car stops about 80 % (before: at 10:17:56, 81 %)
        self.refresh(cc, 7.75)
        self.assertEqual(1, self.stops())

    def test_lead(self):
        cc = self.charge_control()
        self.assertEqual(1.5, cc.lead_minutes())
        self.psacc.refresh_delay = 3600
        self.assertEqual(ChargeControl.STOP_DELAY_MINUTES + ChargeControl.MAX_REFRESH_LEAD_MINUTES / 2,
                         cc.lead_minutes())
        self.psacc.refresh_delay = None
        self.assertEqual(ChargeControl.STOP_DELAY_MINUTES, cc.lead_minutes())


class TestLastBatteryCurves(unittest.TestCase):
    """What the speed of the charge is learned from: the curves the charges recorded, in the database."""

    def test_last_charges_with_the_one_going_on(self):
        conn = get_new_test_db()
        self.addCleanup(Database.close_db)
        start = datetime(2026, 11, 1, 7, 0, tzinfo=timezone.utc)
        for day in range(3):
            start_at = start + timedelta(days=day)
            conn.execute("INSERT INTO battery(start_at, VIN, start_level) VALUES(?,?,?)", (start_at, VIN, 20))
            for minute in (0, 15):
                conn.execute("INSERT INTO battery_curve(start_at,VIN,date,level,rate,autonomy) VALUES(?,?,?,?,?,?)",
                             (start_at, VIN, start_at + timedelta(minutes=minute), 20 + minute, 42, 100))
        conn.execute("INSERT INTO battery_curve(start_at,VIN,date,level,rate,autonomy) VALUES(?,?,?,?,?,?)",
                     (start, "OTHER", start, 30, 42, 100))
        conn.commit()
        curves = Database.get_last_battery_curves(VIN, 2)
        self.assertEqual(2, len(curves))
        self.assertEqual([[20, 35], [20, 35]], [[point.level for point in curve] for curve in curves])
        self.assertEqual(start + timedelta(days=2, minutes=15), as_utc(curves[-1][-1].date))


class TestStopNotFollowed(AutoStopTestCase):

    def test_stop_is_sent_again_later(self):
        # GIVEN a car that keeps charging whatever it is told
        cc = self.charge_control()
        self.report(cc, 81)
        # sent, still charging after the check, sent again: as before
        self.assertEqual(2, self.stops())
        self.assertEqual(RETRYING, cc.get_state()["phase"])
        # before, that was all until the next charge; now again after LATE_RETRY_INTERVAL
        self.report(cc, 82)
        self.assertEqual(2, self.stops())
        cc._last_stop_at -= timedelta(minutes=ChargeControl.LATE_RETRY_INTERVAL)
        self.report(cc, 83)
        self.assertEqual(4, self.stops())
        cc._last_stop_at -= timedelta(minutes=ChargeControl.LATE_RETRY_INTERVAL)
        self.report(cc, 84)
        self.assertEqual(4, self.stops())
        self.assertEqual(GAVE_UP, cc.get_state()["phase"])

    def test_late_retry_after_the_interval(self):
        cc = self.charge_control()
        self.report(cc, 81)
        self.assertEqual(2, self.stops())
        cc._last_stop_at -= timedelta(minutes=ChargeControl.LATE_RETRY_INTERVAL - 1)
        self.report(cc, 82)
        self.assertEqual(2, self.stops())
        cc._last_stop_at -= timedelta(minutes=2)
        # the car follows this one
        self.after_stop = get_status(83, status=STOPPED)
        self.report(cc, 83)
        self.assertEqual(3, self.stops())
        self.assertEqual(CONFIRMED, cc.get_state()["phase"])

    def test_no_more_than_the_maximum(self):
        cc = self.charge_control()
        for level in range(81, 90):
            self.report(cc, level)
            if cc._last_stop_at:
                cc._last_stop_at -= timedelta(hours=1)
        self.assertEqual(ChargeControl.MAX_STOP_ATTEMPTS, self.stops())
        self.assertEqual(GAVE_UP, cc.get_state()["phase"])
        self.assertTrue(any(event.level == "ERROR" for event in events.EVENTS.entries([events.CHARGE_CONTROL])))

    def test_a_followed_stop_is_confirmed(self):
        cc = self.charge_control()
        self.after_stop = get_status(81, status=STOPPED)
        self.report(cc, 81)
        self.assertEqual(1, self.stops())
        state = cc.get_state()
        self.assertEqual(CONFIRMED, state["phase"])
        self.assertIn("81", state["message"])
        self.assertEqual("sent", state["command"]["status"])

    def test_unconfirmed_stop_is_confirmed_by_the_next_status(self):
        cc = self.charge_control()
        self.after_stop = get_status(81, status=None)
        self.after_stop.get_energy("Electric").charging = None
        self.report(cc, 81)
        self.assertEqual(UNCONFIRMED, cc.get_state()["phase"])
        self.report(cc, 81, status=STOPPED)
        self.assertEqual(CONFIRMED, cc.get_state()["phase"])

    def test_charge_ended_by_itself(self):
        cc = self.charge_control()
        self.report(cc, 60)
        self.report(cc, 62, status=STOPPED)
        self.assertIsNone(cc.get_state())
        self.assertIn("ended before", events.EVENTS.entries([events.CHARGE_CONTROL])[0].message)

    def test_get_dict_has_the_state_but_no_internals(self):
        cc = self.charge_control()
        self.report(cc, 60)
        data = cc.get_dict()
        self.assertEqual(WATCHING, data["auto_stop"]["phase"])
        for name in ("_last_stop_at", "_level_points", "_speed_factor", "_speed_factor_source", "_state", "psacc",
                     "_last_wakeup"):
            self.assertNotIn(name, data)

    def test_events_dont_repeat_while_watching(self):
        cc = self.charge_control()
        for level in (50, 52, 54):
            self.report(cc, level)
        self.assertEqual(1, len(events.EVENTS.entries([events.CHARGE_CONTROL])))


class TestStopMargin(AutoStopTestCase):

    def test_stops_before_the_limit(self):
        cc = self.charge_control(estimate=False, stop_margin=5)
        self.report(cc, 74)
        self.assertEqual(0, self.stops())
        self.assertIn("at 75 % (5 % before the limit of 80 %)", cc.get_state()["message"])
        self.report(cc, 75)
        self.assertGreaterEqual(self.stops(), 1)

    def test_ignored_with_the_estimate(self):
        cc = self.charge_control(estimate=True, stop_margin=5)
        self.assertEqual(80, cc.stop_level())
        self.report(cc, 76)
        self.assertEqual(0, self.stops())

    def test_no_limit_no_margin(self):
        self.assertEqual(100, self.charge_control(threshold=100, estimate=False, stop_margin=5).stop_level())

    def test_invalid_values_are_the_defaults(self):
        for estimate, margin in (("yes", 5.5), (1, -1), (None, 21), (True, True)):
            with self.subTest((estimate, margin)):
                cc = self.charge_control(estimate=estimate, stop_margin=margin)
                self.assertEqual(estimate is True, cc.estimate)
                self.assertEqual(0, cc.stop_margin)

    def test_wakeups_aim_at_the_earlier_stop(self):
        cc = self.charge_control(estimate=False, stop_margin=10)
        status = get_status(60, remaining_time="PT4H")
        # 10 % to go instead of 20 % at 0.17 %/min
        self.assertAlmostEqual(10 * 240 / 40, cc._minutes_to_threshold(status.get_energy("Electric")))


class TestSettingsFile(unittest.TestCase):

    def test_saved_only_when_set(self):
        with tempfile.TemporaryDirectory() as directory:
            name = os.path.join(directory, "charge_config.json")
            controls = ChargeControls(name)
            # noinspection PyTypeChecker
            controls["A"] = ChargeControl(None, "A", 80, [6, 0])
            # noinspection PyTypeChecker
            controls["B"] = ChargeControl(None, "B", 80, None, True, 5)
            controls.save_config()
            with open(name, encoding="utf-8") as file:
                saved = json.load(file)
            self.assertEqual({"percentage_threshold": 80, "stop_hour": [6, 0]}, saved["A"])
            self.assertEqual({"percentage_threshold": 80, "stop_hour": None, "estimate": True, "stop_margin": 5},
                             saved["B"])
            loaded = ChargeControls.load_config(MagicMock(), name)
            self.assertEqual((True, 5), (loaded["B"].estimate, loaded["B"].stop_margin))
            self.assertEqual((False, 0), (loaded["A"].estimate, loaded["A"].stop_margin))


class TestDescribeSettings(unittest.TestCase):
    def test_texts(self):
        self.assertEqual("off", describe_settings(100, None))
        self.assertEqual("off", describe_settings(100, [0, 0]))
        self.assertEqual("at 80 %", describe_settings(80, None))
        self.assertEqual("at 80 % or at 06:30", describe_settings(80, [6, 30]))
        self.assertEqual("at 06:30", describe_settings(100, [6, 30]))
        self.assertEqual("at 80 % (estimated level)", describe_settings(80, None, True, 5))
        self.assertEqual("at 75 % (5 % before 80 %)", describe_settings(80, None, False, 5))


class TestSettingsOnThePageAndTheApi(AutoStopTestCase):

    def setUp(self):
        super().setUp()
        self.charge = get_web_view("charge")
        self.api = get_web_view("api")
        self.cc = self.charge_control(estimate=False)
        self.controls = MagicMock()
        self.controls.get.return_value = self.cc
        patcher = patch.object(self.charge, "app", return_value=MagicMock(chc=self.controls))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_advanced_is_folded_with_the_values(self):
        self.cc.set_estimate(False, 5)
        advanced = self.charge._advanced(self.cc)
        self.assertNotIn("open", advanced.to_plotly_json()["props"])
        self.assertIn("Advanced", text(advanced))

    def test_save(self):
        self.charge.save_settings(True, 80, "", True, 5, VIN)
        self.assertEqual((True, 5), (self.cc.estimate, self.cc.stop_margin))
        self.controls.save_config.assert_called_once()
        self.assertIn("estimated level", events.EVENTS.entries([events.USER])[0].message)

    def test_invalid_margin_isnt_saved(self):
        for margin in (21, -1, 2.5, "x", None):
            with self.subTest(margin):
                saved, _ = self.charge.save_settings(True, 80, "", False, margin, VIN)
                self.assertIn("0 to 20", saved)
        self.controls.save_config.assert_not_called()

    def test_margin_hint(self):
        self.assertIn("75", text(self.charge.show_margin(80, 5, False)[0]))
        self.assertEqual(("Off while the level is estimated.", True), self.charge.show_margin(80, 5, True))

    def test_api(self):
        app = MagicMock(chc=self.controls)
        with patch.object(self.api, "APP", app):
            client = self.api.app.test_client()
            answer = client.get(f"/charge_control?vin={VIN}&estimate=0&margin=5").get_json()
            self.assertEqual((5, False), (answer["stop_margin"], answer["estimate"]))
            self.assertEqual(400, client.get(f"/charge_control?vin={VIN}&margin=30").status_code)
            self.assertEqual(400, client.get(f"/charge_control?vin={VIN}&estimate=yes").status_code)
            client.get(f"/charge_control?vin={VIN}&estimate=1")
        self.assertEqual((True, 5), (self.cc.estimate, self.cc.stop_margin))


class TestPages(AutoStopTestCase):

    def setUp(self):
        super().setUp()
        self.car_access = get_web_view("car_access")
        # imported before the patch: home takes app() from car_access when it is imported
        self.home = get_web_view("home")
        self.cc = self.charge_control()
        app = MagicMock()
        app.chc.get.return_value = self.cc
        app.myp.remote_client = self.remote
        patcher = patch.object(self.car_access, "app", return_value=app)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_nothing_without_a_charge(self):
        self.assertIsNone(self.car_access.auto_stop_state(VIN))
        self.assertIsNone(self.car_access.auto_stop_line(None))

    def test_watching(self):
        self.report(self.cc, 60)
        state = self.car_access.auto_stop_state(VIN)
        self.assertFalse(self.car_access.auto_stop_busy(state))
        self.assertIn("stops at 80", text(self.car_access.auto_stop_line(state)))

    def test_waiting_makes_the_button_wait(self):
        self.charge_now(VIN, False)
        self.cc.retry_count = 1
        self.cc._set_state(charge_control_module.WAITING, "limit", "Stop sent")
        state = self.car_access.auto_stop_state(VIN)
        self.assertTrue(self.car_access.auto_stop_busy(state))
        line = text(self.car_access.auto_stop_line(state))
        # the steps of a command sent by hand
        self.assertIn("Sent", line)
        self.assertIn("Stop", line)

    def test_unconfirmed_waits_only_while_the_command_does(self):
        self.charge_now(VIN, False)
        self.cc._set_state(UNCONFIRMED, "limit", "can't tell")
        self.assertTrue(self.car_access.auto_stop_busy(self.car_access.auto_stop_state(VIN)))
        command = self.remote.commands.to_list(VIN)[-1]
        self.remote.commands.answer(command["correlation_id"], "0")
        self.assertFalse(self.car_access.auto_stop_busy(self.car_access.auto_stop_state(VIN)))

    def test_old_confirmed_stop_isnt_shown(self):
        self.cc._set_state(CONFIRMED, "limit", "Charge stopped")
        self.assertIsNotNone(self.car_access.auto_stop_state(VIN))
        self.cc._state["since"] -= timedelta(seconds=self.car_access.CONFIRMED_SHOWN_FOR + 1)
        self.assertIsNone(self.car_access.auto_stop_state(VIN))

    def test_home_tile(self):
        self.charge_now(VIN, False)
        self.cc._set_state(charge_control_module.WAITING, "limit", "Stop sent")
        self.car.status = get_status(81)
        with patch.object(self.home, "auto_stop_state", self.car_access.auto_stop_state):
            tiles = self.home._tiles(self.car, True, None)
        self.assertIn("Auto-stop: waiting for car", text(tiles))


if __name__ == "__main__":
    unittest.main()
