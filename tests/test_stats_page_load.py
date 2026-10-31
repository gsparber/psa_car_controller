"""/v1/stats: the browser gets the callbacks when the app loads, and the page must answer without any trips loaded."""
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from psa_car_controller.psacc.model.car import Car
from psa_car_controller.web import app as web_app
from tests.utils import get_web_view


def callbacks():
    return web_app.dash_app._callback_list  # pylint: disable=protected-access


def ids_in(component):
    if isinstance(component, (list, tuple)):
        return [found for child in component for found in ids_in(child)]
    own = [component.id] if getattr(component, "id", None) else []
    children = getattr(component, "children", None)
    return own + ids_in([] if children is None else children)


def store(layout, store_id):
    return next(child.data for child in layout.children if getattr(child, "id", None) == store_id)


def run_with_limit(function, limit=5):
    """The old code never returned here: a thread that can't keep the tests from ending."""
    result = []
    thread = threading.Thread(target=lambda: result.append(function()), daemon=True)
    thread.start()
    thread.join(limit)
    return result


class StatsTest(unittest.TestCase):
    def setUp(self):
        self.views = get_web_view("views")
        self.car = Car("VIN-stats", "id", "Peugeot")
        self.general = SimpleNamespace(currency="CHF", export_format="csv", minimum_trip_length=7)
        self.app = SimpleNamespace(is_good=True, myp=SimpleNamespace(vehicles_list=[self.car]),
                                   config=SimpleNamespace(General=self.general))
        figures = self.views.figures
        for patcher in (patch.object(self.views, "APP", self.app),
                        # another test may have imported figures before the Dash app existed
                        patch.object(figures, "dash_app", web_app.dash_app),
                        patch.multiple(self.views, cached_layout=None, min_millis=1000, max_millis=2000, step=10,
                                       marks={}, create=False),
                        patch.object(self.views, "chargings", [], create=True),
                        patch.object(self.views, "trips", MagicMock(get_trips_as_dict=list)),
                        patch.multiple(figures, table_fig=figures.table_fig, battery_table=figures.battery_table,
                                       consumption_fig=figures.consumption_fig, trips_map=figures.trips_map,
                                       consumption_fig_by_speed=figures.consumption_fig_by_speed,
                                       consumption_fig_by_temp=figures.consumption_fig_by_temp)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def build(self):
        self.views.figures.get_figures(self.car)
        return self.views._cached_or_new_layout()  # pylint: disable=protected-access


class TestCallbacksExistBeforeThePage(StatsTest):
    def test_filter_callback_is_there_before_any_layout(self):
        outputs = [spec["output"] for spec in callbacks()]
        self.assertTrue(any("trips-table.data" in output and "battery-table.data" in output for output in outputs))
        # also the others of the page: the slider marks, the export buttons, the popups, the price of a charge
        for output in ("date-slider.marks", "loading-output-trips.children", "empty-div.children"):
            self.assertIn(output, outputs)
        self.assertTrue(any("tab_trips_popup_graph.children" in output for output in outputs))

    def test_export_animation_waits_for_a_click(self):
        """Each slept 3 s on every visit of the page. Leaving it meanwhile was a failed callback in WebKit."""
        for output, button in (("loading-output-trips.children", "export-trips-table"),
                               ("loading-output-battery.children", "export-battery-table")):
            spec = next(spec for spec in callbacks() if spec["output"] == output)
            self.assertTrue(spec["prevent_initial_call"], output)
            self.assertEqual([{"id": button, "property": "n_clicks"}], spec["inputs"])

    def test_building_the_page_adds_no_callback(self):
        before = len(callbacks())
        self.build()
        self.assertEqual(before, len(callbacks()))

    def test_page_gives_the_callback_what_it_fills(self):
        views = self.views
        layout = self.build()
        params = store(layout, "clientside-params-store")
        self.assertEqual(views.STATS_TABLES, [table["table_id"] for table in params["table_src"]])
        figure_store = store(layout, "clientside-figure-store")
        self.assertEqual((len(views.STATS_TABLES), len(views.STATS_GRAPHS), len(views.STATS_MAPS)),
                         (len(figure_store["tables"]), len(figure_store["graph"]), len(figure_store["maps"])))
        self.assertLessEqual({*views.STATS_TABLES, *views.STATS_GRAPHS, *views.STATS_MAPS, "date-slider"},
                             set(ids_in(layout)))
        self.assertEqual(["start_at", "end_at"], params["date_columns"]["trips"])

    def test_output_order_follows_the_filter(self):
        views = self.views
        spec = next(spec for spec in callbacks() if "trips-table.data" in spec["output"])
        expected = [f"{table}.data" for table in views.STATS_TABLES]
        expected += [f"{figure}.figure" for figure in views.STATS_GRAPHS + views.STATS_MAPS]
        self.assertEqual(".." + "...".join(expected) + "..", spec["output"])
        self.assertEqual("clientside-params-store", spec["state"][-1]["id"])

    def test_view_of_the_user_reaches_the_filter(self):
        layout = self.build()
        stores = [f"{map_id}-view-store" for map_id in self.views.STATS_MAPS]
        self.assertLessEqual(set(stores), set(ids_in(layout)))
        spec = next(spec for spec in callbacks() if "trips-table.data" in spec["output"])
        self.assertEqual([{"id": store_id, "property": "data"} for store_id in stores], spec["state"][:-1])
        for map_id, store_id in zip(self.views.STATS_MAPS, stores):
            spec = next(spec for spec in callbacks() if spec["output"] == f"{store_id}.data")
            self.assertEqual([{"id": map_id, "property": "relayoutData"}], spec["inputs"])

    def test_minimum_length_follows_the_config(self):
        self.assertEqual({"minimumLength": 7}, store(self.build(), "clientside-params-store")["config"])
        self.general.minimum_trip_length = 12
        self.views.cached_layout = None
        self.assertEqual({"minimumLength": 12}, store(self.build(), "clientside-params-store")["config"])

    def test_no_data_shows_only_the_hint(self):
        # the callbacks of the page are always known now: with their inputs missing the browser reports errors
        views = self.views
        views.min_millis = None
        self.assertIs(views.figures.ERROR_DIV, views._cached_or_new_layout())  # pylint: disable=protected-access

    def test_page_with_data_after_no_data(self):
        views = self.views
        views.min_millis = None
        views._cached_or_new_layout()  # pylint: disable=protected-access
        views.min_millis = 1000
        views.cached_layout = None
        self.assertIn("trips-table", ids_in(self.build()))


class TestWaitForTrips(StatsTest):
    def setUp(self):
        super().setUp()
        views = self.views
        for patcher in (patch.object(views, "DATA_READY", False),
                        patch.object(views.Database, "get_db"),
                        patch.object(views, "TRIPS_WAIT_STEP", 0.01),
                        patch.object(views, "_cached_or_new_layout", return_value="layout")):
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def join_loaders():
        for thread in threading.enumerate():
            if thread.name == "update_trips":
                thread.join(5)

    def test_first_page_after_a_web_login_loads_the_trips(self):
        views = self.views
        with patch.object(views.Database, "add_altitude_to_db"), patch.object(views, "_load_trips") as load:
            answer = run_with_limit(views.serve_layout)
            self.join_loaders()
        self.assertEqual(["layout"], answer)
        load.assert_called_once_with()
        self.assertTrue(views.DATA_READY)

    def test_page_answers_while_the_altitudes_take_long(self):
        views = self.views
        release = threading.Event()
        with patch.object(views.Database, "add_altitude_to_db", side_effect=lambda _: release.wait(5)), \
                patch.object(views, "_load_trips") as load, patch.object(views, "TRIPS_WAIT_STEPS", 3):
            try:
                answer = run_with_limit(views.serve_layout)
                self.assertEqual(1, len(answer))
                self.assertIn("still loading", answer[0].children)
                views._cached_or_new_layout.assert_not_called()  # pylint: disable=protected-access
                load.assert_not_called()
            finally:
                release.set()
                self.join_loaders()
        self.assertEqual(["layout"], run_with_limit(views.serve_layout))

    def test_requests_load_once(self):
        views = self.views
        release = threading.Event()
        started = []

        def slow(_):
            started.append(time.monotonic())
            release.wait(5)

        with patch.object(views.Database, "add_altitude_to_db", side_effect=slow), \
                patch.object(views, "_load_trips"), patch.object(views, "TRIPS_WAIT_STEPS", 2):
            try:
                run_with_limit(views.serve_layout)
                run_with_limit(views.serve_layout)
                self.assertEqual(1, len(started))
            finally:
                release.set()
                self.join_loaders()

    def test_no_login_doesnt_count_as_loaded(self):
        # the database can change before the login: the first page after the login has to load
        views = self.views
        self.app.is_good = False
        with patch.object(views.Database, "add_altitude_to_db"):
            views.update_trips()
        self.assertFalse(views.DATA_READY)
        self.app.is_good = True
        with patch.object(views.Database, "add_altitude_to_db"), patch.object(views, "_load_trips"):
            views.update_trips()
        self.assertTrue(views.DATA_READY)

    def test_failed_load_doesnt_keep_pages_waiting(self):
        views = self.views
        with patch.object(views.Database, "add_altitude_to_db"), \
                patch.object(views, "_load_trips", side_effect=ValueError("broken")):
            with self.assertRaises(ValueError):
                views.update_trips()
        self.assertTrue(views.DATA_READY)


if __name__ == "__main__":
    unittest.main()
