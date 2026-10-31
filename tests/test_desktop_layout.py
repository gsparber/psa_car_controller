"""From 992 px the mobile pages use the width of the screen. The browser decides how that looks, so this only checks what
keeps phones as they were and the order of the pages: the wide rules stay behind their media query, and the wrappers
for the columns don't move anything in the page."""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from psa_car_controller.web.view import ui

WEB = Path(__file__).parent.parent / "psa_car_controller" / "web"
CSS = (WEB / "assets" / "10_mobile.css").read_text()
BIG_SCREEN = "@media (min-width: 992px) {"
# Used only by the wide layout: outside of the media query they would change the phone layout
WIDE_ONLY = (".page-columns", ".page-col", ".home-status", ".program-grid", ".insights-content", ".recent-trips")


def split_big_screen_block(css):
    """(the block of BIG_SCREEN, the rest of the file)"""
    start = css.index(BIG_SCREEN)
    depth = 0
    for position in range(start, len(css)):
        if css[position] == "{":
            depth += 1
        elif css[position] == "}":
            depth -= 1
            if depth == 0:
                return css[start:position + 1], css[:start] + css[position + 1:]
    raise AssertionError("unbalanced block")


class TestWideRulesStayWide(unittest.TestCase):

    def test_page_is_phone_wide_outside_of_the_block(self):
        block, rest = split_big_screen_block(CSS)
        self.assertIn("max-width: 34rem", re.search(r"\.page \{[^}]*\}", rest).group())
        self.assertIn("max-width: 90rem", re.search(r"\.page \{[^}]*\}", block).group())

    def test_layout_classes_have_no_rules_below_992_px(self):
        _, rest = split_big_screen_block(CSS)
        rest = re.sub(r"/\*.*?\*/", "", rest, flags=re.S)
        rest = re.sub(r"@media \(min-width: 1200px\) \{.*?\}\s*\}", "", rest, flags=re.S)
        for name in WIDE_ONLY:
            with self.subTest(name=name):
                self.assertNotIn(name, rest)


class TestColumns(unittest.TestCase):

    def test_left_comes_first(self):
        """The order on a phone and for the tab key."""
        columns = ui.columns(["a", "b"], ["c"])
        self.assertEqual("page-columns", columns.className)
        self.assertEqual([["a", "b"], ["c"]], [column.children for column in columns.children])
        self.assertEqual({"page-col"}, {column.className for column in columns.children})

    def test_extra_class(self):
        self.assertEqual("page-columns wide", ui.columns([], [], class_name="wide").className)

    def test_section_class(self):
        self.assertEqual("page-section", ui.section("Title", []).className)
        self.assertEqual("page-section recent", ui.section("Title", [], class_name="recent").className)


@unittest.skipUnless(shutil.which("node"), "needs node")
class TestCurrentTab(unittest.TestCase):

    def test_only_the_active_tab_is_marked(self):
        script = """
            const vm = require('vm'), fs = require('fs');
            globalThis.window = {console};
            vm.runInThisContext(fs.readFileSync(process.argv[1], 'utf8'));
            const link = active => ({
                attributes: {'aria-current': 'stale'},
                classList: {contains: name => active && name === 'active'},
                setAttribute (name, value) { this.attributes[name] = value },
                removeAttribute (name) { delete this.attributes[name] }
            });
            const links = [link(true), link(false)];
            markCurrentTab({querySelectorAll: () => links});
            console.log(JSON.stringify(links.map(l => l.attributes)));
        """
        result = subprocess.run(["node", "-e", script, str(WEB / "assets" / "clientside.js")], capture_output=True,
                                text=True, check=True, timeout=30)
        self.assertEqual([{"aria-current": "page"}, {}], json.loads(result.stdout))


class TestAssetsReachInstalledApps(unittest.TestCase):

    def test_service_worker_cache_is_new(self):
        """The old cache may hold the page answers with the fixed colors of the tables, always light."""
        version = re.search(r"const CACHE = 'psacc-v(\d+)'", (WEB / "static" / "sw.js").read_text())
        self.assertGreaterEqual(int(version.group(1)), 3)
