"""The map of /v1/stats shows the last known position of the car, and nothing where it knows none."""
import json
import shutil
import unittest
from unittest.mock import patch

from tests.test_browser_time import TestClientside as Clientside
from tests.test_stats_page_load import StatsTest

HINT = "No trip with a position to show"


def trip(start_at, lat, long):
    return {"start_at": start_at, "start_at_str": str(start_at), "positions": {"lat": lat, "long": long}}


def update_map(figure, *rounds):
    """The figure after updateMap got each list of trips in turn, as when the slider moves."""
    return Clientside.run_js(
        f"(() => {{ let figure = {figure};"
        "const log = console.log; console.log = () => {};"  # run_js reads the result from there
        f"for (const trips of {json.dumps(rounds)}) {{"
        "figure = updateMap({trips}, [figure], ['lat'], [['long', 'start_at_str']], getLastPosition(trips))[0] }"
        "console.log = log; return figure })()", "UTC")


class TestServerFigure(StatsTest):
    def setUp(self):
        super().setUp()
        self.views.figures.get_figures(self.car)
        self.figure = json.loads(self.views.figures.trips_map.to_json())

    def test_no_made_up_position(self):
        """It had a marker in the hills of Catalonia, which looked like the car when no trip replaced it."""
        for trace in self.figure["data"]:
            self.assertEqual({None}, {*trace["lat"], *trace["lon"]})
        self.assertNotIn("center", self.figure["layout"]["map"])

    def test_every_trace_has_an_entry(self):
        """Plotly hides a trace without entries, and without a visible trace it draws axes instead of a map."""
        for trace in self.figure["data"]:
            self.assertEqual(1, len(trace["lat"]))

    def test_hint_waits_for_the_browser(self):
        hint = self.figure["layout"]["annotations"][0]
        self.assertEqual(HINT, hint["text"])
        self.assertFalse(hint["visible"])

    def test_trips_of_the_last_run_are_gone_with_the_positions(self):
        """get_trips has no entry for the car without positions: the old trips stayed until the restart."""
        views = self.views
        with patch.object(views.Trips, "get_trips", return_value={}), \
                patch.object(views.Charging, "get_chargings", return_value=[]), \
                patch.object(views, "trips", ["trip of the last run"]):
            views._load_trips()  # pylint: disable=protected-access
            self.assertEqual([], views.trips)


@unittest.skipUnless(shutil.which("node"), "needs node")
class TestUpdateMap(StatsTest):
    def setUp(self):
        super().setUp()
        self.views.figures.get_figures(self.car)
        self.figure = self.views.figures.trips_map.to_json()

    def assert_position(self, figure, lat, long):
        self.assertEqual(([lat], [long]), (figure["data"][1]["lat"], figure["data"][1]["lon"]))
        self.assertEqual({"lat": lat, "lon": long}, figure["layout"]["map"]["center"])
        self.assertFalse(figure["layout"]["annotations"][0]["visible"])

    def assert_no_position(self, figure):
        self.assertEqual(([None], [None]), (figure["data"][1]["lat"], figure["data"][1]["lon"]))
        self.assertTrue(figure["layout"]["annotations"][0]["visible"])

    def test_no_trip(self):
        figure = update_map(self.figure, [])
        self.assert_no_position(figure)
        self.assertEqual(([], []), (figure["data"][0]["lat"], figure["data"][0]["lon"]))
        self.assertNotIn("center", figure["layout"]["map"])
        self.assertEqual(1, figure["layout"]["map"]["zoom"])

    def test_one_trip(self):
        figure = update_map(self.figure, [trip(1, [47.1, 47.2], [-1.1, -1.2])])
        self.assert_position(figure, 47.2, -1.2)
        self.assertEqual(12, figure["layout"]["map"]["zoom"])
        self.assertEqual([47.1, 47.2, None], figure["data"][0]["lat"])

    def test_newest_trip_whatever_the_order(self):
        trips = [trip(2, [47.2], [-1.2]), trip(3, [47.3], [-1.3]), trip(1, [47.1], [-1.1])]
        self.assert_position(update_map(self.figure, trips), 47.3, -1.3)

    def test_end_of_the_trip_without_position(self):
        """It centered the map on 0/0, in the sea."""
        figure = update_map(self.figure, [trip(1, [47.1, 47.2, None], [-1.1, -1.2, None])])
        self.assert_position(figure, 47.2, -1.2)

    def test_newest_trip_without_position(self):
        trips = [trip(1, [47.1], [-1.1]), trip(2, [None, None], [None, None]), trip(3, [], [])]
        self.assert_position(update_map(self.figure, trips), 47.1, -1.1)

    def test_half_a_position_is_none(self):
        trips = [trip(1, [47.1, 47.2, None], [-1.1, None, -1.3])]
        self.assert_position(update_map(self.figure, trips), 47.1, -1.1)

    def test_only_trips_without_position(self):
        self.assert_no_position(update_map(self.figure, [trip(1, [None], [None])]))

    def test_range_without_trip_leaves_no_old_marker(self):
        figure = update_map(self.figure, [trip(1, [47.1], [-1.1])], [])
        self.assert_no_position(figure)
        self.assertEqual({"lat": 47.1, "lon": -1.1}, figure["layout"]["map"]["center"])

    def test_back_from_a_range_without_trip(self):
        figure = update_map(self.figure, [trip(1, [47.1], [-1.1])], [], [trip(1, [47.1], [-1.1])])
        self.assert_position(figure, 47.1, -1.1)

    def test_first_position_after_none(self):
        figure = update_map(self.figure, [], [trip(1, [47.1], [-1.1])])
        self.assert_position(figure, 47.1, -1.1)
        self.assertEqual(12, figure["layout"]["map"]["zoom"])

    def test_layout_from_before_the_hint(self):
        """The service worker can keep the page of the last version."""
        old = {"data": [{}, {}], "layout": {"map": {"zoom": 12, "center": {"lat": 42, "lon": 1}}}}
        figure = update_map(json.dumps(old), [])
        self.assertEqual([None], figure["data"][1]["lat"])
