"""Sorting the two tables of /v1/stats. filterAndSort writes the rows of both tables, whichever was clicked."""
import json
import shutil
import unittest

from tests.test_browser_time import TestClientside as Clientside
from tests.test_unclicked_callbacks import callback_of
from tests.utils import get_web_view

DAY = 86400000
START = 1790841600000
TABLES = [{"table_id": "trips-table", "src": "trips"}, {"table_id": "battery-table", "src": "chargings"}]


def filter_and_sort(clicked, sort):
    data = {"trips": [{"id": i + 1, "start_at": START + i * DAY} for i in range(3)],
            "chargings": [{"start_at": START + i * DAY, "price": i} for i in range(3)]}
    context = {"triggered": [{"prop_id": f"{clicked}.sort_by", "value": sort[clicked]}]}
    return Clientside.run_js(
        f"(() => {{ globalThis.dash_clientside = {{callback_context: {json.dumps(context)}}};"
        "const log = console.log; console.log = () => {};"  # run_js reads the result from there
        f"const res = filterAndSort({json.dumps(data)}, [0, 2e9], {{graph: [], maps: []}},"
        f"{{table_src: {json.dumps(TABLES)}}}, 10, {json.dumps(sort)}, {{minimumLength: 0}});"
        "console.log = log; return res })()", "UTC")


def column(direction, column_id="start_at_str"):
    return [{"column_id": column_id, "direction": direction}]


@unittest.skipUnless(shutil.which("node"), "needs node")
class TestSort(unittest.TestCase):

    def test_other_table_keeps_its_order(self):
        """Sorting the trips showed the charges in the order of the database, under the arrow for newest first."""
        trips, chargings = filter_and_sort("trips-table", {"trips-table": column("asc", "id"),
                                                           "battery-table": column("desc")})
        self.assertEqual([1, 2, 3], [row["id"] for row in trips])
        self.assertEqual([2, 1, 0], [row["price"] for row in chargings])

    def test_clicked_table_is_sorted(self):
        trips, chargings = filter_and_sort("battery-table", {"trips-table": column("desc", "id"),
                                                             "battery-table": column("asc")})
        self.assertEqual([3, 2, 1], [row["id"] for row in trips])
        self.assertEqual([0, 1, 2], [row["price"] for row in chargings])

    def sorted_column(self, rows, column_id, direction):
        table = json.dumps(TABLES)
        data = json.dumps({"chargings": rows})
        return Clientside.run_js(
            f"(() => {{ const data = {data};"
            f"sortDataset({{prop_id: 'battery-table.sort_by', value: {json.dumps(column(direction, column_id))}}},"
            f"data, {table}); return data.chargings.map(row => row[{json.dumps(column_id)}]) }})()", "UTC")

    def test_number_column_with_a_null_in_the_first_row(self):
        rows = [{"price": None}, {"price": 5}, {"price": 3}]
        self.assertEqual([None, 3, 5], self.sorted_column(rows, "price", "asc"))
        self.assertEqual([5, 3, None], self.sorted_column(rows, "price", "desc"))

    def test_text_column_with_a_null(self):
        rows = [{"charging_mode": "Slow"}, {"charging_mode": None}, {"charging_mode": "Fast"}]
        self.assertEqual([None, "Fast", "Slow"], self.sorted_column(rows, "charging_mode", "asc"))


@unittest.skipUnless(shutil.which("node"), "needs node")
class TestNoUnsortedState(unittest.TestCase):
    """The table cycles asc, desc, none. None is the order of the database, for ids and dates the same as asc:
    from desc two clicks lead to the same rows, the second one seemed to do nothing."""

    @staticmethod
    def steps(*sort_bys):
        calls = ", ".join(f"skipUnsorted('t', {json.dumps(sort_by)})" for sort_by in sort_bys)
        return Clientside.run_js(
            f"(() => {{ globalThis.dash_clientside = {{no_update: 'same'}}; return [{calls}] }})()", "UTC")

    def test_unsorted_becomes_ascending(self):
        self.assertEqual(["same", [{"column_id": "id", "direction": "asc"}]], self.steps(column("desc", "id"), []))

    def test_ascending_stays(self):
        self.assertEqual(["same", "same"], self.steps(column("asc", "id"), column("desc", "id")))

    def test_uses_the_last_column(self):
        self.assertEqual(["same", "same", [{"column_id": "b", "direction": "asc"}]],
                         self.steps(column("desc", "a"), column("desc", "b"), []))

    def test_nothing_to_go_back_to(self):
        self.assertEqual(["same"], self.steps([]))

    def test_tables_are_connected(self):
        get_web_view("views")
        for table in ("trips-table", "battery-table"):
            with self.subTest(table=table):
                callback = callback_of(f"{table}.sort_by")
                self.assertEqual([{"id": table, "property": "sort_by"}], callback["inputs"])
                self.assertIsNotNone(callback["clientside_function"])
