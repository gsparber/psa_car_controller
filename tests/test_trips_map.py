"""The map of /v1/stats shows the last known position of the car, and nothing where it knows none."""
import json
import shutil
import subprocess
import unittest
from unittest.mock import patch

from tests.test_browser_time import CLIENTSIDE, TestClientside as Clientside
from tests.test_stats_page_load import StatsTest, callbacks, ids_in

HINT = "No trip with a position to show"


def trip(start_at, lat, long):
    return {"start_at": start_at, "start_at_str": str(start_at), "positions": {"lat": lat, "long": long}}


def rounded(center):
    return {key: round(value, 6) for key, value in center.items()}


def update_map(figure, *rounds, moved=None):
    """The figure after updateMap got each list of trips in turn, as when the slider moves.
    moved: the view the user gave the map."""
    return Clientside.run_js(
        f"(() => {{ let figure = {figure};"
        "const log = console.log; console.log = () => {};"  # run_js reads the result from there
        f"for (const trips of {json.dumps(rounds)}) {{"
        "figure = updateMap({trips}, [figure], ['lat'], [['long', 'start_at_str']], getLastPosition(trips),"
        f"[{json.dumps(moved)}])[0] }}"
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


TILE = "https://tile.openstreetmap.org/12/2028/1425.png"
# what a dcc.Graph says about a map the user moved
RELAYOUT = {"map.center": {"lat": 48, "lon": 2}, "map.zoom": 7.5, "map.bearing": 20, "map.pitch": 30,
            "map._derived": {}}
USER_VIEW = {"center": {"lat": 48, "lon": 2}, "zoom": 7.5, "bearing": 20, "pitch": 30}


def fetch_tiles(*answers, url=TILE):
    """What fetchTile gives the map and what the hint says, the answers of the server are given one after the other.
    A number is the status of an answer, 200 an image; "text" an answer that is no image, "gone" and "abort" throw."""
    return Clientside.run_js(
        f"(async () => {{ const answers = {json.dumps(answers)};"
        "const element = {hidden: true, textContent: ''};"
        "globalThis.document = {getElementById: id => id === 'map-tiles-hint' ? element : null};"
        "const load = async () => { const answer = answers.shift();"
        "  if (answer === 'gone') throw new TypeError('Failed to fetch');"
        "  if (answer === 'abort') throw new DOMException('Aborted', 'AbortError');"
        "  return new Response('tile', {status: answer === 'text' ? 200 : answer, "
        "    headers: {'Content-Type': answer === 200 ? 'image/png' : 'text/plain'}}) };"
        "const fetched = fetchTile(load); const res = [];"
        f"while (answers.length) {{ try {{ const answer = await fetched(new Request({json.dumps(url)}));"
        "  res.push([answer.status, answer.headers.get('Content-Type'), (await answer.arrayBuffer()).byteLength]) }"
        "  catch (error) { res.push(error.name) } }"
        "return {res, hidden: element.hidden, text: element.textContent} })()",
        "UTC")


def map_errors(*reasons, then_tile=False):
    """The hint after these rejections of promises reached the page, and a good tile after them if asked for."""
    return Clientside.run_js(
        "(async () => { const element = {hidden: true, textContent: ''};"
        "globalThis.document = {getElementById: id => id === 'map-tiles-hint' ? element : null};"
        f"{json.dumps(list(reasons))}.forEach(message => noteMapError({{reason: message && new Error(message)}}));"
        f"if ({json.dumps(then_tile)}) await fetchTile(async () => new Response('tile', "
        "{headers: {'Content-Type': 'image/png'}}))(new Request('https://tile.openstreetmap.org/12/2028/1425.png'));"
        "return {hidden: element.hidden, text: element.textContent} })()",
        "UTC")


@unittest.skipUnless(shutil.which("node"), "needs node")
class TestMapStopped(unittest.TestCase):
    """fetchTile can't catch everything, and it catches nothing should the map no more use fetch: then it is said."""

    def test_page_listens_for_it(self):
        script = (
            f"const heard = []; globalThis.window = {{console, addEventListener: (type, run) => heard.push([type, run.name])}};"
            f"require('vm').runInThisContext(require('fs').readFileSync({json.dumps(str(CLIENTSIDE))}, 'utf8'));"
            "console.log(JSON.stringify(heard))")
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True, timeout=30)
        self.assertIn(["unhandledrejection", "noteMapError"], json.loads(result.stdout))

    def test_map_error_is_shown(self):
        result = map_errors("Map error.")
        self.assertFalse(result["hidden"])
        self.assertIn("The map stopped working", result["text"])
        self.assertIn("Reload the page", result["text"])

    def test_other_failures_are_not_about_the_map(self):
        self.assertTrue(map_errors("Failed to fetch", None)["hidden"])

    def test_good_tile_does_not_take_it_back(self):
        """Plotly stays stopped, tiles or not."""
        result = map_errors("Map error.", then_tile=True)
        self.assertFalse(result["hidden"])
        self.assertIn("The map stopped working", result["text"])


@unittest.skipUnless(shutil.which("node"), "needs node")
class TestFetchTile(unittest.TestCase):
    def assert_empty_tile(self, answer):
        self.assertEqual(200, answer[0])
        self.assertEqual("image/png", answer[1])
        self.assertGreater(answer[2], 4)  # a PNG of one pixel, not the body of the failed answer

    def test_tile(self):
        result = fetch_tiles(200)
        self.assertEqual([[200, "image/png", 4]], result["res"])
        self.assertTrue(result["hidden"])

    def test_refused_tile_is_no_error_of_the_map(self):
        """Any error fails the drawing of plotly for good, see fetchTile."""
        result = fetch_tiles(403)
        self.assert_empty_tile(result["res"][0])
        self.assertFalse(result["hidden"])
        self.assertIn("No map background", result["text"])
        self.assertIn("tile.openstreetmap.org answered with HTTP 403", result["text"])
        self.assertIn("Referer", result["text"])

    def test_no_connection(self):
        """The browser hides why: a refusal without CORS headers looks like no network."""
        result = fetch_tiles("gone")
        self.assert_empty_tile(result["res"][0])
        self.assertIn("could not be reached", result["text"])
        self.assertIn("Referer", result["text"])

    def test_answer_that_is_no_tile(self):
        """A captive portal answers 200 with a page, the map can't decode that."""
        result = fetch_tiles("text")
        self.assert_empty_tile(result["res"][0])
        self.assertIn("not a tile", result["text"])
        self.assertNotIn("Referer", result["text"])

    def test_server_error_does_not_blame_the_referer(self):
        result = fetch_tiles(503)
        self.assertIn("HTTP 503", result["text"])
        self.assertNotIn("Referer", result["text"])

    def test_other_tile_server_does_not_blame_the_referer(self):
        result = fetch_tiles(403, url="https://tiles.example.org/1/2/3.png")
        self.assertIn("tiles.example.org answered with HTTP 403", result["text"])
        self.assertNotIn("Referer", result["text"])

    def test_some_tiles_missing(self):
        result = fetch_tiles(200, 403, 200)
        self.assertEqual(3, len(result["res"]))
        self.assertFalse(result["hidden"])
        self.assertIn("Parts of the map background are missing", result["text"])

    def test_aborted_tile_stays_aborted(self):
        """The map cancels tiles it left behind, that is no failure."""
        result = fetch_tiles("abort")
        self.assertEqual(["AbortError"], result["res"])
        self.assertTrue(result["hidden"])

    def test_hint_leaves_when_the_tiles_are_back(self):
        self.assertFalse(fetch_tiles(403, *[200] * 31)["hidden"])
        self.assertTrue(fetch_tiles(403, *[200] * 32)["hidden"])

    def test_other_requests_are_left_alone(self):
        result = fetch_tiles(403, url="https://example.org/style.json")
        self.assertEqual([[403, "text/plain", 4]], result["res"])
        self.assertTrue(result["hidden"])


class TestTilesHintElement(StatsTest):
    def test_page_has_the_hidden_hint(self):
        self.assertIn("map-tiles-hint", ids_in(self.build()))


@unittest.skipUnless(shutil.which("node"), "needs node")
class TestUpdateMap(StatsTest):
    def setUp(self):
        super().setUp()
        self.views.figures.get_figures(self.car)
        self.figure = self.views.figures.trips_map.to_json()

    def assert_position(self, figure, lat, long):
        self.assertEqual(([lat], [long]), (figure["data"][1]["lat"], figure["data"][1]["lon"]))
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

    def test_one_place_is_not_zoomed_in_any_further(self):
        figure = update_map(self.figure, [trip(1, [47.1, 47.1], [-1.1, -1.1])])
        self.assertEqual({"lat": 47.1, "lon": -1.1}, rounded(figure["layout"]["map"]["center"]))
        self.assertEqual(12, figure["layout"]["map"]["zoom"])

    def test_view_fits_all_trips_of_the_range(self):
        """It was zoom 12 on the last position, the rest of the trips stayed outside."""
        near = update_map(self.figure, [trip(1, [47.1, 47.2], [-1.1, -1.2])])["layout"]["map"]
        far = update_map(self.figure, [trip(1, [47.1], [-1.1]), trip(2, [52.5], [13.4])])["layout"]["map"]
        self.assertLess(far["zoom"], near["zoom"])
        self.assertLess(near["zoom"], 12)
        self.assertEqual(6.15, round(far["center"]["lon"], 2))
        # halfway on the map is north of halfway in degrees, its scale grows to the north
        self.assertGreater(far["center"]["lat"], (47.1 + 52.5) / 2)
        self.assertLess(far["center"]["lat"], 52.5)
        # 800 x 540 pixels without a window: 14.5 degrees are 80% of 800 pixels, less than the height allows
        self.assertEqual(5.0, round(far["zoom"], 1))

    def test_fit_ignores_points_without_position(self):
        figure = update_map(self.figure, [trip(1, [47.1, None, 47.1], [-1.1, None, -1.1])])
        self.assertEqual({"lat": 47.1, "lon": -1.1}, rounded(figure["layout"]["map"]["center"]))

    def test_slider_keeps_the_view_of_the_user(self):
        """Every move of the slider put the map back on zoom 12."""
        map_layout = update_map(self.figure, [trip(1, [47.1], [-1.1])], [trip(2, [52.5], [13.4])], [],
                                [trip(1, [47.1], [-1.1])], moved=USER_VIEW)["layout"]["map"]
        self.assertEqual(({"lat": 48, "lon": 2}, 7.5, 20, 30), (map_layout["center"], map_layout["zoom"],
                                                                map_layout["bearing"], map_layout["pitch"]))

    def test_user_view_comes_from_relayout_data(self):
        self.assertEqual(USER_VIEW, Clientside.run_js(f"userMapView({json.dumps(RELAYOUT)})", "UTC"))

    def test_resize_is_no_user_view(self):
        """Switching to the tab of the map is a resize too, it must not replace the view the user gave it."""
        for relayout in ({"autosize": True}, None):
            self.assertEqual("skip", Clientside.run_js(
                f"dash_clientside = {{no_update: 'skip'}}; userMapView({json.dumps(relayout)})", "UTC"))

    def test_marker_and_hint_follow_with_the_view_of_the_user(self):
        figure = update_map(self.figure, [trip(1, [47.1], [-1.1])], [trip(2, [52.5], [13.4])], moved=USER_VIEW)
        self.assert_position(figure, 52.5, 13.4)
        self.assert_no_position(update_map(json.dumps(figure), [], moved=USER_VIEW))

    def test_sorting_a_table_keeps_the_view_of_the_user(self):
        """The sort gives the figure of the store, with the view of the last slider move."""
        figure = Clientside.run_js(
            f"(() => {{ const log = console.log; console.log = () => {{}};"
            f"dash_clientside = {{callback_context: {{triggered: [{{prop_id: 'trips-table.sort_by'}}]}}}};"
            f"const figures = {{graph: [], maps: [{self.figure}]}};"
            f"const res = filterAndSort({{trips: [], chargings: []}}, [0, 2e9], figures,"
            f"{{table_src: [], date_columns: {{}}}}, 20, {{}}, {{minimumLength: 0}}, [{json.dumps(USER_VIEW)}]);"
            "console.log = log; return res[0] })()", "UTC")
        self.assertEqual(7.5, figure["layout"]["map"]["zoom"])

    def test_layout_from_before_the_hint(self):
        """The service worker can keep the page of the last version."""
        old = {"data": [{}, {}], "layout": {"map": {"zoom": 12, "center": {"lat": 42, "lon": 1}}}}
        figure = update_map(json.dumps(old), [])
        self.assertEqual([None], figure["data"][1]["lat"])


@unittest.skipUnless(shutil.which("node"), "needs node")
class TestMapOnlyWithItsTabOpen(StatsTest):
    """A drawn map loads tiles even in a hidden tab: each visit of /v1/stats told the tile server where the trips are."""
    SKIP = "skip"

    def setUp(self):
        super().setUp()
        self.views.figures.get_figures(self.car)
        self.figure = self.views.figures.trips_map.to_json()

    def filter_and_sort(self, trigger, visible, trips=()):
        """What the callback gives its outputs: one table, one graph and the map. trigger: its Input, None on load."""
        triggered = [{"prop_id": trigger}] if trigger else []
        data = {"trips": list(trips), "chargings": []}
        return Clientside.run_js(
            "(() => { const log = console.log; console.log = () => {};"
            f"dash_clientside = {{no_update: {json.dumps(self.SKIP)}, "
            f"callback_context: {{triggered: {json.dumps(triggered)}}}}};"
            "globalThis.document = {getElementById: () => ({})};"  # the summary cards
            "const graph = {data: [{x: [], y: []}], layout: {}};"
            f"const figures = {{graph: [graph], maps: [{self.figure}]}};"
            "const table = {table_id: 't', src: 'trips'};"
            f"const res = filterAndSort({json.dumps(data)}, [0, 2e9], figures,"
            "{table_src: [table], date_columns: {trips: [], chargings: []},"
            "graph_x_label: ['start_at'], graph_y_label: [[]],"
            "map_x_label: ['lat'], map_y_label: [['long', 'start_at_str']]}, 20, {}, {minimumLength: 0}, [null],"
            f"{json.dumps(visible)});"
            "console.log = log; return res })()", "UTC")

    def test_page_load_on_another_tab_draws_no_map(self):
        table, graph, trips_map = self.filter_and_sort(None, False)
        self.assertEqual(self.SKIP, trips_map)
        self.assertEqual([], table)
        self.assertNotEqual(self.SKIP, graph)

    def test_slider_and_sort_on_another_tab_draw_no_map(self):
        for trigger in ("date-slider.value", "t.sort_by"):
            table, _, trips_map = self.filter_and_sort(trigger, False)
            self.assertEqual(self.SKIP, trips_map, trigger)
            self.assertEqual([], table, trigger)

    def test_opening_the_tab_draws_the_trips_the_slider_selects(self):
        trips = [dict(trip(1790841600, [47.1], [-1.1]), distance=10)]
        table, graph, trips_map = self.filter_and_sort("tabs.active_tab", True, trips)
        self.assertEqual([self.SKIP, self.SKIP], [table, graph])
        self.assertEqual([47.1, None], trips_map["data"][0]["lat"])
        self.assertEqual([47.1], trips_map["data"][1]["lat"])

    def test_leaving_the_tab_changes_nothing(self):
        self.assertEqual([self.SKIP] * 3, self.filter_and_sort("tabs.active_tab", False))

    def test_tab_open_on_load_draws_the_map(self):
        """The tabs keep the open one over a reload."""
        self.assertNotEqual(self.SKIP, self.filter_and_sort(None, True)[2])

    def test_slider_with_the_tab_open_draws_the_map(self):
        trips = [dict(trip(1790841600, [47.1], [-1.1]), distance=10)]
        self.assertEqual([47.1, None], self.filter_and_sort("date-slider.value", True, trips)[2]["data"][0]["lat"])

    def test_callback_knows_the_open_tab(self):
        views = self.views
        self.assertIn(views.STATS_TABS, ids_in(self.build()))
        spec = next(spec for spec in callbacks() if "trips-table.data" in spec["output"])
        self.assertIn({"id": views.STATS_TABS, "property": "active_tab"}, spec["inputs"])

    def test_map_starts_without_a_figure(self):
        """The figure waits in the store, a figure in the layout would be drawn and load tiles."""
        trips_map = find(self.build(), self.views.STATS_MAPS[0])
        self.assertFalse(getattr(trips_map, "figure", None))

    def test_map_is_in_the_tab_the_callback_watches(self):
        tabs = find(self.build(), self.views.STATS_TABS)
        tab = next(child for child in tabs.children if child.tab_id == self.views.STATS_MAP_TAB)
        self.assertIn(self.views.STATS_MAPS[0], ids_in(tab))


def find(component, component_id):
    if isinstance(component, (list, tuple)):
        return next((found for child in component if (found := find(child, component_id)) is not None), None)
    if getattr(component, "id", None) == component_id:
        return component
    children = getattr(component, "children", None)
    return None if children is None else find(children, component_id)
