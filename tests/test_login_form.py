import bz2
import contextlib
import json
import os
import shutil
import tempfile
import threading
import unittest
import zipfile
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from dash import dcc, no_update

from psa_car_controller.psa.setup.apk_parser import ApkParser, UnknownCountryError
from psa_car_controller.psa.setup.app_decoder import InitialSetup
from psa_car_controller.psa.setup.countries import (BRAND_COUNTRIES, COUNTRY_NAMES, countries_of_brand,
                                                    normalize_country_code)
from tests.github_fixture import FakeApk, build_apk_archive
from tests.test_worker_oauth import text_of
from tests.utils import get_web_view, isolate_jobs, wait_for_job

OPEL = "com.psa.mym.myopel"
VAUXHALL = "com.psa.mym.myvauxhall"
EMAIL = "someone@example.com"
PASSWORD = "S3cret pass "
VALID = {"psa-app": OPEL, "psa-email": EMAIL, "psa-password": PASSWORD, "psa-countrycode": "DE"}
ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))


def find(component, component_id):
    if getattr(component, "id", None) == component_id:
        return component
    children = getattr(component, "children", None)
    for child in children if isinstance(children, (list, tuple)) else [children]:
        if child is not None and not isinstance(child, str):
            found = find(child, component_id)
            if found is not None:
                return found
    return None


def field_order(component, ids):
    """The ids of the fields in the order of the page."""
    found = [component.id] if getattr(component, "id", None) in ids else []
    children = getattr(component, "children", None)
    for child in children if isinstance(children, (list, tuple)) else [children]:
        if child is not None and not isinstance(child, str):
            found += field_order(child, ids)
    return found


class LoginFormTestCase(unittest.TestCase):
    def setUp(self):
        self.views = get_web_view("config_views")
        isolate_jobs(self)
        self.setup = MagicMock()
        self.setup.psacc.manager.generate_redirect_url.return_value = "https://example.com/authorize"
        self.setup.psacc.manager.redirect_uri = "mymop://oauth2redirect"
        config = SimpleNamespace(Login_Worker=SimpleNamespace(url=""))
        for name, target, new in (("headless", "get_oauth_code_headless", None),
                                  ("initial_setup", "InitialSetup", MagicMock(return_value=self.setup))):
            patcher = patch.object(self.views, target, new) if new else patch.object(self.views, target)
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)
        patcher = patch.object(self.views.app, "config", config)
        patcher.start()
        self.addCleanup(patcher.stop)

    def submit(self, **fields):
        values = {**VALID, **fields}
        result, stored, *_ = self.views.connectPSA(1, values["psa-app"], values["psa-email"], values["psa-password"],
                                               values["psa-countrycode"])
        if stored is no_update:
            return result
        wait_for_job(self.views.LOGIN_JOB)
        return self.views.poll_login(stored, 1)[5]

    def marks(self, **fields):
        values = {**VALID, **fields}
        result = self.views.markLoginForm(1, values["psa-app"], values["psa-email"], values["psa-password"],
                                          values["psa-countrycode"])
        count = len(self.views.LOGIN_FIELDS)
        return (dict(zip(self.views.LOGIN_FIELDS, result[:count])), dict(zip(self.views.LOGIN_FIELDS, result[count:])))


class TestFormLayout(LoginFormTestCase):
    def test_order_is_brand_country_email_password(self):
        ids = set(self.views.LOGIN_FIELDS)
        self.assertEqual(["psa-app", "psa-countrycode", "psa-email", "psa-password"],
                         field_order(self.views.login_form(), ids))

    def test_country_is_a_choice_disabled_until_the_brand_is_known(self):
        country = find(self.views.login_form(), "psa-countrycode")
        self.assertEqual([], country.options)
        self.assertTrue(country.disabled)
        self.assertNotIsInstance(find(self.views.login_form(), "psa-app"), dcc.Input)

    def test_every_field_has_a_feedback(self):
        form = self.views.login_form()
        for field in self.views.LOGIN_FIELDS:
            self.assertIsNotNone(find(form, f"{field}-feedback"), field)

    def test_settings_prefill_keeps_a_stored_country(self):
        for stored in ("DE", "de", " de "):
            country = find(self.views.login_form(OPEL, country=stored), "psa-countrycode")
            self.assertEqual("DE", country.value)
            self.assertFalse(country.disabled)

    def test_stored_country_the_brand_does_not_have_is_not_preselected(self):
        self.assertIsNone(find(self.views.login_form(OPEL, country="GB"), "psa-countrycode").value)
        self.assertIsNone(find(self.views.login_form(OPEL, country="XX"), "psa-countrycode").value)

    def test_only_country_of_a_brand_is_preselected(self):
        self.assertEqual("GB", find(self.views.login_form(VAUXHALL), "psa-countrycode").value)

    def test_submit_is_disabled_while_the_login_runs(self):
        from psa_car_controller.web.app import dash_app  # pylint: disable=import-outside-toplevel
        # pylint: disable=protected-access
        callback = next(spec for spec in dash_app._callback_list
                        if spec["inputs"] == [{"id": "submit-form", "property": "n_clicks"}]
                        and spec.get("running"))
        self.assertIn("submit-form.disabled", json.dumps(callback["running"]))


class TestCountryChoice(LoginFormTestCase):
    def test_list_follows_the_brand(self):
        options, _, disabled = self.views.updateCountries(OPEL, None)
        self.assertEqual(len(BRAND_COUNTRIES[OPEL]), len(options))
        self.assertFalse(disabled)
        self.assertIn({"label": "Germany (DE)", "value": "DE"}, options)
        self.assertNotIn("GB", [option["value"] for option in options])

    def test_choice_stays_when_the_new_brand_has_it(self):
        self.assertEqual("DE", self.views.updateCountries("com.psa.mym.mypeugeot", "DE")[1])

    def test_choice_goes_when_the_new_brand_does_not_have_it(self):
        self.assertIsNone(self.views.updateCountries(OPEL, "GB")[1])

    def test_no_brand_means_nothing_to_choose(self):
        self.assertEqual(([], None, True), self.views.updateCountries(None, "DE"))


class TestFormCheck(LoginFormTestCase):
    def assert_refused(self, *fields, **values):
        result = self.submit(**values)
        self.initial_setup.assert_not_called()
        self.headless.assert_not_called()
        invalid, messages = self.marks(**values)
        self.assertEqual(set(fields), {field for field, value in invalid.items() if value})
        for field in fields:
            self.assertTrue(messages[field], field)
        self.assertNotIn("NoneType", text_of(result))
        return result

    def test_empty_form(self):
        self.assert_refused(*self.views.LOGIN_FIELDS, **{field: None for field in self.views.LOGIN_FIELDS})

    def test_missing_brand(self):
        self.assert_refused("psa-app", "psa-countrycode", **{"psa-app": None})

    def test_missing_country(self):
        for value in (None, "", "  "):
            self.assert_refused("psa-countrycode", **{"psa-countrycode": value})

    def test_missing_email(self):
        for value in (None, "", "  "):
            self.assert_refused("psa-email", **{"psa-email": value})

    def test_invalid_email(self):
        for value in ("someone", "someone@example", "@example.com", "some one@example.com", "a@b@example.com"):
            self.assert_refused("psa-email", **{"psa-email": value})

    def test_missing_password(self):
        for value in (None, ""):
            self.assert_refused("psa-password", **{"psa-password": value})

    def test_country_of_another_brand(self):
        self.assert_refused("psa-countrycode", **{"psa-countrycode": "GB"})

    def test_unknown_brand_or_wrong_types_from_a_forged_request(self):
        self.assert_refused("psa-app", "psa-countrycode", **{"psa-app": "com.example.other"})
        self.assert_refused("psa-app", "psa-countrycode", **{"psa-app": ["com.psa.mym.myopel"]})
        self.assert_refused("psa-email", **{"psa-email": 5})
        self.assert_refused("psa-countrycode", **{"psa-countrycode": 5})

    def test_valid_form_has_no_marks(self):
        invalid, messages = self.marks()
        self.assertFalse(any(invalid.values()))
        self.assertFalse(any(messages.values()))

    def test_valid_form_starts_the_login(self):
        self.submit()
        self.initial_setup.assert_called_once_with(OPEL, EMAIL, PASSWORD, "DE")

    def test_lowercase_country_is_sent_in_uppercase(self):
        self.submit(**{"psa-countrycode": " de "})
        self.initial_setup.assert_called_once_with(OPEL, EMAIL, PASSWORD, "DE")

    def test_email_is_trimmed_and_password_is_not(self):
        self.submit(**{"psa-email": f"  {EMAIL} "})
        self.initial_setup.assert_called_once_with(OPEL, EMAIL, PASSWORD, "DE")
        self.assertEqual(EMAIL, self.headless.call_args.args[1])
        self.assertEqual(PASSWORD, self.headless.call_args.args[2])

    def test_refused_country_of_a_newer_apk_is_shown(self):
        self.initial_setup.side_effect = UnknownCountryError("MyOpel is not available in the country \"DE\".")
        self.assertIn("not available", text_of(self.submit()))


class TestOneLoginAtATime(LoginFormTestCase):
    def test_second_submit_during_a_login_follows_it(self):
        started, finish = threading.Event(), threading.Event()

        def slow_setup(*_):
            started.set()
            finish.wait(10)
            return self.setup

        self.initial_setup.side_effect = slow_setup
        _, first, *_ = self.views.connectPSA(1, OPEL, EMAIL, PASSWORD, "DE")
        try:
            self.assertTrue(started.wait(10))
            _, second, *_ = self.views.connectPSA(2, OPEL, EMAIL, "other password", "DE")
            self.assertIsNone(second["id"])
            self.assertIsNotNone(first["id"])
            self.assertEqual(1, self.initial_setup.call_count)
        finally:
            finish.set()
        wait_for_job(self.views.LOGIN_JOB)
        self.assertEqual(1, self.initial_setup.call_count)

    def test_next_login_works_after_a_failed_one(self):
        self.initial_setup.side_effect = ConnectionError("no answer")
        self.assertIn("no answer", text_of(self.submit()))
        self.initial_setup.side_effect = None
        self.setup.psacc.manager.redirect_uri = "mymop://oauth2redirect"
        self.assertNotIn("already running", text_of(self.submit()))
        self.assertEqual(2, self.initial_setup.call_count)


class TestInitialSetupArguments(unittest.TestCase):
    def test_missing_brand_is_explained_before_any_download(self):
        with patch("psa_car_controller.psa.setup.app_decoder.get_content_from_apk") as download:
            for brand in (None, "", "com.example.other"):
                with self.assertRaisesRegex(ValueError, "Choose the brand"):
                    InitialSetup(brand, EMAIL, PASSWORD, "DE")
        download.assert_not_called()

    def test_missing_country_is_explained_before_any_download(self):
        with patch("psa_car_controller.psa.setup.app_decoder.get_content_from_apk") as download:
            for country in (None, "", "D", "DEU", "1A"):
                with self.assertRaisesRegex(ValueError, "Choose the country"):
                    InitialSetup(OPEL, EMAIL, PASSWORD, country)
        download.assert_not_called()

    def test_country_is_normalized(self):
        self.assertEqual("DE", normalize_country_code(" de "))
        self.assertEqual("", normalize_country_code(None))


class TestApkCountry(unittest.TestCase):
    def parse(self, country):
        with tempfile.TemporaryDirectory() as tmp, contextlib.chdir(tmp):
            with open("mypeugeot.apk", "wb") as apk:
                apk.write(bz2.decompress(build_apk_archive()))
            with patch("psa_car_controller.psa.setup.apk_parser.APK", FakeApk):
                parser = ApkParser("mypeugeot.apk", country)
                parser.retrieve_content_from_apk()
            return parser

    def test_known_country(self):
        self.assertEqual("fr_FR", self.parse("FR").culture)

    def test_country_missing_from_the_apk_is_explained(self):
        with self.assertRaisesRegex(UnknownCountryError, "MyPeugeot is not available in the country \"XX\""):
            self.parse("XX")

    def test_country_without_parameters_is_explained(self):
        # the real apks list IL without its parameters.json
        with self.assertRaisesRegex(UnknownCountryError, "\"IL\""):
            self.parse("IL")

    def test_real_apk_refuses_unknown_countries(self):
        archive = os.environ.get("PSACC_TEST_APK", os.path.join(ROOT, "mypeugeot.apk.bz2"))
        if not os.path.isfile(archive):
            self.skipTest(f"real apk not available: {archive}")
        with tempfile.TemporaryDirectory() as tmp, contextlib.chdir(tmp):
            with bz2.open(archive, "rb") as compressed, open("mypeugeot.apk", "wb") as apk:
                shutil.copyfileobj(compressed, apk)
            for country in ("XX", "IL"):
                with self.assertRaisesRegex(UnknownCountryError, f"\"{country}\""):
                    ApkParser("mypeugeot.apk", country).retrieve_content_from_apk()


class TestCountryLists(unittest.TestCase):
    def test_every_country_has_a_name(self):
        for brand, codes in BRAND_COUNTRIES.items():
            self.assertTrue(codes, brand)
            self.assertLessEqual(codes, set(COUNTRY_NAMES), brand)
        self.assertEqual(set(COUNTRY_NAMES), set().union(*BRAND_COUNTRIES.values()))

    def test_sizes_read_from_the_apks(self):
        self.assertEqual({"com.psa.mym.mypeugeot": 58, "com.psa.mym.mycitroen": 64, "com.psa.mym.myds": 42,
                          OPEL: 39, VAUXHALL: 1}, {brand: len(codes) for brand, codes in BRAND_COUNTRIES.items()})
        self.assertEqual(sorted(BRAND_COUNTRIES[OPEL], key=COUNTRY_NAMES.get),
                         [code for code, _ in countries_of_brand(OPEL)])

    def test_list_matches_the_local_apks(self):
        """Runs for each archive of github.com/flobz/psa_apk found in PSACC_TEST_APK_DIR or the root of the repo."""
        directory = os.environ.get("PSACC_TEST_APK_DIR", ROOT)
        compared = 0
        for brand, codes in BRAND_COUNTRIES.items():
            archive = os.path.join(directory, brand.rsplit(".", maxsplit=1)[-1] + ".apk.bz2")
            if not os.path.isfile(archive):
                continue
            compared += 1
            with self.subTest(brand=brand), tempfile.TemporaryFile() as apk:
                with bz2.open(archive, "rb") as compressed:
                    shutil.copyfileobj(compressed, apk)
                self.assertEqual(set(codes), self.usable_countries(apk))
        if not compared:
            self.skipTest(f"no real apk in {directory}")

    @staticmethod
    def usable_countries(apk):
        with zipfile.ZipFile(apk) as content:
            names = set(content.namelist())
            cultures = json.loads(content.read("res/raw/cultures.json"))
        usable = set()
        for code, culture in cultures.items():
            language, country = culture["languages"][0].split("_")
            if f"res/raw-{language}-r{country}/parameters.json" in names:
                usable.add(code)
        return usable


if __name__ == "__main__":
    unittest.main()
