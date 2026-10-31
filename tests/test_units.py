"""Display units (km or mi): conversion is done right before showing values, recorded data stays metric.

Idea from upstream PR #1181 (imperial units), rebuilt for the mobile UI and its settings page."""
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from psa_car_controller.psacc.model.car import Car
from psa_car_controller.psacc.repository.config_repository import ConfigRepository
from psa_car_controller.web import figures, units
from psa_car_controller.web.view import status_text, ui
from tests.utils import get_web_view

TRIP = {"id": 1, "distance": 100.0, "mileage": 16093.44, "speed_average": 80.4672, "consumption_km": 16.0,
        "consumption_fuel_km": 5.0, "consumption": 16.0, "duration": 75, "altitude_diff": 12}


def _view(name):
    return get_web_view(name)


def service_text(*args):
    return "".join(getattr(child, "children", child) for child in ui.next_service(*args))


def _suffix(column):
    return column["format"].to_plotly_json()["locale"]["symbol"][1]


class TestUnitConversion(unittest.TestCase):
    def test_normalize(self):
        for value in ("km", "KM", " km ", "kilometres", "metric"):
            self.assertEqual(units.KM, units.normalize(value))
        for value in ("mi", "Miles", "imperial", "mile"):
            self.assertEqual(units.MI, units.normalize(value))
        # unknown or empty values must never break a page
        for value in (None, "", "furlong", 3):
            self.assertEqual(units.KM, units.normalize(value))

    def test_distance(self):
        self.assertAlmostEqual(62.137, units.distance(100, "mi"), places=3)
        self.assertEqual(100, units.distance(100, "km"))
        self.assertAlmostEqual(100, units.to_km(units.distance(100, "mi"), "mi"))
        self.assertIsNone(units.distance(None, "mi"))
        self.assertEqual("–", units.distance("–", "mi"))
        self.assertTrue(units.distance(True, "mi"))  # bool is no number

    def test_consumption_per_100_miles_grows(self):
        # 16 kWh for 100 km = 25.75 kWh for 100 mi (160.9 km), not 9.94
        self.assertAlmostEqual(25.75, units.per_distance(16, "mi"), places=2)
        self.assertEqual(16, units.per_distance(16, "km"))
        self.assertIsNone(units.per_distance(None, "mi"))

    def test_labels(self):
        self.assertEqual(("km", "km/h", "kWh/100km", "L/100km"),
                         (units.distance_label("km"), units.speed_label("km"), units.consumption_label("km"),
                          units.consumption_label("km", "L")))
        self.assertEqual(("mi", "mph", "kWh/100mi", "L/100mi"),
                         (units.distance_label("mi"), units.speed_label("mi"), units.consumption_label("mi"),
                          units.consumption_label("mi", "L")))

    def test_convert_rows_metric_is_unchanged(self):
        rows = [dict(TRIP)]
        self.assertIs(rows, units.convert_rows(rows, "km"))
        self.assertIsNone(units.convert_rows(None, "mi"))

    def test_convert_rows_miles(self):
        rows = [dict(TRIP), {"id": 2, "distance": None, "consumption_km": None}]
        converted = units.convert_rows(rows, "mi", keep_km=("distance",))
        trip = converted[0]
        self.assertAlmostEqual(62.137, trip["distance"], places=3)
        self.assertAlmostEqual(10000, trip["mileage"], places=6)
        self.assertAlmostEqual(50, trip["speed_average"], places=6)
        self.assertAlmostEqual(25.75, trip["consumption_km"], places=2)
        self.assertAlmostEqual(8.05, trip["consumption_fuel_km"], places=2)
        # energy, durations and altitudes have no distance in them
        for key in ("consumption", "duration", "altitude_diff", "id"):
            self.assertEqual(TRIP[key], trip[key])
        self.assertEqual(100.0, trip["distance_km"])  # for the minimum trip length (km) filter
        self.assertIsNone(converted[1]["distance"])
        self.assertNotIn("mileage", converted[1])
        # the source (the trips cache) stays metric
        self.assertEqual(TRIP, rows[0])

    def test_convert_chargings_only_mileage(self):
        charge = {"mileage": 1609.344, "kw": 20.7, "price": 4.29, "co2": 50}
        converted = units.convert_chargings([charge], "mi")[0]
        self.assertAlmostEqual(1000, converted["mileage"])
        self.assertEqual((20.7, 4.29, 50), (converted["kw"], converted["price"], converted["co2"]))
        self.assertEqual(1609.344, charge["mileage"])

    def test_current_reads_config(self):
        config = MagicMock()
        config.General.length_unit = "mi"
        with patch("psa_car_controller.psacc.application.car_controller.PSACarController") as controller:
            controller.return_value.config = config
            self.assertEqual(units.MI, units.current())
            controller.return_value.config = None
            self.assertEqual(units.KM, units.current())

    def test_config_length_unit_default_and_read(self):
        conf = ConfigRepository.config_file_to_dto(ConfigRepository.get_default_config())
        self.assertEqual("km", conf.General.length_unit)
        with patch.object(ConfigRepository, "_read_file", return_value="[General]\nlength unit = mi\n"
                                                                       "[Electricity config]\n"):
            self.assertEqual(units.MI, units.normalize(ConfigRepository.read_config("x").General.length_unit))


class TestStatusTextUnits(unittest.TestCase):
    def test_format_distance(self):
        self.assertEqual("12\u202f345 km", status_text.format_distance(12345))
        self.assertEqual("7\u202f671 mi", status_text.format_distance(12345, "mi"))
        self.assertEqual("–", status_text.format_distance(None, "mi"))

    def test_speed(self):
        self.assertEqual("Driving · 80 km/h", status_text.car_state_text(None, True, 80))
        self.assertEqual("Driving · 50 mph", status_text.car_state_text(None, True, 80.4672, "mi"))

    def test_maintenance(self):
        self.assertEqual("16\u202f093 km · 30 days", service_text({"mileage": 16093, "days": 30}))
        self.assertEqual("10\u202f000 mi · 30 days",
                         service_text({"mileage": 16093.44, "days": 30}, "mi"))
        self.assertEqual("1\u202f000 mi overdue", service_text({"mileage": -1609.344}, "mi"))


class TestPagesUnits(unittest.TestCase):
    def tearDown(self):
        figures.UNIT = units.KM
        _view("insights").set_trips_provider(list)

    def test_insights_totals_in_miles(self):
        insights = _view("insights")
        insights.set_trips_provider(lambda: [dict(TRIP, start_at="2026-10-01T10:00:00+00:00")])
        trips = insights._trips("mi")  # pylint: disable=protected-access
        totals = insights.insights_data.trip_totals(trips)
        self.assertAlmostEqual(62.137, totals["distance"], places=3)
        # 16 kWh over 62.1 mi
        self.assertAlmostEqual(25.75, totals["consumption"], places=2)
        metric = insights._trips()  # pylint: disable=protected-access
        self.assertAlmostEqual(16.0, insights.insights_data.trip_totals(metric)["consumption"])

    def test_classic_figures_labels(self):
        car = Car("VR3UHZKX", "vid", "Peugeot")
        figures.UNIT = units.MI
        figures.get_figures(car)
        columns = {col["id"]: col for col in figures.table_fig.columns}
        self.assertEqual(" mi", _suffix(columns["distance"]))
        self.assertEqual(" mph", _suffix(columns["speed_average"]))
        self.assertEqual(" kWh/100mi", _suffix(columns["consumption_km"]))
        self.assertEqual("mileage (mi)", columns["mileage"]["name"])
        self.assertEqual("Consumption kWh/100mi", figures.consumption_fig.layout.yaxis.title.text)
        self.assertEqual("average Speed mph", figures.consumption_fig_by_speed.layout.xaxis.title.text)
        with patch.object(figures, "dash_app"):  # only for the icon urls
            cards = figures.get_summary_cards()
        self.assertIn("g/mi", str(cards["Average emission"]["text"][0]))
        figures.UNIT = units.KM
        figures.get_figures(car)
        columns = {col["id"]: col for col in figures.table_fig.columns}
        self.assertEqual(" km", _suffix(columns["distance"]))
        # the duration suffix must not leak into the distance column (shared Format object)
        self.assertEqual(" min", _suffix(columns["duration"]))

    def test_mileage_is_no_money(self):
        car = Car("VR3UHZKX", "vid", "Peugeot")
        for unit in (units.KM, units.MI):
            figures.UNIT = unit
            figures.get_figures(car)
            for table in (figures.table_fig, figures.battery_table):
                columns = {col["id"]: col for col in table.columns}
                mileage = columns["mileage"]["format"].to_plotly_json()
                self.assertEqual(["", f" {unit}"], mileage["locale"]["symbol"])
                self.assertEqual("$,.0f", mileage["specifier"])
                self.assertEqual(f"mileage ({unit})", columns["mileage"]["name"])

    def test_levels_are_percents(self):
        figures.get_figures(Car("VR3UHZKX", "vid", "Peugeot"))
        columns = {col["id"]: col for col in figures.battery_table.columns}
        for level in ("start_level", "end_level"):
            self.assertEqual(" %", _suffix(columns[level]))
        self.assertEqual("$,.0f", columns["end_level"]["format"].to_plotly_json()["specifier"])

    def test_wide_tables_scroll_inside_not_the_page(self):
        figures.get_figures(Car("VR3UHZKX", "vid", "Peugeot"))
        for table in (figures.table_fig, figures.battery_table):
            self.assertEqual("auto", table.style_table["overflowX"], table.id)

    def test_no_table_column_falls_back_to_the_dollar_symbol(self):
        figures.get_figures(Car("VR3UHZKX", "vid", "Peugeot"))
        for table in (figures.table_fig, figures.battery_table):
            for column in table.columns:
                if "format" in column:
                    # the "$" of a specifier is the locale symbol, without one the table shows a dollar
                    fmt = column["format"].to_plotly_json()
                    self.assertIn("symbol", fmt["locale"], f"{table.id}: {column['id']}")
                    self.assertEqual("", fmt["locale"]["symbol"][0], f"{table.id}: {column['id']}")

    def test_price_column_uses_the_configured_currency(self):
        views = _view("views")
        car = Car("VR3UHZKX", "vid", "Peugeot")
        trip = MagicMock(car=car, start_at=datetime(2026, 10, 1, tzinfo=timezone.utc))
        general = SimpleNamespace(currency="CHF", export_format="xlsx")
        fake_app = SimpleNamespace(is_good=True, myp=SimpleNamespace(vehicles_list=[car]),
                                   config=SimpleNamespace(General=general))
        # as after a login at runtime: the figures are built before any layout was requested
        with patch.object(views, "APP", fake_app), patch.object(views, "get_default_car", lambda: car), \
                patch.object(views.Trips, "get_trips", lambda _: {car.vin: [trip]}), \
                patch.object(views.Charging, "get_chargings", list), \
                patch.object(figures, "CURRENCY", "€"), patch.object(figures, "EXPORT_FORMAT", "csv"), \
                patch.multiple(views, create=True, trips=None, chargings=None, cached_layout=None, min_date=None,
                               max_date=None, min_millis=None, max_millis=None, step=None, marks=None,
                               cached_unit=views.cached_unit):
            views._load_trips()  # pylint: disable=protected-access
            columns = {col["id"]: col for col in figures.battery_table.columns}
            self.assertEqual(" CHF", _suffix(columns["price"]))
            self.assertEqual("xlsx", figures.battery_table.export_format)


class TestSettingsUnits(unittest.TestCase):
    def test_set_length_unit_saves_config(self):
        settings = _view("settings")
        config = MagicMock()
        config.General.length_unit = "km"
        with patch.object(settings, "app") as app:
            app.return_value.config = config
            self.assertTrue(settings.set_length_unit("miles"))
            self.assertEqual("mi", config.General.length_unit)
            config.write_config.assert_called_once()
            config.write_config.side_effect = OSError("read only")
            self.assertFalse(settings.set_length_unit("km"))
            self.assertEqual("mi", config.General.length_unit)  # rolled back


if __name__ == '__main__':
    unittest.main()
