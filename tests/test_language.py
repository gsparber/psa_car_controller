"""The language of the charts, dates and numbers: the browser's by default, or fixed in the settings.

Before, the start read the locale of the server: in a container that logged "Can't get language" (C.UTF-8 is
en_US.UTF-8 for Python 3.11, and that one is not installed) or stopped the start (LC_ALL=C gives None), and a browser
never got its own language."""
import json
import locale
import re
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import plotly
from dash import html

from psa_car_controller.common import language
from psa_car_controller.psacc.repository.config_repository import GeneralConfig
from psa_car_controller.web import app as web_app
from psa_car_controller.web import units
from psa_car_controller.web.view import ui
from tests.utils import get_web_view

VIEW = "psa_car_controller.web.units"
WEB = Path(web_app.__file__).parent
LANGUAGE_JS = WEB / "assets" / "00_language.js"
CLIENTSIDE = WEB / "assets" / "clientside.js"
LOCALES = WEB / "assets" / web_app.PLOTLY_LOCALE_DIR


class TestStart(unittest.TestCase):
    def setUp(self):
        for name in ("app", "dash_app"):
            patcher = patch.object(web_app, name, None)
            patcher.start()
            self.addCleanup(patcher.stop)

    def start(self):
        return web_app.config_flask("test", "/", False, "127.0.0.1", 0, view=VIEW)

    def test_locale_without_a_name_does_not_stop_the_start(self):
        # what LC_ALL=C or an LC_ALL that is not installed gives: AttributeError on None.split
        with patch("locale.getlocale", return_value=(None, None)):
            self.start()
        self.assertIsNotNone(web_app.dash_app)

    def test_locale_that_is_not_installed_is_no_warning(self):
        # what a container without LANG gives on Python 3.11
        with patch("locale.getlocale", return_value=("en_US", "UTF-8")), \
                patch("locale.setlocale", side_effect=locale.Error("unsupported locale setting")), \
                self.assertNoLogs(web_app.logger, level="WARNING"):
            self.start()

    def test_start_leaves_the_locale_of_the_process(self):
        with patch("locale.setlocale") as setlocale:
            self.start()
        setlocale.assert_not_called()

    def test_no_script_from_the_plotly_cdn(self):
        with patch("locale.getlocale", return_value=("de_AT", "UTF-8")), patch("locale.setlocale"):
            self.start()
        self.assertFalse(web_app.dash_app.config.external_scripts)


class TestIndex(unittest.TestCase):
    def setUp(self):
        for name in ("app", "dash_app"):
            patcher = patch.object(web_app, name, None)
            patcher.start()
            self.addCleanup(patcher.stop)
        web_app.config_flask("test", "/", False, "127.0.0.1", 0, view=VIEW)
        web_app.dash_app.layout = html.Div()

    @staticmethod
    def index(setting="auto", **kwargs):
        with patch.object(language, "current", return_value=setting):
            return web_app.app.test_client().get("/", **kwargs).get_data(as_text=True)

    def test_setting_is_read_for_each_page(self):
        self.assertIn('name="psacc-language" content="auto"', self.index())
        self.assertIn('name="psacc-language" content="de-AT"', self.index("de-AT"))

    def test_only_the_language_script_is_linked(self):
        page = self.index()
        self.assertIn("00_language.js", page)
        self.assertNotIn("assets/plotly-locales/plotly-locale-", page)
        self.assertNotIn("cdn.plot.ly", page)

    def test_language_script_comes_before_the_renderer(self):
        page = self.index()
        self.assertLess(page.index("00_language.js"), page.index("clientside.js"))
        self.assertLess(page.index("00_language.js"), page.index("new DashRenderer"))

    def test_url_of_the_files_follows_the_prefix(self):
        self.assertIn('data-plotly-locales-url="/assets/plotly-locales/"', self.index())
        self.assertIn('data-plotly-locales-url="/ingress/assets/plotly-locales/"',
                      self.index(headers={"X-Ingress-Path": "/ingress"}))

    def test_prefix_can_not_break_out_of_the_tag(self):
        with patch.object(web_app.dash_app, "get_asset_url", return_value='/x"y/'), \
                patch.object(language, "current", return_value="auto"):
            meta = web_app.dash_app._language_meta()  # pylint: disable=protected-access
        self.assertIn('data-plotly-locales-url="/x&quot;y/"', meta)

    def test_locale_file_is_served(self):
        response = web_app.app.test_client().get("/assets/plotly-locales/plotly-locale-de.js")
        self.assertEqual(200, response.status_code)
        self.assertIn(b'name:"de"', response.get_data())
        response.close()


class TestSetting(unittest.TestCase):
    def test_normalize(self):
        for value, expected in (("de-AT", "de-AT"), ("de_at", "de-AT"), (" DE-at ", "de-AT"), ("fr", "fr-BE"),
                                ("auto", "auto"), ("", "auto"), (None, "auto"), ("C", "auto"), (12, "auto"),
                                ('"><script>', "auto"), ("xx-YY", "auto")):
            self.assertEqual(expected, language.normalize(value), value)

    def test_only_unknown_values_warn(self):
        with self.assertNoLogs(language.logger, level="WARNING"):
            language.normalize("")
        with self.assertLogs(language.logger, level="WARNING"):
            language.normalize("xx")

    def test_config_default_is_the_browser(self):
        self.assertEqual("auto", GeneralConfig().language)

    def test_config_never_holds_an_unknown_value(self):
        self.assertEqual("auto", GeneralConfig(language="../../etc").language)
        config = GeneralConfig(language="de_at")
        self.assertEqual("de-AT", config.language)
        config.language = "xx"  # the same way as /settings/general?language=xx
        self.assertEqual("auto", config.language)

    def test_current_without_config(self):
        with patch("psa_car_controller.psacc.application.car_controller.PSACarController", MagicMock(side_effect=AttributeError)):
            self.assertEqual("auto", language.current())


class TestSettingsPage(unittest.TestCase):
    def setUp(self):
        self.settings = get_web_view("settings")
        self.config = MagicMock()
        self.config.General.language = "auto"
        patcher = patch.object(self.settings, "app")
        patcher.start().return_value.config = self.config
        self.addCleanup(patcher.stop)
        patcher = patch.object(language, "current", side_effect=lambda: self.config.General.language)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_saved(self):
        result = self.settings.change_language("de-AT")
        self.assertEqual("de-AT", self.config.General.language)
        self.config.write_config.assert_called_once()
        self.assertEqual("de-AT", result[-1])
        self.assertEqual("Language: Deutsch (Österreich)", result[0])

    def test_unknown_value_is_not_saved(self):
        result = self.settings.change_language('"><script>')
        self.assertEqual("auto", self.config.General.language)
        self.config.write_config.assert_not_called()
        self.assertEqual("Error", result[1])
        self.assertEqual(self.settings.no_update, result[-1])

    def test_write_error_goes_back(self):
        self.config.write_config.side_effect = OSError("read only")
        result = self.settings.change_language("de-AT")
        self.assertEqual("auto", self.config.General.language)
        self.assertEqual("Error", result[1])
        self.assertEqual(self.settings.no_update, result[-1])

    def test_page_shows_every_language(self):
        select = self.settings._language().children[1]  # pylint: disable=protected-access
        self.assertEqual("auto", select.value)
        self.assertEqual(list(language.LANGUAGES), [option["value"] for option in select.options])


class TestBundledFiles(unittest.TestCase):
    def test_license_and_source_are_there(self):
        self.assertIn("MIT License", (LOCALES / "LICENSE.txt").read_text(encoding="utf-8"))
        self.assertIn("plotly.js", (LOCALES / "SOURCE.txt").read_text(encoding="utf-8"))

    def test_same_major_version_as_the_plotly_of_dash(self):
        bundled = re.search(r"plotly\.js (\d+)\.", (LOCALES / "SOURCE.txt").read_text(encoding="utf-8")).group(1)
        min_js = Path(plotly.__file__).parent / "package_data" / "plotly.min.js"
        with min_js.open(encoding="utf-8") as file:
            used = re.search(r"plotly\.js v(\d+)\.", file.read(200)).group(1)
        self.assertEqual(used, bundled, "refresh assets/plotly-locales")

    def test_files_register_themselves(self):
        files = list(LOCALES.glob("plotly-locale-*.js"))
        self.assertGreater(len(files), 50)
        for file in files:
            self.assertIn("window.PlotlyLocales.push(locale)", file.read_text(encoding="utf-8"), file.name)

    def test_every_language_has_its_file_except_english(self):
        known = web_app.plotly_locales()
        for key in language.LANGUAGES:
            code = key.lower()
            base = {"nb": "no"}.get(code.split("-")[0], code.split("-")[0])
            if key != language.AUTO and base != "en":
                self.assertTrue(code in known or base in known, key)

    def test_service_worker_has_no_plotly_cdn(self):
        self.assertNotIn("cdn.plot.ly", (WEB / "static" / "sw.js").read_text(encoding="utf-8"))
        self.assertIn("'assets/'", (WEB / "static" / "sw.js").read_text(encoding="utf-8"))


class TestPageHelpers(unittest.TestCase):
    def test_number_is_left_to_the_browser(self):
        number = ui.fmt(1234.56, 1)
        props = number.to_plotly_json()["props"]
        self.assertEqual(("local-number", "1234.56", 1), (props["className"], props["value"], props["data-digits"]))
        self.assertEqual("1 234.6", props["children"])

    def test_unknown_number(self):
        for value in (None, "x", True):
            self.assertEqual("–", ui.fmt(value))
            self.assertEqual("–", ui.with_unit(value, "km"))
            self.assertEqual("–", ui.distance(value, units.KM))

    def test_distance_in_the_display_unit(self):
        number, unit = ui.distance(16093.44, units.MI)
        self.assertEqual((10000, " mi"), (round(float(number.value)), unit))


@unittest.skipUnless(shutil.which("node"), "needs node")
class TestLanguageScript(unittest.TestCase):
    @staticmethod
    def run_js(expression, setting="auto", browser="de-AT", ready="loading", plotly_locales="de de-ch fr no ja"):
        script = f"""
            const log = console.log; const written = []; const added = [];
            globalThis.window = globalThis;
            Object.defineProperty(globalThis, 'navigator', {{ value: {{ language: {json.dumps(browser)} }} }});
            globalThis.document = {{
              readyState: {json.dumps(ready)},
              querySelector: () => ({{ content: {json.dumps(setting)},
                dataset: {{ plotlyLocales: {json.dumps(plotly_locales)}, plotlyLocalesUrl: '/pre/assets/plotly-locales/' }} }}),
              write: text => written.push(text),
              createElement: () => ({{}}),
              head: {{ appendChild: script => added.push(script.src) }}
            }};
            require('vm').runInThisContext(require('fs').readFileSync({json.dumps(str(LANGUAGE_JS))}, 'utf8'));
            log(JSON.stringify(eval({json.dumps(expression)})));"""
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True, timeout=30)
        return json.loads(result.stdout)

    def test_browser_by_default(self):
        self.assertEqual("de-AT", self.run_js("psaccLanguage.tag"))

    def test_setting_beats_the_browser(self):
        self.assertEqual("fr-FR", self.run_js("psaccLanguage.tag", setting="fr-FR"))

    def test_unknown_language_is_english(self):
        for browser in ("xx-YY", "no tag at all!", ""):
            self.assertEqual("en-US", self.run_js("psaccLanguage.tag", browser=browser), browser)
            self.assertEqual([], self.run_js("written", browser=browser), browser)

    def test_name_for_plotly(self):
        for tag, name in (("de-AT", "de"), ("de-CH", "de-CH"), ("fr-FR", "fr"), ("nb-NO", "no"), ("nn-NO", "no"),
                          ("en-US", "en-US"), ("en-GB", "en"), ("en", "en"), ("xx-YY", "en-US")):
            self.assertEqual(name, self.run_js(f"psaccLanguage.plotlyLocale('{tag}')"), tag)

    def test_file_of_the_language_blocks_while_the_page_loads(self):
        self.assertEqual(['<script src="/pre/assets/plotly-locales/plotly-locale-de.js"></script>'], self.run_js("written"))

    def test_nothing_to_load_for_english(self):
        self.assertEqual([], self.run_js("written", browser="en-US"))

    def test_file_is_loaded_once(self):
        self.assertEqual(1, self.run_js("psaccLanguage.set('de-DE'); psaccLanguage.set('de-AT'); written.length"))

    def test_new_choice_adds_a_script_once_the_page_is_there(self):
        self.assertEqual(["/pre/assets/plotly-locales/plotly-locale-fr.js"],
                         self.run_js("document.readyState = 'complete'; psaccLanguage.set('fr-FR'); added"))

    def test_plotly_gets_the_language_when_it_loads_and_on_a_change(self):
        expression = """(() => {
            const calls = []; window.Plotly = { setPlotConfig: config => calls.push(config.locale) };
            document.readyState = 'complete'; psaccLanguage.set('fr-FR'); return [calls, window.Plotly !== undefined]
        })()"""
        self.assertEqual([["de", "fr"], True], self.run_js(expression))


@unittest.skipUnless(shutil.which("node"), "needs node")
class TestClientsideFormats(unittest.TestCase):
    @staticmethod
    def run_js(expression, language_tag):
        script = (f"globalThis.window = {{console, psaccLanguage: {{tag: {json.dumps(language_tag)}}}}};"
                  f"require('vm').runInThisContext(require('fs').readFileSync({json.dumps(str(CLIENTSIDE))}, 'utf8'));"
                  f"console.log(JSON.stringify(eval({json.dumps(expression)})))")
        result = subprocess.run(["node", "-e", script], env={"TZ": "Europe/Vienna", "PATH": shutil.which("node").rsplit("/", 1)[0]},
                                capture_output=True, text=True, check=True, timeout=30)
        return json.loads(result.stdout)

    def test_date(self):
        expression = "localTimeText('2026-10-29T23:30:00Z', 'datetime')"
        self.assertRegex(self.run_js(expression, "de-AT"), r"^Fr\.,? 30\. Okt\.?,? 00:30$")
        self.assertRegex(self.run_js(expression, "en-US"), r"^Fri, Oct 30(,| at) 12:30 AM$")
        self.assertRegex(self.run_js(expression, "fr-FR"), r"^ven\. 30 oct\.,? (à )?00:30$")

    def test_date_of_the_control_page(self):
        expression = "localTimeText('2026-10-29T23:30:00Z', 'locale')"
        self.assertEqual("00:30:00 30.10.2026", self.run_js(expression, "de-AT"))

    def test_updated_with_date(self):
        self.assertRegex(self.run_js("localTimeText('2020-01-05T23:30:00Z', 'updated')", "de-AT"), r"^6\. Jän\.?,? 00:30$")

    def test_number(self):
        self.assertEqual("1.234,50", self.run_js("numberText(1234.5, 2)", "de-DE"))
        self.assertEqual("1,234.50", self.run_js("numberText(1234.5, 2)", "en-US"))
        self.assertEqual("1 234,50", self.run_js("numberText(1234.5, 2)", "fr-FR"))

    def test_cards_drop_the_decimals_of_big_numbers(self):
        self.assertEqual(["6,30", "12.346"], self.run_js("[nbFormat(6.3), nbFormat(12345.6)]", "de-DE"))

    def test_separators_of_the_tables(self):
        self.assertEqual({"decimal": ",", "group": "."}, self.run_js("tableLocale()", "de-DE"))
        self.assertEqual({"decimal": ".", "group": ","}, self.run_js("tableLocale()", "en-US"))

    def test_table_date_and_slider(self):
        table = "(() => { const d = {trips: [{a: '2026-10-29T23:30:00Z'}]}; addLocaleDate(d, {trips: ['a']}); return d.trips[0].a_str })()"
        self.assertEqual("30.10.2026, 00:30", self.run_js(table, "de-AT"))
        self.assertEqual("30.10.26", self.run_js("localSliderMarks({1793316600: {label: 'x'}})[1793316600].label", "de-AT"))

    def test_page_numbers_are_filled_in(self):
        expression = """(() => {
            const element = { getAttribute: () => '1234.5', dataset: { digits: '1' }, textContent: '1 234.5' };
            localizeNumbers({ getElementsByClassName: () => [element] });
            return element.textContent
        })()"""
        self.assertEqual("1.234,5", self.run_js(expression, "de-DE"))

    def test_broken_number_keeps_the_text(self):
        expression = """(() => {
            const element = { getAttribute: () => 'x', dataset: { digits: '0' }, textContent: 'old' };
            localizeNumbers({ getElementsByClassName: () => [element] });
            return element.textContent
        })()"""
        self.assertEqual("old", self.run_js(expression, "de-DE"))


class TestChartAxis(unittest.TestCase):
    def test_date_axis_takes_the_format_of_the_language(self):
        source = (WEB / "figures.py").read_text(encoding="utf-8")
        self.assertIn('tickformat="%x"', source)
        self.assertNotIn("%d/%m/%Y", source)


if __name__ == '__main__':
    unittest.main()
