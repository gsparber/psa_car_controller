"""What the page takes from the code of Dash and plotly, read from the installed packages: no browser needed.

A new version fails test_versions_were_checked on purpose. See that the page works in a browser, German charts and a
map with refused tiles on /v1/stats, then add the version here. The other tests name what changed, should one fail."""
import json
import re
import unittest
from pathlib import Path

import dash
import plotly

ROOT = Path(__file__).parent.parent
ASSETS = ROOT / "psa_car_controller" / "web" / "assets"
# seen working in a browser: the versions of poetry.lock, used by CI, and the newest ones the image can install
CHECKED = {"dash": {"4.1.0", "4.2.0"}, "plotly": {"6.7.0", "6.9.0"}}


def package_file(package, *path):
    return Path(package.__file__).parent.joinpath(*path).read_text(encoding="utf-8")


class TestVersions(unittest.TestCase):
    def test_versions_were_checked(self):
        self.assertIn(dash.__version__, CHECKED["dash"], "see the top of this file")
        self.assertIn(plotly.__version__, CHECKED["plotly"], "see the top of this file")

    def test_lock_file_has_checked_versions(self):
        lock = (ROOT / "poetry.lock").read_text(encoding="utf-8")
        for name, versions in CHECKED.items():
            version = re.search(rf'name = "{name}"\nversion = "([^"]+)"', lock).group(1)
            self.assertIn(version, versions, name)

    def test_project_stays_on_the_major_versions(self):
        project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('dash = ">=4.0.0, <5.0.0"', project)
        self.assertIn('plotly = "^6.', project)


class TestDash(unittest.TestCase):
    """assets/00_language.js and RENDERER of web/app.py"""

    def test_graph_loads_plotly_with_a_script(self):
        """A listener on the document sees the script load before the graph does. A module wouldn't fire it: the
        charts would be in English without any error."""
        source = package_file(dash, "dcc", "dash_core_components.js")
        self.assertRegex(source, r'window\.Plotly\|\|new Promise\(.{0,120}createElement\("script"\)')
        self.assertRegex(source, r'createElement\("script"\).{0,120}\.onload=')

    def test_graph_draws_with_the_react_of_the_global(self):
        """themeChartsOfPlotly of clientside.js replaces it. If the graph stops using it, charts stay light in the dark
        theme: readable, but without any error."""
        self.assertRegex(package_file(dash, "dcc", "async-graph.js"), r"[^.\w]Plotly\.react\(")

    def test_default_renderer_is_what_gets_replaced(self):
        """The attribute is the documented way of Dash to change this script."""
        self.assertRegex(dash.Dash(__name__).renderer, r"^var renderer = new DashRenderer\(.*\);$")


class TestPlotly(unittest.TestCase):
    source = None

    @classmethod
    def setUpClass(cls):
        cls.source = package_file(plotly, "package_data", "plotly.min.js")

    def test_sets_its_global(self):
        self.assertIn("window.Plotly = Plotly", self.source)

    def test_has_the_default_for_all_charts(self):
        self.assertRegex(self.source, r"\.setPlotConfig=")

    def test_takes_the_language_files_loaded_before_it(self):
        self.assertIn("window.PlotlyLocales&&Array.isArray(window.PlotlyLocales)", self.source)

    def test_map_error_has_the_text_the_page_looks_for(self):
        clientside = (ASSETS / "clientside.js").read_text(encoding="utf-8")
        text = re.search(r"const MAP_ERROR = '([^']+)'", clientside).group(1)
        self.assertIn(f'mapOnErrorMsg:"{text}"', self.source)

    def test_map_fails_the_drawing_on_an_error(self):
        """Why fetchTile is there. Should it go away, fetchTile and its hints can go too."""
        self.assertRegex(self.source, r"rejectOnError=function")

    def test_map_loads_with_the_fetch_of_the_page(self):
        """fetchTile replaces window.fetch. MapLibre falls back to XMLHttpRequest, then a refused tile stops the map."""
        self.assertRegex(self.source, r'Object\.prototype\.hasOwnProperty\.call\(Request\.prototype,"signal"\)\)return')
        self.assertRegex(self.source, r"new Request\(W\.url.{0,700}\b(?:yield|await) fetch\(")

    def test_tile_of_our_style_is_seen_as_a_tile(self):
        clientside = (ASSETS / "clientside.js").read_text(encoding="utf-8")
        pattern = re.search(r"const MAP_TILE = /(.+)/\n", clientside).group(1).replace(r"\/", "/")
        style = json.loads((ASSETS / "style.json").read_text(encoding="utf-8"))
        tiles = [tile for source in style["sources"].values() for tile in source.get("tiles", [])]
        self.assertTrue(tiles)
        for tile in tiles:
            url = tile.replace("{z}", "12").replace("{x}", "2028").replace("{y}", "1425")
            self.assertRegex(url, pattern)
