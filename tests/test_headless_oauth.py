import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from psa_car_controller.psa.setup import headless_oauth
from psa_car_controller.psa.setup.headless_oauth import (AUTHORIZE_SELECTORS, EMAIL_SELECTOR, LOGIN_TIMEOUT_S,
                                                         SESSION_EXPIRED_MESSAGE, FormException, HeadlessOAuthError,
                                                         OAuthFlowError, OAuthTimeout, get_code, parse_op_error)

ERROR_PAGE = "https://idpcvs.opel.com/am/OPErrorPage.php"
SCHEME = "mymop"


class PlaywrightTimeout(Exception):
    pass


class FakePage:
    """Page with the given visible selectors and form errors."""

    def __init__(self, url="https://idpcvs.opel.com/am/oauth2/authorize", visible=(), form_errors=()):
        self.url = url
        self.visible = set(visible)
        self.form_errors = list(form_errors)
        self.clicked = []
        self.listeners = {}

    def on(self, event, handler):
        self.listeners[event] = handler

    def request(self, url):
        self.listeners["request"](SimpleNamespace(url=url))

    def is_visible(self, selector):
        return selector in self.visible

    def click(self, selector):
        self.clicked.append(selector)

    def locator(self, _):
        return SimpleNamespace(all_inner_texts=lambda: self.form_errors)


def in_the_future():
    return time.time() + 5


class TestParseOpError(unittest.TestCase):
    def test_plus_becomes_space(self):
        self.assertEqual("Something went wrong",
                         parse_op_error(f"{ERROR_PAGE}?code=500&message=Something+went+wrong"))

    def test_percent_encoding_is_decoded(self):
        self.assertEqual("Bad request", parse_op_error(f"{ERROR_PAGE}?message=Bad%20request"))

    def test_expired_context_asks_to_retry(self):
        self.assertEqual(SESSION_EXPIRED_MESSAGE,
                         parse_op_error(f"{ERROR_PAGE}?code=400&message=contextId+has+EXPIRED"))

    def test_took_too_long_asks_to_retry(self):
        self.assertEqual(SESSION_EXPIRED_MESSAGE,
                         parse_op_error(f"{ERROR_PAGE}?message=The+login+took+too+long"))

    def test_only_a_code(self):
        self.assertEqual("Stellantis reported error 500", parse_op_error(f"{ERROR_PAGE}?code=500"))

    def test_nothing_in_the_query(self):
        self.assertEqual("Stellantis reported an unknown error", parse_op_error(ERROR_PAGE))

    def test_other_pages_are_no_error(self):
        self.assertIsNone(parse_op_error("https://id-dcr.opel.com/index/authorize-consentments?code=1&message=x"))

    def test_page_named_in_a_parameter_is_no_error(self):
        self.assertIsNone(parse_op_error(f"https://idpcvs.opel.com/am/login?next={ERROR_PAGE}&message=x"))


class TestSelectors(unittest.TestCase):
    def test_dcr_consent_button_comes_first(self):
        self.assertEqual('#consentbutton', AUTHORIZE_SELECTORS[0])


class TestGetCode(unittest.TestCase):
    def test_error_page_in_a_request_is_raised(self):
        page = FakePage()
        page.is_visible = lambda _: page.request(f"{ERROR_PAGE}?message=Access+denied")
        with self.assertRaisesRegex(OAuthFlowError, "^Access denied$"), patch("time.sleep"):
            get_code(page, SCHEME, "a@b.c", "pw", in_the_future())

    def test_already_on_the_error_page(self):
        page = FakePage(url=f"{ERROR_PAGE}?message=expired")
        with self.assertRaises(OAuthFlowError) as raised:
            get_code(page, SCHEME, "a@b.c", "pw", in_the_future())
        self.assertEqual(SESSION_EXPIRED_MESSAGE, str(raised.exception))

    def test_redirect_on_a_dcr_host_returns_the_code(self):
        page = FakePage(url="https://id-dcr.opel.com/index/authorize-consentments", visible=['#consentbutton'])
        page.click = lambda selector: page.request(f"{SCHEME}://oauth2redirect/de?code=the-code")
        with patch("time.sleep"):
            self.assertEqual("the-code", get_code(page, SCHEME, "a@b.c", "pw", in_the_future()))

    def test_oauth_code_is_not_taken_for_an_error_code(self):
        page = FakePage()
        page.is_visible = lambda _: page.request(f"{SCHEME}://oauth2redirect/de?message=expired&code=the-code")
        with patch("time.sleep"):
            self.assertEqual("the-code", get_code(page, SCHEME, "a@b.c", "pw", in_the_future()))

    def test_form_error_does_not_wait_for_the_deadline(self):
        page = FakePage(visible=[EMAIL_SELECTOR], form_errors=["Invalid login or password"])
        with patch.object(headless_oauth, "_fill_credentials"), patch("time.sleep"):
            with self.assertRaisesRegex(FormException, "Invalid login or password"):
                get_code(page, SCHEME, "a@b.c", "pw", time.time() + 3600)

    def test_timeout_names_the_limit(self):
        with self.assertRaisesRegex(OAuthTimeout, f"within {LOGIN_TIMEOUT_S}s"):
            get_code(FakePage(), SCHEME, "a@b.c", "pw", time.time() - 1)


class TestFillCredentials(unittest.TestCase):
    def test_slow_page_after_submit_is_no_failure(self):
        page = MagicMock()
        page.locator.return_value.wait_for.side_effect = PlaywrightTimeout()
        with patch.object(headless_oauth, "playwright_sync", SimpleNamespace(TimeoutError=PlaywrightTimeout)):
            headless_oauth._fill_credentials(page, "a@b.c", "pw")  # pylint: disable=protected-access
        page.locator.return_value.click.assert_called_once()


class TestRunHeadlessOauth(unittest.TestCase):
    def run_flow(self, page, get_code_mock=None):
        playwright = MagicMock(TimeoutError=PlaywrightTimeout)
        browser = playwright.sync_playwright.return_value.__enter__.return_value.chromium.launch.return_value
        browser.new_context.return_value.new_page.return_value = page
        with patch.object(headless_oauth, "playwright_sync", playwright):
            if get_code_mock:
                with patch.object(headless_oauth, "get_code", get_code_mock):
                    return headless_oauth._run_headless_oauth(  # pylint: disable=protected-access
                        "https://auth", "a@b.c", "pw", SCHEME)
            return headless_oauth._run_headless_oauth(  # pylint: disable=protected-access
                "https://auth", "a@b.c", "pw", SCHEME)

    def test_error_page_message_reaches_the_user(self):
        page = MagicMock(url=f"{ERROR_PAGE}?message=Access+denied")
        page.locator.return_value.all_inner_texts.return_value = []
        page.screenshot.return_value = b""
        page.content.return_value = "<html/>"
        with self.assertRaisesRegex(HeadlessOAuthError, "Access denied"):
            self.run_flow(page)

    def test_whole_flow_gets_the_full_budget(self):
        get_code_mock = MagicMock(return_value="the-code")
        started = time.time()
        self.assertEqual("the-code", self.run_flow(MagicMock(), get_code_mock))
        deadline = get_code_mock.call_args.args[-1]
        self.assertEqual(180, LOGIN_TIMEOUT_S)
        self.assertAlmostEqual(started + LOGIN_TIMEOUT_S, deadline, delta=5)


if __name__ == "__main__":
    unittest.main()
