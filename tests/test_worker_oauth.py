import json
import logging
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import requests
from dash import html

from psa_car_controller.common.sensitive_filter import MASK
from psa_car_controller.psa.setup.headless_oauth import HeadlessOAuthError
from psa_car_controller.psa.setup.worker_oauth import (CONNECT_TIMEOUT_S, WORKER_TIMEOUT_S, WorkerLoginError,
                                                       get_oauth_code_from_worker, is_insecure_worker_url,
                                                       worker_endpoint, worker_host)
from psa_car_controller.psacc.repository.config_repository import ConfigRepository
from tests.test_remote_command_api import get_api_view

AUTH_URL = "https://idpcvs.opel.com/am/oauth2/authorize?code_challenge=abc&redirect_uri=mymop%3A%2F%2Foauth2redirect"
EMAIL = "someone@example.com"
PASSWORD = "S3cret+Pass word"
POST = "psa_car_controller.psa.setup.worker_oauth.requests.post"
WORKER = "https://worker.example.com"


def worker_answer(status, body):
    response = MagicMock(status_code=status)
    response.json.return_value = body
    return response


def ask_worker(url=WORKER):
    return get_oauth_code_from_worker(url, AUTH_URL, EMAIL, PASSWORD)


class TestWorkerUrl(unittest.TestCase):
    def test_endpoint_is_added_once(self):
        for url in ("https://w.example", "https://w.example/", "https://w.example/worker", " https://w.example/worker/ "):
            self.assertEqual("https://w.example/worker", worker_endpoint(url))

    def test_base_path_is_kept(self):
        self.assertEqual("https://w.example/psa/worker", worker_endpoint("https://w.example/psa"))

    def test_host_hides_the_url_credentials(self):
        self.assertEqual("w.example", worker_host("https://user:pw@w.example:8080/x"))

    def test_http_to_non_local_address_is_insecure(self):
        for url in ("http://worker.example.com", "HTTP://worker.example.com:8080", "http://8.8.8.8/",
                    "http://[2001:4860:4860::8888]/"):
            self.assertTrue(is_insecure_worker_url(url), url)

    def test_https_or_local_address_is_fine(self):
        for url in ("https://worker.example.com", "http://localhost:8080", "http://127.0.0.1", "http://[::1]:80",
                    "http://192.168.1.10:8080", "http://10.0.0.2", "http://stelloauth:8080", "http://nas.local",
                    "http://nas.home.arpa"):
            self.assertFalse(is_insecure_worker_url(url), url)


class TestWorkerCall(unittest.TestCase):
    @patch(POST, return_value=worker_answer(200, {"code": "the-code"}))
    def test_200_returns_the_code(self, post):
        self.assertEqual("the-code", ask_worker())
        args, kwargs = post.call_args
        self.assertEqual((WORKER + "/worker",), args)
        self.assertEqual({"url": AUTH_URL, "email": EMAIL, "password": PASSWORD}, kwargs["json"])
        self.assertEqual((CONNECT_TIMEOUT_S, 300), kwargs["timeout"])
        self.assertEqual(300, WORKER_TIMEOUT_S)

    @patch(POST, return_value=worker_answer(307, {}))
    def test_redirects_are_not_followed(self, post):
        with self.assertRaises(WorkerLoginError):
            ask_worker()
        self.assertFalse(post.call_args.kwargs["allow_redirects"])

    @patch(POST, return_value=worker_answer(400, {"message": "invalid login or password", "code": 400}))
    def test_4xx_shows_the_message(self, _):
        with self.assertRaisesRegex(WorkerLoginError, r"invalid login or password \(HTTP 400\)"):
            ask_worker()

    @patch(POST, return_value=worker_answer(429, {"message": "Rate limit exceeded. Try again later.", "code": 429}))
    def test_429_is_reported_as_rate_limit(self, _):
        with self.assertRaisesRegex(WorkerLoginError, r"rate limit of the service.*Rate limit exceeded.*HTTP 429"):
            ask_worker()

    def test_error_status_is_never_taken_for_a_code(self):
        for status, body in ((200, {"message": "no", "code": 400}), (500, {"code": "looks-like-a-code"}),
                             (200, {"code": ""}), (200, ["code"])):
            with patch(POST, return_value=worker_answer(status, body)), self.assertRaises(WorkerLoginError):
                ask_worker()

    @patch(POST)
    def test_body_that_is_not_json_is_not_shown(self, post):
        post.return_value = worker_answer(502, None)
        post.return_value.json.side_effect = ValueError()
        post.return_value.text = "<html>" + PASSWORD
        with self.assertRaisesRegex(WorkerLoginError, r"unexpected answer \(HTTP 502\)") as raised:
            ask_worker()
        self.assertNotIn(PASSWORD, str(raised.exception))

    @patch(POST, side_effect=requests.exceptions.ReadTimeout())
    def test_timeout(self, _):
        with self.assertRaisesRegex(WorkerLoginError, "did not answer in time"):
            ask_worker()

    def test_connection_error_hides_the_exception_text(self):
        error = requests.exceptions.ConnectionError(f"https://user:{PASSWORD}@worker.example.com failed")
        with patch(POST, side_effect=error), self.assertRaises(WorkerLoginError) as raised:
            ask_worker()
        self.assertNotIn(PASSWORD, str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)
        self.assertTrue(raised.exception.__suppress_context__)

    @patch(POST)
    def test_url_without_http_is_refused_before_sending(self, post):
        with self.assertRaises(WorkerLoginError):
            ask_worker("ftp://worker.example.com")
        post.assert_not_called()

    def test_credentials_repeated_by_the_service_are_masked(self):
        message = (f"bad {PASSWORD} {requests.utils.quote(PASSWORD)} for {EMAIL}, password={PASSWORD} "
                   "client_secret=abc\nnext line")
        with patch(POST, return_value=worker_answer(400, {"message": message})), \
                self.assertRaises(WorkerLoginError) as raised:
            ask_worker()
        text = str(raised.exception)
        for secret in (PASSWORD, requests.utils.quote(PASSWORD), EMAIL, "client_secret=abc", "\n"):
            self.assertNotIn(secret, text)
        self.assertIn(MASK, text)

    @patch(POST, return_value=worker_answer(400, {"message": "x" * 5000}))
    def test_long_message_is_cut(self, _):
        with self.assertRaises(WorkerLoginError) as raised:
            ask_worker()
        self.assertLess(len(str(raised.exception)), 500)

    @patch(POST, return_value=worker_answer(200, {"code": "the-code"}))
    def test_logs_warn_about_http_without_secrets(self, _):
        with self.assertLogs("psa_car_controller.psa.setup.worker_oauth", logging.INFO) as logs:
            ask_worker("http://user:urlpass@worker.example.com")
        output = "\n".join(logs.output)
        self.assertIn("without encryption", output)
        for secret in (PASSWORD, EMAIL, "urlpass", "the-code"):
            self.assertNotIn(secret, output)


class FakeWorker(BaseHTTPRequestHandler):
    answers = []
    received = []

    def do_POST(self):  # pylint: disable=invalid-name
        body = self.rfile.read(int(self.headers["Content-Length"]))
        FakeWorker.received.append((self.path, self.headers["Content-Type"], json.loads(body)))
        status, answer = FakeWorker.answers.pop(0)
        payload = json.dumps(answer).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format, *args):  # pylint: disable=redefined-builtin
        pass


class TestAgainstHttpServer(unittest.TestCase):
    """The wire format of stelloauth's worker.go, through a real HTTP connection."""

    def setUp(self):
        self.server = HTTPServer(("127.0.0.1", 0), FakeWorker)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        FakeWorker.received = []
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def test_code(self):
        FakeWorker.answers = [(200, {"code": "the-code"})]
        self.assertEqual("the-code", ask_worker(self.url))
        self.assertEqual([("/worker", "application/json", {"url": AUTH_URL, "email": EMAIL, "password": PASSWORD})],
                         FakeWorker.received)

    def test_rate_limit(self):
        FakeWorker.answers = [(429, {"message": "Rate limit exceeded. Try again later.", "code": 429})]
        with self.assertRaisesRegex(WorkerLoginError, "rate limit.*HTTP 429"):
            ask_worker(self.url)


def get_config_views():
    get_api_view()  # sets the web app up
    with patch("sys.argv", ["psa-car-controller"]):  # the app singleton parses the command line
        from psa_car_controller.web.view import config_views  # pylint: disable=import-outside-toplevel
    return config_views


def text_of(component):
    """All the text of a dash component tree."""
    if component is None:
        return ""
    if isinstance(component, (list, tuple)):
        return " ".join(text_of(child) for child in component)
    if isinstance(component, str):
        return component
    return text_of(getattr(component, "children", None))


class TestLoginOrder(unittest.TestCase):
    def setUp(self):
        self.views = get_config_views()
        self.setup = MagicMock()
        self.setup.psacc.manager.generate_redirect_url.return_value = AUTH_URL
        self.setup.psacc.manager.redirect_uri = "mymop://oauth2redirect"
        self.config = SimpleNamespace(Login_Worker=SimpleNamespace(url=WORKER))
        triggered = SimpleNamespace(triggered=[{"prop_id": "submit-form.n_clicks"}])
        for name, target, new in (("context", "callback_context", triggered),
                                  ("headless", "get_oauth_code_headless", None),
                                  ("worker", "get_oauth_code_from_worker", None),
                                  ("initial_setup", "InitialSetup", MagicMock(return_value=self.setup))):
            patcher = patch.object(self.views, target, new) if new else patch.object(self.views, target)
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)
        patcher = patch.object(self.views.app, "config", self.config)
        patcher.start()
        self.addCleanup(patcher.stop)

    def login(self):
        return self.views.connectPSA(1, "com.psa.mym.myopel", EMAIL, PASSWORD, "DE")

    def test_empty_url_goes_straight_to_the_browser(self):
        for url in (None, "", "   "):
            self.config.Login_Worker.url = url
            self.headless.return_value = "browser-code"
            self.login()
            self.worker.assert_not_called()
            self.setup.connect.assert_called_with("browser-code")

    def test_worker_code_skips_the_browser(self):
        self.worker.return_value = "worker-code"
        result = self.login()
        self.worker.assert_called_once_with(WORKER, AUTH_URL, EMAIL, PASSWORD)
        self.headless.assert_not_called()
        self.setup.connect.assert_called_once_with("worker-code")
        self.assertEqual("success", result.color)

    def test_failing_worker_falls_back_to_the_browser(self):
        for error in ("Login service w: invalid login (HTTP 400)", "Login service w: rate limit (HTTP 429)",
                      "The login service w did not answer in time"):
            self.worker.side_effect = WorkerLoginError(error)
            self.headless.reset_mock()
            self.headless.return_value = "browser-code"
            result = self.login()
            self.headless.assert_called_once_with(AUTH_URL, EMAIL, PASSWORD, "mymop")
            self.setup.connect.assert_called_with("browser-code")
            self.assertEqual("success", result.color)
            self.assertIn(error, text_of(result))

    def test_failing_worker_and_browser_show_both_and_the_link(self):
        self.worker.side_effect = WorkerLoginError("Login service w: no way (HTTP 400)")
        self.headless.side_effect = HeadlessOAuthError("Automatic login failed: nope", "url", "html", [])
        result = self.login()
        self.assertEqual("warning", result.color)
        text = text_of(result)
        for expected in ("no way", "nope", "Go to login"):
            self.assertIn(expected, text)
        self.setup.connect.assert_not_called()

    def test_failing_worker_and_missing_browser_end_on_the_manual_link(self):
        self.worker.side_effect = WorkerLoginError("Login service w: no way (HTTP 400)")
        self.headless.side_effect = RuntimeError("Playwright is not installed")
        result = self.login()
        text = text_of(result)
        for expected in ("no way", "complete manually", "Go to login"):
            self.assertIn(expected, text)

    def test_notice_only_with_a_worker(self):
        self.config.Login_Worker.url = None
        self.assertIsNone(self.views._login_worker_notice())

    def test_notice_warns_about_the_credentials_and_http(self):
        self.config.Login_Worker.url = "https://user:urlpass@worker.example.com"
        text = text_of(self.views._login_worker_notice())
        self.assertIn("worker.example.com", text)
        self.assertIn("host yourself", text)
        self.assertNotIn("urlpass", text)
        self.assertNotIn("unencrypted", text)
        self.config.Login_Worker.url = "http://worker.example.com"
        self.assertIn("unencrypted", text_of(self.views._login_worker_notice()))

    def test_form_shows_the_notice_only_with_a_worker(self):
        self.config.Login_Worker.url = None
        self.assertIsNone(self.views.login_form().children[0])
        self.config.Login_Worker.url = WORKER
        self.assertIsInstance(self.views.login_form().children[0], html.Div)


class TestLoginWorkerConfig(unittest.TestCase):
    def test_off_by_default(self):
        config = ConfigRepository.config_file_to_dto(ConfigRepository.get_default_config())
        self.assertIsNone(config.Login_Worker.url)

    def test_url_survives_a_write(self):
        config = ConfigRepository.config_file_to_dto(ConfigRepository.get_default_config())
        config.Login_Worker.url = WORKER
        with patch.object(ConfigRepository, "_write") as write:
            config.write_config()
        self.assertEqual(WORKER, write.call_args[0][1]["Login Worker"]["url"].value)

    def test_old_config_without_the_section(self):
        config = ConfigRepository(General={}, Electricity_config={})
        self.assertIsNone(config.Login_Worker.url)


class TestSettingsApi(unittest.TestCase):
    """GET /settings has no authentication: it must neither change nor give the URL the credentials go to."""

    def setUp(self):
        api = get_api_view()
        self.config = ConfigRepository(General={}, Electricity_config={}, Login_Worker={"url": WORKER})
        patcher = patch.object(api.APP, "config", self.config)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = api.app.test_client()

    def test_url_is_not_in_the_settings(self):
        self.assertNotIn(WORKER, self.client.get("/settings").get_data(as_text=True))

    @patch.object(ConfigRepository, "write_config")
    def test_url_can_not_be_changed(self, write):
        for section in ("login_worker", "Login_Worker", "login_Worker", "LOGIN_WORKER"):
            try:
                self.client.get(f"/settings/{section}?url=https://attacker.example")
            except AttributeError:
                pass
            self.assertEqual(WORKER, self.config.Login_Worker.url, section)
        write.assert_not_called()
