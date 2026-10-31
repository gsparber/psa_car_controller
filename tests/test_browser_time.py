"""Dates are shown in the time zone of the browser, not the one of the server (UTC in a container).

Before, the mobile pages, the control page, the date slider and the insights bars used the time zone of the server
and the consumption chart UTC, while the tables of /v1/stats used the one of the browser.
"""
import json
import os
import shutil
import subprocess
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from dash import dcc, html

from psa_car_controller.psacc.model.car import Car
from psa_car_controller.web import units
from psa_car_controller.web.view import insights_data, status_text, ui
from tests.test_remote_command_ui import get_car_access, text
from tests.utils import get_web_view

CLIENTSIDE = Path(__file__).parent.parent / "psa_car_controller" / "web" / "assets" / "clientside.js"
VIN = "VR3UHZKXZLT000000"
NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
START = datetime(2026, 10, 29, 23, 30, tzinfo=timezone.utc)
STOP = START + timedelta(minutes=45)


def find_all(component, wanted):
    if isinstance(component, (list, tuple)):
        return [found for child in component for found in find_all(child, wanted)]
    found = [component] if isinstance(component, wanted) else []
    children = getattr(component, "children", None)
    return found + (find_all(children, wanted) if children is not None else [])


def dates(component):
    return [time.dateTime for time in find_all(component, html.Time)]


class TestPagesLeaveTheDateToTheBrowser(unittest.TestCase):

    def test_time_element_is_filled_from_the_server_date(self):
        time = ui.local_datetime(START)
        self.assertEqual(("local-time", START.isoformat()), (time.className, time.dateTime))
        self.assertEqual(f"{START.astimezone():%a %d %b, %H:%M}", time.children)

    def test_charge_row(self):
        charge = get_web_view("charge")
        row = charge._charge_row({"start_at": START, "stop_at": STOP, "kw": 5, "price": None, "start_level": 10,
                                  "end_level": 20}, "€")
        self.assertEqual([START.isoformat()], dates(row))
        self.assertIn("· 45 min", text(row))

    def test_charge_in_progress(self):
        charge = get_web_view("charge")
        row = charge._charge_row({"start_at": START, "stop_at": None, "kw": 5, "price": None, "start_level": 10,
                                  "end_level": 20}, "€")
        self.assertEqual([START.isoformat()], dates(row))
        self.assertIn("in progress", text(row))

    def test_estimated_charge_row(self):
        charge = get_web_view("charge")
        row = charge._estimated_charge_row({"start_at": START, "stop_at": STOP, "start_level": 10, "end_level": 20})
        self.assertEqual([START.isoformat(), STOP.isoformat()], dates(row))
        self.assertIn("Between", text(row))

    def test_trip_row(self):
        insights = get_web_view("insights")
        row = insights._trip_row({"start_at": START, "distance": 5, "consumption_km": 15}, units.KM)
        self.assertEqual([START.isoformat()], dates(row))

    def test_home_updated(self):
        get_car_access()
        home = get_web_view("home")
        updated = home._updated(START, 300)
        self.assertEqual([START.isoformat()], dates(updated))
        self.assertEqual("updated", find_all(updated, html.Time)[0].to_plotly_json()["props"]["data-format"])
        self.assertEqual(f"updated  {status_text.format_updated(START)}  (5 min ago)", text(updated))

    def test_home_updated_without_date(self):
        get_car_access()
        self.assertIsNone(get_web_view("home")._updated(None, None))

    def test_control_refresh_date(self):
        control = get_web_view("control")
        car = Car(VIN, "vehicle-id", "Peugeot")
        car.status = MagicMock()
        car.status.get_energy.return_value.updated_at = START
        config = MagicMock()
        config.myp.vehicles_list = [car]
        with patch.object(control.Database, "get_last_soh_by_vin", return_value=None), \
                patch.object(control, "units", MagicMock()), \
                patch.object(control, "Button") as button, patch.object(control, "Switch"):
            button.return_value.get_html.return_value = html.Div()
            control.get_control_tabs(config)
        self.assertEqual([START.isoformat()], dates(button.call_args[0][2]))

    def test_log_page_names_its_time_zone(self):
        layout = get_web_view("log").get_log_layout()
        self.assertIn("server's time zone", text(layout))

    def test_consumption_chart_uses_the_local_field(self):
        source = (CLIENTSIDE.parent.parent / "view" / "views.py").read_text(encoding="utf-8")
        self.assertIn('dcc.Graph(id="consumption_fig"), "start_at_local"', source)


class TestInsightsBars(unittest.TestCase):

    def test_local_series(self):
        rows = [{"start_at": START, "distance": 10, "consumption": 2}]
        local = insights_data.local_series(rows, "consumption", "7d", divisor_key="distance", now=NOW)
        self.assertEqual({"unit": "day", "start": (NOW - timedelta(days=7)).timestamp(), "end": NOW.timestamp(),
                          "per_100": True, "rows": [[START.timestamp(), 2, 10]]}, local)

    def test_local_series_of_all_starts_at_the_first_row(self):
        rows = [{"start_at": START, "kw": 3}]
        local = insights_data.local_series(rows, "kw", "all", now=NOW)
        self.assertEqual(("month", START.timestamp(), False), (local["unit"], local["start"], local["per_100"]))

    def test_charts_hand_their_rows_to_the_browser(self):
        insights = get_web_view("insights")
        chart = insights._chart("distance", ["2026-10-29"], [1], "km", {"rows": []})
        self.assertEqual([{"rows": []}], [store.data for store in find_all(chart, dcc.Store)])
        self.assertEqual({"type": insights.CHART, "index": "distance"}, find_all(chart, dcc.Graph)[0].id)


@unittest.skipUnless(shutil.which("node"), "needs node")
class TestClientside(unittest.TestCase):

    @staticmethod
    def run_js(expression, time_zone, language="en-GB"):
        script = (f"globalThis.window = {{console, psaccLanguage: {{tag: {json.dumps(language)}}}}};"
                  "const vm = require('vm'); const fs = require('fs');"
                  f"vm.runInThisContext(fs.readFileSync({json.dumps(str(CLIENTSIDE))}, 'utf8'));"
                  f"console.log(JSON.stringify(eval({json.dumps(expression)})))")
        result = subprocess.run(["node", "-e", script], env={**os.environ, "TZ": time_zone, "LANG": "en_US.UTF-8"},
                                capture_output=True, text=True, check=True, timeout=30)
        return json.loads(result.stdout)

    def test_time_zones(self):
        for time_zone, expected in (("Europe/Vienna", "Fri 30 Oct, 00:30"), ("America/New_York", "Thu 29 Oct, 19:30"),
                                    ("UTC", "Thu 29 Oct, 23:30")):
            self.assertEqual(expected, self.run_js("localTimeText('2026-10-29T23:30:00+00:00', 'datetime')", time_zone))

    def test_clock_change(self):
        # Vienna goes back from 03:00 to 02:00 at 01:00 UTC: the same local hour twice, one hour apart
        for utc, expected in (("2026-10-25T00:30:00Z", "Sun 25 Oct, 02:30"), ("2026-10-25T01:30:00Z", "Sun 25 Oct, 02:30"),
                              ("2026-03-29T00:59:00Z", "Sun 29 Mar, 01:59"), ("2026-03-29T01:00:00Z", "Sun 29 Mar, 03:00")):
            self.assertEqual(expected, self.run_js(f"localTimeText('{utc}', 'datetime')", "Europe/Vienna"))

    def test_date_without_offset_is_utc(self):
        for value in ("2026-10-29T23:30:00", "2026-10-29 23:30:00"):
            self.assertEqual("Fri 30 Oct, 00:30", self.run_js(f"localTimeText('{value}', 'datetime')", "Europe/Vienna"))

    def test_invalid_date_keeps_the_text(self):
        self.assertIsNone(self.run_js("localTimeText('soon', 'datetime')", "Europe/Vienna"))

    def test_updated_format(self):
        expression = ("[localTimeText(new Date(Date.now()).toISOString(), 'updated').length,"
                      "localTimeText('2020-01-05T23:30:00Z', 'updated')]")
        self.assertEqual([5, "6 Jan, 00:30"], self.run_js(expression, "Europe/Vienna"))

    def test_tables_get_a_local_field_for_the_charts(self):
        expression = ("(() => { const data = {trips: [{start_at: '2026-10-29T23:30:00+00:00'}]};"
                      "addLocaleDate(data, {trips: ['start_at']}); addLocaleDate(data, {trips: ['start_at']});"
                      "return data.trips[0] })()")
        row = self.run_js(expression, "America/New_York")
        self.assertEqual("2026-10-29 19:30:00", row["start_at_local"])
        self.assertEqual(int(START.timestamp() * 1000), row["start_at"])

    def test_charge_in_progress_has_no_stop_date(self):
        expression = ("(() => { const data = {chargings: [{start_at: '2026-10-29T23:30:00Z', stop_at: null}]};"
                      "addLocaleDate(data, {chargings: ['start_at', 'stop_at']}); return data.chargings[0] })()")
        row = self.run_js(expression, "Europe/Vienna")
        self.assertEqual(("", None), (row["stop_at_str"], row["stop_at"]))

    def test_slider_marks(self):
        expression = ("localSliderMarks({1793316600: {label: 'x'}, 1793320200: {label: 'y'}, 1793323800: {label: 'z'}})")
        marks = self.run_js(expression, "America/New_York")
        self.assertRegex(marks["1793316600"]["label"], r"^2[0-9]/10/26 \d\dh\d\d$")
        self.assertIsNone(self.run_js("localSliderMarks(null)", "Europe/Vienna"))

    def bars(self, spec, time_zone):
        return self.run_js(f"bucketSeries({json.dumps(spec)})", time_zone)

    def test_bars_use_the_days_of_the_browser(self):
        spec = {"unit": "day", "start": (START - timedelta(days=1)).timestamp(), "end": NOW.timestamp(),
                "per_100": False, "rows": [[START.timestamp(), 4, 0]]}
        self.assertEqual({"x": ["2026-10-28", "2026-10-29", "2026-10-30"], "y": [0, 4, 0]},
                         self.bars(spec, "America/New_York"))
        self.assertEqual({"x": ["2026-10-29", "2026-10-30"], "y": [0, 4]}, self.bars(spec, "Europe/Vienna"))

    def test_bars_ratio(self):
        spec = {"unit": "day", "start": START.timestamp(), "end": START.timestamp(), "per_100": True,
                "rows": [[START.timestamp(), 2, 10], [START.timestamp(), 1, 10]]}
        self.assertEqual([15], self.bars(spec, "UTC")["y"])
        spec["rows"] = [[START.timestamp(), 0, 0]]
        self.assertEqual([None], self.bars(spec, "UTC")["y"])

    def test_bars_over_clock_changes_have_every_day_once(self):
        start, end = datetime(2026, 3, 27, 12, tzinfo=timezone.utc), datetime(2026, 3, 31, 12, tzinfo=timezone.utc)
        spec = {"unit": "day", "start": start.timestamp(), "end": end.timestamp(), "per_100": False, "rows": []}
        self.assertEqual(["2026-03-27", "2026-03-28", "2026-03-29", "2026-03-30", "2026-03-31"],
                         self.bars(spec, "Europe/Vienna")["x"])
        start, end = datetime(2026, 10, 23, 12, tzinfo=timezone.utc), datetime(2026, 10, 26, 12, tzinfo=timezone.utc)
        spec.update(start=start.timestamp(), end=end.timestamp())
        self.assertEqual(["2026-10-23", "2026-10-24", "2026-10-25", "2026-10-26"],
                         self.bars(spec, "Europe/Vienna")["x"])

    def test_bars_by_month(self):
        first = datetime(2026, 1, 31, 23, 30, tzinfo=timezone.utc)
        spec = {"unit": "month", "start": first.timestamp(), "end": NOW.timestamp(), "per_100": False,
                "rows": [[first.timestamp(), 3, 0]]}
        result = self.bars(spec, "Europe/Vienna")
        self.assertEqual(("2026-02-01", "2026-10-01", 9), (result["x"][0], result["x"][-1], len(result["x"])))
        self.assertEqual(3, result["y"][0])
        self.assertEqual("2026-01-01", self.bars(spec, "America/New_York")["x"][0])


if __name__ == "__main__":
    unittest.main()
