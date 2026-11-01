"""Numbers and percentages the server can't write itself, because only the browser knows the language."""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from dash import html

from psa_car_controller.web.view import ui
from tests.utils import get_web_view

WEB = Path(__file__).parent.parent / "psa_car_controller" / "web"
CLIENTSIDE = WEB / "assets" / "clientside.js"
LANGUAGE_JS = WEB / "assets" / "00_language.js"
OVERRIDES = WEB / "assets" / "99_custom_overides.css"
NBSP, NARROW_NBSP = " ", " "


def props(component):
    return component.to_plotly_json()["props"]


def walk(component):
    yield component
    children = getattr(component, "children", None)
    for child in children if isinstance(children, (list, tuple)) else [children]:
        if child is not None and not isinstance(child, str):
            yield from walk(child)


def run_js(expression, tag):
    script = (
        f"globalThis.window = globalThis; globalThis.document = {{querySelector: () => null, addEventListener: () => {{}}}};"
        f"const vm = require('vm'), fs = require('fs');"
        f"vm.runInThisContext(fs.readFileSync({json.dumps(str(LANGUAGE_JS))}, 'utf8'));"
        f"delete globalThis.document; window.psaccLanguage.set({json.dumps(tag)});"
        f"vm.runInThisContext(fs.readFileSync({json.dumps(str(CLIENTSIDE))}, 'utf8'));"
        f"console.log(JSON.stringify(eval({json.dumps(expression)})))")
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True, timeout=30)
    return json.loads(result.stdout)


class TestPercentOnTheServer(unittest.TestCase):
    def test_marked_for_the_browser(self):
        number = props(ui.percent(79.6))
        self.assertEqual(("local-number", "79.6", 0, "percent"),
                         (number["className"], number["value"], number["data-digits"], number["data-style"]))
        self.assertEqual("80 %", number["children"])

    def test_unknown(self):
        for value in (None, float("nan"), float("inf"), True, "12"):
            self.assertEqual("–", ui.percent(value))

    def test_plain_numbers_are_not_percents(self):
        self.assertNotIn("data-style", props(ui.fmt(80)))

    def test_no_page_writes_the_sign_itself(self):
        for name in ("home", "charge", "control", "insights"):
            source = (WEB / "view" / f"{name}.py").read_text(encoding="utf-8")
            self.assertEqual([], re.findall(r'''[}"'] ?%["']|, "%"''', source), name)

    def test_limit_on_the_charge_page_follows_the_slider(self):
        charge = get_web_view("charge")
        _, limit = charge.show_limit(True, 75)
        self.assertEqual("percent", props(limit)["data-style"])
        self.assertEqual("75", props(limit)["value"])


@unittest.skipUnless(shutil.which("node"), "needs node")
class TestPercentInTheBrowser(unittest.TestCase):
    def test_as_the_language_writes_it(self):
        self.assertEqual("80%", run_js("percentText(80, 0)", "en-US"))
        self.assertEqual("80%", run_js("percentText(80, 0)", "en-GB"))
        self.assertEqual(f"80{NBSP}%", run_js("percentText(80, 0)", "de-AT"))
        self.assertRegex(run_js("percentText(80, 0)", "fr-FR"), f"^80[{NBSP}{NARROW_NBSP}]%$")

    def test_sign_in_front(self):
        self.assertEqual("%80", run_js("percentText(80, 0)", "tr-TR"))
        self.assertEqual(["%", ""], run_js("percentAffixes()", "tr-TR"))

    def test_digits(self):
        self.assertEqual(f"94,5{NBSP}%", run_js("percentText(94.5, 1)", "de-AT"))

    def test_page_elements_take_it(self):
        page = """(() => {
          const element = { dataset: { digits: '0', style: 'percent' }, getAttribute: () => '80', textContent: '80 %' }
          localizeNumbers({ getElementsByClassName: () => [element], querySelectorAll: () => [] })
          return element.textContent
        })()"""
        self.assertEqual("80%", run_js(page, "en-US"))
        self.assertEqual(f"80{NBSP}%", run_js(page, "de-AT"))

    def test_symbol_of_the_table(self):
        self.assertEqual(["", "%"], run_js("tableLocale().symbol", "en-US"))
        self.assertEqual(["", f"{NBSP}%"], run_js("tableLocale().symbol", "de-AT"))
        self.assertRegex(run_js("tableLocale().symbol", "fr-FR")[1], f"^[{NBSP}{NARROW_NBSP}]%$")


class TestCommandLineFlags(unittest.TestCase):
    """At 390 px the hint broke inside the flag: "--remote-disable or -" and "-offline"."""

    def test_stay_on_one_line(self):
        code = ui.flag("--offline")
        self.assertIsInstance(code, html.Code)
        self.assertEqual("text-nowrap", code.className)

    def test_every_flag_of_the_hints(self):
        car_access = get_web_view("car_access")
        flags = [part for part in car_access.REMOTE_OFF_HINT if not isinstance(part, str)]
        self.assertEqual(["--remote-disable", "--offline"], [flag.children for flag in flags])
        for flag in flags:
            self.assertEqual("text-nowrap", flag.className)

    def test_no_page_writes_a_flag_in_plain_code(self):
        for name in ("home", "charge", "climate", "settings", "car_access"):
            source = (WEB / "view" / f"{name}.py").read_text(encoding="utf-8")
            self.assertNotIn('html.Code("-', source, name)


class TestTooltip(unittest.TestCase):
    def test_does_not_widen_the_page(self):
        css = OVERRIDES.read_text(encoding="utf-8")
        self.assertRegex(css, r"#react-entry-point:has\(\.dash-tooltip\)\s*\{\s*overflow-x: clip;")


class TestLabelOnTheServer(unittest.TestCase):
    def test_marks_the_numbers(self):
        label = ui.number_label("Battery {} percent, range {} km", 59, 1234.4)
        self.assertEqual("Battery {59} percent, range {1234} km", label["data-label"])
        self.assertEqual(f"Battery 59 percent, range 1{NARROW_NBSP}234 km", label["aria-label"])

    def test_unknown(self):
        label = ui.number_label("Battery {} percent, range {} km", None, float("nan"))
        self.assertEqual("Battery – percent, range – km", label["data-label"])
        self.assertEqual(label["data-label"], label["aria-label"])

    def test_home_ring_says_it_in_the_display_unit(self):
        props_of_ring = props(get_web_view("home")._soc_ring(59, 2000, "mi"))  # pylint: disable=protected-access
        self.assertEqual("Battery {59} percent, range {1243} mi", props_of_ring["data-label"])


@unittest.skipUnless(shutil.which("node"), "needs node")
class TestLabelInTheBrowser(unittest.TestCase):
    PAGE = """(() => {
      const attributes = { 'aria-label': 'server text' }
      const element = { dataset: { label: %s }, getAttribute: name => attributes[name],
                        setAttribute: (name, value) => { attributes[name] = value; calls++ } }
      let calls = 0
      const root = { querySelectorAll: selector => selector === '[data-label]' ? [element] : [] }
      localizeLabels(root); localizeLabels(root)
      return [attributes['aria-label'], calls]
    })()"""

    def label(self, label, tag):
        return run_js(self.PAGE % json.dumps(label), tag)

    def test_as_the_language_writes_it(self):
        label = "Battery {59} percent, range {1234} km"
        self.assertEqual(["Battery 59 percent, range 1,234 km", 1], self.label(label, "en-US"))
        self.assertEqual([f"Battery 59 percent, range 1{NBSP}234 km", 1], self.label(label, "de-AT"))
        self.assertEqual([f"Battery 59 percent, range 1{NARROW_NBSP}234 km", 1], self.label(label, "fr-FR"))

    def test_unknown_stays(self):
        self.assertEqual(["Battery – percent, range – km", 1], self.label("Battery – percent, range – km", "de-AT"))

    def test_page_updates_watch_it(self):
        self.assertIn("'data-label'", CLIENTSIDE.read_text(encoding="utf-8"))


@unittest.skipUnless(shutil.which("node"), "needs node")
class TestOneSourceOfSeparators(unittest.TestCase):
    """In de-AT the axis of plotly had "48.470", from its file for "de", and the tables "48 470", from Intl."""

    def plotly_config(self, tag):
        return run_js(f"""(() => {{
          let config
          window.Plotly = {{ setPlotConfig: value => {{ config = value }} }}
          window.psaccLanguage.set({json.dumps(tag)})
          return config
        }})()""", tag)

    def test_plotly_takes_the_ones_of_the_language(self):
        for tag, decimal, group in (("de-AT", ",", NBSP), ("en-US", ".", ","), ("fr-FR", ",", NARROW_NBSP),
                                    ("de-DE", ",", ".")):
            with self.subTest(tag):
                config = self.plotly_config(tag)
                self.assertEqual({"decimal": decimal, "thousands": group},
                                 config["locales"][config["locale"]]["format"])

    def test_same_as_the_tables_and_the_numbers_on_the_page(self):
        for tag in ("de-AT", "de-CH", "en-US", "en-GB", "fr-FR", "es-ES", "nb-NO", "ja-JP"):
            with self.subTest(tag):
                config = self.plotly_config(tag)
                format_ = config["locales"][config["locale"]]["format"]
                table = run_js("tableLocale()", tag)
                self.assertEqual((format_["decimal"], format_["thousands"]), (table["decimal"], table["group"]))
                written = run_js("numberText(1234567.5, 1)", tag)
                self.assertEqual(f"1{table['group']}234{table['group']}567{table['decimal']}5", written)

    def test_clientside_has_no_second_formula(self):
        source = CLIENTSIDE.read_text(encoding="utf-8")
        self.assertIn("psaccLanguage.separators()", source)
        self.assertNotIn("formatToParts(1234567.5)", source)
