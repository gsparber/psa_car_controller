"""A value that isn't known is shown as unknown: not as NaN, as Infinity or as a made up number."""
import json
import shutil
import subprocess
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from dash import html

from psa_car_controller.web import units
from psa_car_controller.web.view import insights_data, status_text, ui
from tests.test_remote_command_ui import text
from tests.test_status_age import get_car
from tests.utils import get_web_view

CLIENTSIDE = Path(ui.__file__).parent.parent / "assets" / "clientside.js"
NAN = float("nan")
INFINITY = float("inf")
START = datetime(2026, 10, 29, 10, tzinfo=timezone.utc)
STOP = START + timedelta(hours=1)
CHARGE = {"start_at": "2026-10-29T10:00:00Z", "stop_at": "2026-10-29T12:00:00Z", "kw": 20, "co2": 100, "price": 4}
TRIP = {"mileage": 1000, "consumption_km": 15}


def cards(data):
    """What updateCardsValue writes in the cards, with values from an earlier range in them."""
    expression = f"""(() => {{
        const elements = {{}};
        globalThis.document = {{ getElementById: id => {{ elements[id] = elements[id] || {{ innerHTML: 'old' }}; return elements[id] }} }};
        ['avg_chg_speed', 'avg_emission_kw', 'avg_emission_km', 'avg_consum_kw', 'avg_consum_price', 'elec_consum_kw',
         'elec_consum_price'].forEach(id => document.getElementById(id));
        updateCardsValue({json.dumps(data)});
        return Object.fromEntries(Object.entries(elements).map(([id, element]) => [id, element.innerHTML]))
    }})()"""
    return run_js(expression)


def run_js(expression):
    script = ("globalThis.window = {console, psaccLanguage: {tag: 'en-US'}};"
              f"require('vm').runInThisContext(require('fs').readFileSync({json.dumps(str(CLIENTSIDE))}, 'utf8'));"
              f"console.log(JSON.stringify(eval({json.dumps(expression)})))")
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True, timeout=30)
    return json.loads(result.stdout)


class TestServerNumbers(unittest.TestCase):
    def test_fmt(self):
        for value in (NAN, INFINITY, -INFINITY, None, True, "12"):
            self.assertEqual("–", ui.fmt(value, 1))

    def test_with_unit_has_no_unit_without_a_number(self):
        self.assertEqual("–", ui.with_unit(NAN, "kWh", 1))

    def test_format_int_does_not_raise(self):
        for value in (NAN, INFINITY, -INFINITY):
            self.assertEqual("–", status_text.format_int(value, "%"))

    def test_power_estimate_needs_real_numbers(self):
        self.assertIsNone(status_text.charge_power_kw(NAN, 50, 50, 100))
        self.assertIsNone(status_text.charge_power_kw(INFINITY, 50, 50, 100))

    def test_home_ring_without_a_level(self):
        ring = get_web_view("home")._soc_ring(None, None)
        self.assertNotIn("None", ring.to_plotly_json()["props"]["aria-label"])


@unittest.skipUnless(shutil.which("node"), "needs node")
class TestCardNumbers(unittest.TestCase):
    def test_nb_format(self):
        self.assertEqual(["–", "–", "–", "0.00"], run_js("[nbFormat(NaN), nbFormat(Infinity), nbFormat(-Infinity), nbFormat(0)]"))

    def test_charge_in_progress_without_energy(self):
        values = cards({"trips": [], "chargings": [{"start_at": CHARGE["start_at"], "stop_at": None, "kw": None}]})
        self.assertEqual("–", values["avg_chg_speed"])

    def test_charge_in_progress_with_energy(self):
        values = cards({"trips": [], "chargings": [{"start_at": CHARGE["start_at"], "stop_at": None, "kw": 3}]})
        self.assertEqual("–", values["avg_chg_speed"])

    def test_known_values_are_kept(self):
        values = cards({"trips": [TRIP, {**TRIP, "mileage": 1100}], "chargings": [CHARGE]})
        self.assertEqual({"avg_chg_speed": "10.00", "avg_emission_kw": "100.00", "avg_emission_km": "15.00",
                          "avg_consum_kw": "15.00", "avg_consum_price": "3.00", "elec_consum_kw": "15.00",
                          "elec_consum_price": "3.00"}, values)

    def test_no_co2_is_not_zero_emission(self):
        values = cards({"trips": [TRIP], "chargings": [{**CHARGE, "co2": None}]})
        self.assertEqual("–", values["avg_emission_kw"])
        self.assertEqual("–", values["avg_emission_km"])

    def test_no_price_is_not_free(self):
        values = cards({"trips": [TRIP, {**TRIP, "mileage": 1100}], "chargings": [{**CHARGE, "price": None}]})
        self.assertEqual("–", values["avg_consum_price"])
        self.assertEqual("–", values["elec_consum_price"])

    def test_free_charge_stays_free(self):
        values = cards({"trips": [TRIP, {**TRIP, "mileage": 1100}], "chargings": [{**CHARGE, "price": 0, "co2": 0}]})
        self.assertEqual(("0.00", "0.00"), (values["avg_consum_price"], values["avg_emission_kw"]))

    def test_no_consumption_is_not_zero(self):
        values = cards({"trips": [{**TRIP, "consumption_km": None}, {**TRIP, "mileage": 1100, "consumption_km": None}],
                        "chargings": []})
        self.assertEqual(("–", "–"), (values["avg_consum_kw"], values["elec_consum_kw"]))

    def test_no_energy_gives_no_price(self):
        values = cards({"trips": [TRIP, {**TRIP, "mileage": 1100}], "chargings": [{**CHARGE, "kw": None}]})
        self.assertEqual("–", values["avg_consum_price"])

    def test_range_without_data_clears_the_cards(self):
        self.assertEqual({"–"}, set(cards({"trips": [], "chargings": []}).values()))


class TestChargeRows(unittest.TestCase):
    def setUp(self):
        self.running = {"start_at": START, "stop_at": None, "kw": None, "price": None, "start_level": 40, "end_level": None}

    def test_charge_without_energy_is_not_0_kwh(self):
        rows = insights_data.charge_rows([self.running])
        self.assertIsNone(rows[0]["kw"])
        row = get_web_view("charge")._charge_row(rows[0], "€")
        self.assertNotIn("kWh", text(row))
        self.assertIn("–", text(row))

    def test_totals_count_the_known_energy(self):
        rows = insights_data.charge_rows([self.running, {**self.running, "kw": 12, "price": 3, "stop_at": STOP}])
        totals = insights_data.charge_totals(rows)
        self.assertEqual((12, 2), (totals["energy"], totals["count"]))
        self.assertEqual(0.25, totals["price_per_kwh"])
        self.assertEqual(12, totals["avg_power"])

    def test_no_known_energy_is_no_total(self):
        self.assertIsNone(insights_data.charge_totals(insights_data.charge_rows([self.running]))["energy"])

    def test_no_charge_at_all_is_a_total_of_zero(self):
        self.assertEqual(0, insights_data.charge_totals([])["energy"])

    def test_totals_skip_what_can_not_be_summed(self):
        rows = insights_data.charge_rows([{**self.running, "kw": NAN, "price": NAN},
                                          {**self.running, "kw": 12, "price": 3}])
        totals = insights_data.charge_totals(rows)
        self.assertEqual((12, 3), (totals["energy"], totals["cost"]))

    def test_chart_of_a_charge_without_energy(self):
        rows = insights_data.charge_rows([self.running])
        self.assertEqual([0], insights_data.series(rows, "kw", "all", STOP)[1][-1:])


def local_numbers(component):
    """The values clientside.js localizes, in the tree of a page."""
    if isinstance(component, (list, tuple)):
        return [number for child in component for number in local_numbers(child)]
    if isinstance(component, str) or component is None:
        return []
    numbers = [component.value] if type(component).__name__ == "Data" else []
    return numbers + local_numbers(getattr(component, "children", None))


class TestLocalizedPages(unittest.TestCase):
    """What the server writes as text the browser can't put in the language of the user."""

    def setUp(self):
        self.car = get_car()
        self.car.status = MagicMock()
        patcher = patch.object(units, "current", return_value=units.KM)
        patcher.start()
        self.addCleanup(patcher.stop)

    def control_cards(self, level, soh, mileage):
        control = get_web_view("control")
        self.car.status.get_energy.return_value.level = level
        self.car.status.timed_odometer.mileage = mileage
        config = MagicMock()
        config.myp.vehicles_list = [self.car]
        config.remote_control = False
        with patch.object(control.Database, "get_last_soh_by_vin", return_value=soh), \
                patch.object(control, "Switch") as switch:
            switch.return_value.get_html.return_value = html.Div()
            return control.get_control_tabs(config)

    def test_control_cards(self):
        self.assertEqual(["79.6", "94", "48470"], local_numbers(self.control_cards(79.6, 94, 48470)))

    def test_control_cards_without_a_number_are_unknown(self):
        for value in (NAN, INFINITY, None):
            with self.subTest(value):
                tabs = self.control_cards(value, None, value)
                self.assertEqual([], local_numbers(tabs))
                self.assertIn("–", text(tabs))

    def test_battery_on_the_home_page(self):
        home = get_web_view("home")
        self.assertEqual(["81"], local_numbers(home._soc_ring(81, None)))
        self.assertIn("–", text(home._soc_ring(None, None)))

    def test_battery_on_the_charge_page(self):
        charge = get_web_view("charge")
        self.car.status.get_energy.return_value.level = 81
        self.car.status.get_energy.return_value.autonomy = None
        with patch.object(charge, "remote_ready", return_value=True), \
                patch.object(charge, "is_charging", return_value=False), \
                patch.object(charge, "is_pending", return_value=False), \
                patch.object(charge, "read", return_value=None):
            self.assertEqual(["81"], local_numbers(charge._status(self.car, None)))

    def test_next_service_on_the_home_page(self):
        home = get_web_view("home")
        self.car.status.timed_odometer.mileage = None
        state = MagicMock()
        state.offline = True
        with patch.object(home, "maintenance", return_value={"mileage": 12000, "days": 150}), \
                patch.object(home, "app", return_value=state), \
                patch.object(self.car, "has_battery", return_value=False), \
                patch.object(home, "read", return_value=None):
            self.assertEqual(["12000", "150"], local_numbers(home._info(self.car)))


if __name__ == "__main__":
    unittest.main()
