"""Dark theme on all pages. What it looks like is measured in a browser, this keeps what can break without anyone seeing
it: the colors of the tables reach AA in both themes, nothing on the server is fixed to one theme, charts change theme."""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from psa_car_controller.web import app as web_app

WEB = Path(__file__).parent.parent / "psa_car_controller" / "web"
CSS = (WEB / "assets" / "10_mobile.css").read_text()
FIGURES = (WEB / "figures.py").read_text()
OVERRIDES = (WEB / "assets" / "99_custom_overides.css").read_text()
AA_TEXT = 4.5
AA_LARGE_OR_CONTROL = 3


def tokens(selector):
    block = re.search(rf"^{re.escape(selector)} {{\n(.*?)^}}", CSS, re.S | re.M).group(1)
    return dict(re.findall(r"^\s*(--[\w-]+): ([^;]+);", block, re.M))


LIGHT = tokens(":root")
DARK = tokens('[data-bs-theme="dark"]')
NAMED = {"ivory": "#fffff0", "lightgrey": "#d3d3d3"}


def rgba(color):
    color = NAMED.get(color, color)
    if color.startswith("rgba"):
        red, green, blue, alpha = re.findall(r"[\d.]+", color)
        return int(red), int(green), int(blue), float(alpha)
    digits = color[1:]
    if len(digits) in (3, 4):
        digits = "".join(digit * 2 for digit in digits)
    values = [int(digits[i:i + 2], 16) for i in range(0, len(digits), 2)]
    return values[0], values[1], values[2], values[3] / 255 if len(values) == 4 else 1


def over(top, below):
    alpha = top[3]
    return tuple(top[i] * alpha + below[i] * (1 - alpha) for i in range(3)) + (1,)


def luminance(color):
    def channel(value):
        value /= 255
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4
    return 0.2126 * channel(color[0]) + 0.7152 * channel(color[1]) + 0.0722 * channel(color[2])


def contrast(first, second):
    high, low = sorted((luminance(first), luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


class TestTableColors(unittest.TestCase):
    TEXT = ("--table-muted", "--table-strong", "--table-low", "--table-high", "--table-price", "--table-text")

    def backgrounds(self, theme):
        plain = rgba(theme["--table-bg"])
        quick = rgba(theme["--table-quick"])
        hover = rgba(theme["--table-hover"])
        selected = rgba(theme["--table-selected"])
        cells = {"cell": plain, "quick": quick, "hover": over(hover, plain), "quick hover": over(hover, quick)}
        # a selected cell has the color of the selection on the row, with the hover of the row over it
        cells["selected"] = over(selected, plain)
        cells["selected hover"] = over(hover, cells["selected"])
        return cells

    def test_text_reaches_aa_on_every_state_of_a_row(self):
        for name, theme in (("light", LIGHT), ("dark", DARK)):
            for state, background in self.backgrounds(theme).items():
                for token in self.TEXT:
                    with self.subTest(theme=name, state=state, token=token):
                        ratio = contrast(rgba(theme[token]), background)
                        self.assertGreaterEqual(ratio, AA_TEXT)

    def test_sort_arrows_reach_aa_for_controls(self):
        for theme in (LIGHT, DARK):
            ratio = contrast(rgba(theme["--table-sort"]), rgba(theme["--table-header-bg"]))
            self.assertGreaterEqual(ratio, AA_LARGE_OR_CONTROL)

    def test_tables_have_no_color_of_one_theme(self):
        """A color in the figure is an inline style: only a variable can follow the theme."""
        found = re.findall(r"'(?:color|backgroundColor)': '([^']+)'", FIGURES)
        self.assertTrue(found)
        for color in found:
            self.assertRegex(color, r"^var\(--table-[\w-]+\)$")
            name = color[4:-1]
            self.assertIn(name, LIGHT)
            self.assertIn(name, DARK)

    def test_every_table_token_has_a_dark_value(self):
        self.assertEqual({key for key in LIGHT if key.startswith("--table-")},
                         {key for key in DARK if key.startswith("--table-")})

    def test_overrides_win_over_the_styles_of_the_table(self):
        """dash-table adds its styles later, with the same selectors: without the id they would win."""
        for selector in ("table", "td", "th"):
            self.assertIn(f"#react-entry-point .dash-table-container .dash-spreadsheet-container "
                          f".dash-spreadsheet-inner {selector} {{", OVERRIDES)


class TestControlsAndText(unittest.TestCase):
    def test_field_borders_reach_aa_for_controls(self):
        for theme, surfaces in ((LIGHT, ("#fff", "#f2f2f7")), (DARK, ("#1c1c1e", "#000"))):
            for surface in surfaces:
                self.assertGreaterEqual(contrast(rgba(theme["--app-control-border"]), rgba(surface)),
                                        AA_LARGE_OR_CONTROL)

    def test_links_reach_aa_on_page_and_card(self):
        for theme, surfaces in ((LIGHT, ("#fff", "#f2f2f7")), (DARK, ("#1c1c1e", "#000"))):
            for surface in surfaces:
                self.assertGreaterEqual(contrast(rgba(theme["--bs-link-color"]), rgba(surface)), AA_TEXT)

    def test_hover_label_of_mini_chart_keeps_its_own_color(self):
        self.assertIn(".mini-chart .main-svg text:not(.hoverlayer text) {", CSS)


class TestThemeScript(unittest.TestCase):
    def test_no_page_is_left_out(self):
        self.assertNotIn("/v1/", web_app.THEME_SCRIPT)
        self.assertNotIn("pathname", web_app.THEME_SCRIPT)
        self.assertIn("dark.matches ? 'dark' : 'light'", web_app.THEME_SCRIPT)
        self.assertIn("dark.addEventListener('change', apply)", web_app.THEME_SCRIPT)


class TestServerCharts(unittest.TestCase):
    def test_all_charts_of_the_stats_page_are_readable(self):
        from psa_car_controller.web import figures  # pylint: disable=import-outside-toplevel
        car = MagicMock()
        car.is_electric.return_value = True
        car.is_thermal.return_value = False
        figures.get_figures(car)
        for figure in (figures.consumption_fig, figures.consumption_fig_by_speed, figures.consumption_fig_by_temp,
                       figures.trips_map):
            layout = figure.layout
            self.assertEqual("#000", layout.hoverlabel.font.color)
            self.assertEqual("#808080", layout.modebar.color)

    def test_toolbar_color_holds_in_both_themes(self):
        from psa_car_controller.web.figures import READABLE_CHART  # pylint: disable=import-outside-toplevel
        for surface in ("#fff", "#1c1c1e"):
            for color in READABLE_CHART["modebar"].values():
                self.assertGreaterEqual(contrast(rgba(color), rgba(surface)), AA_LARGE_OR_CONTROL)


@unittest.skipUnless(shutil.which("node"), "needs node")
class TestChartTheme(unittest.TestCase):
    """Runs clientside.js with a page of its own: the theme, the charts on the page and the Plotly of dcc.Graph."""
    PAGE = """
        const vm = require('vm'), fs = require('fs');
        let theme = process.argv[2];
        const listeners = {};
        const charts = [];
        globalThis.MutationObserver = class { observe () {} };
        globalThis.document = {
            documentElement: {getAttribute: () => theme},
            addEventListener: () => {},
            querySelectorAll: () => charts,
            getElementsByClassName: () => []
        };
        const calls = [];
        globalThis.window = {
            console,
            matchMedia: () => ({addEventListener: (name, listener) => { listeners[name] = listener }}),
            Plotly: {
                react (element, figure) { calls.push(figure); return 'drawn' },
                relayout (element, update) { calls.push(update); return 'relaid' }
            }
        };
        globalThis.TEMPLATE = {
            data: {bar: [{marker: {line: {color: '#E5ECF6', width: 0.5}, pattern: {size: 10}}}], scatter: [{a: 1}]},
            layout: {colorway: ['#636efa'], paper_bgcolor: 'white', plot_bgcolor: '#E5ECF6',
                     font: {color: '#2a3f5f'}, xaxis: {automargin: true, gridcolor: 'white'}, yaxis: {ticks: ''}}
        };
        vm.runInThisContext(fs.readFileSync(process.argv[1], 'utf8'));
    """

    def run_page(self, code, theme="dark"):
        script = self.PAGE + code
        result = subprocess.run(["node", "-e", script, str(WEB / "assets" / "clientside.js"), theme],
                                capture_output=True, text=True, check=True, timeout=30)
        return json.loads(result.stdout)

    def test_dark_template_has_the_colors_and_keeps_the_rest(self):
        layout = self.run_page("console.log(JSON.stringify(darkChartTemplate(TEMPLATE)))")
        self.assertEqual("#1c1c1e", layout["layout"]["paper_bgcolor"])
        self.assertEqual("#1c1c1e", layout["layout"]["plot_bgcolor"])
        self.assertEqual("#dee2e6", layout["layout"]["font"]["color"])
        self.assertEqual("#48484a", layout["layout"]["xaxis"]["gridcolor"])
        self.assertEqual("#48484a", layout["layout"]["yaxis"]["zerolinecolor"])
        self.assertEqual(["#636efa"], layout["layout"]["colorway"])
        self.assertTrue(layout["layout"]["xaxis"]["automargin"])
        self.assertEqual({"a": 1}, layout["data"]["scatter"][0])
        self.assertEqual({"color": "#1c1c1e", "width": 0.5}, layout["data"]["bar"][0]["marker"]["line"])
        self.assertEqual({"size": 10}, layout["data"]["bar"][0]["marker"]["pattern"])

    def test_dark_colors_are_the_tokens_of_the_css(self):
        colors = self.run_page("console.log(JSON.stringify(DARK_CHART))")
        self.assertEqual(DARK["--app-card"], colors["background"])
        self.assertEqual(DARK["--table-border"], colors["line"])
        self.assertGreaterEqual(contrast(rgba(colors["text"]), rgba(colors["background"])), AA_TEXT)

    def test_figure_without_a_template_works(self):
        layout = self.run_page("console.log(JSON.stringify(darkChartTemplate(undefined)))")
        self.assertEqual("#1c1c1e", layout["layout"]["paper_bgcolor"])

    def test_light_figure_is_not_touched(self):
        same = self.run_page("""
            const figure = {data: [], layout: {template: TEMPLATE}};
            console.log(JSON.stringify(themedFigure(figure) === figure));
        """, "light")
        self.assertTrue(same)

    def test_dark_figure_leaves_the_figure_of_dash_alone(self):
        result = self.run_page("""
            const figure = {data: [1], layout: {template: TEMPLATE, title: 'x'}, config: {a: 1}};
            const themed = themedFigure(figure);
            console.log(JSON.stringify({old: figure.layout.template.layout.paper_bgcolor,
                                        paper: themed.layout.template.layout.paper_bgcolor,
                                        title: themed.layout.title, data: themed.data, config: themed.config}));
        """)
        self.assertEqual("white", result["old"])
        self.assertEqual("#1c1c1e", result["paper"])
        self.assertEqual("x", result["title"])
        self.assertEqual([1], result["data"])
        self.assertEqual({"a": 1}, result["config"])

    def test_every_drawing_gets_the_theme_and_only_once_wrapped(self):
        result = self.run_page("""
            // clientside.js has already done it, Plotly was there when it loaded
            const react = window.Plotly.react;
            themeChartsOfPlotly();
            const answer = window.Plotly.react({}, {data: [], layout: {template: TEMPLATE}});
            console.log(JSON.stringify({answer, calls: calls.length,
                                        paper: calls[0].layout.template.layout.paper_bgcolor,
                                        once: window.Plotly.react === react}));
        """)
        self.assertEqual("drawn", result["answer"])
        self.assertEqual(1, result["calls"])
        self.assertEqual("#1c1c1e", result["paper"])
        self.assertTrue(result["once"])

    def test_plotly_not_there_yet_is_no_error(self):
        result = self.run_page("""
            delete window.Plotly;
            themeChartsOfPlotly();
            console.log(JSON.stringify('fine'));
        """)
        self.assertEqual("fine", result)

    def test_change_of_the_system_theme_goes_both_ways(self):
        result = self.run_page("""
            themeChartsOfPlotly();
            const element = {};
            const unknown = {};
            charts.push(element, unknown);
            theme = 'light';
            window.Plotly.react(element, {data: [], layout: {template: TEMPLATE}});
            calls.length = 0;
            theme = 'dark';
            listeners.change();
            const toDark = calls[0].template.layout.paper_bgcolor;
            theme = 'light';
            listeners.change();
            console.log(JSON.stringify({toDark, count: calls.length, same: calls[1].template === TEMPLATE,
                                        keys: Object.keys(calls[1])}));
        """)
        self.assertEqual("#1c1c1e", result["toDark"])
        # the chart not yet drawn by dcc.Graph is left alone
        self.assertEqual(2, result["count"])
        self.assertTrue(result["same"])
        # nothing else: zoom and map view stay
        self.assertEqual(["template"], result["keys"])


if __name__ == "__main__":
    unittest.main()
