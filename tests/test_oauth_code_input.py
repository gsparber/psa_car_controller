import json
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import quote

from dash import html
from oauth2_client.credentials_manager import ServiceInformation

from psa_car_controller.psa.oauth import OpenIdCredentialManager, extract_code
from psa_car_controller.web import app as web_app

CODE = "0a1b2c3d-1111-2222-3333-444455556666"
REDIRECT_URL = f"mymap://oauth2redirect/de?code={CODE}&scope=openid%20profile"


def get_config_oauth():
    with patch("sys.argv", ["psa-car-controller"]):  # the app singleton parses the command line
        if web_app.app is None:  # run alone: the integration tests set the app up otherwise
            web_app.config_flask("test", "/", False, "127.0.0.1", 0,
                                 view="psa_car_controller.web.view.config_oauth")
            web_app.dash_app.layout = html.Div()
        from psa_car_controller.web.view import config_oauth  # pylint: disable=import-outside-toplevel
    return config_oauth


def manager():
    res = OpenIdCredentialManager(ServiceInformation("https://idp/auth", "https://idp/token", "id", "secret", []))
    res.redirect_uri = "mymap://oauth2redirect/de"
    res.code_verifier = "verifier"
    res._token_request = MagicMock()
    return res


class TestExtractCode(unittest.TestCase):
    def test_finds_the_code(self):
        pastes = {
            "plain": CODE,
            "whitespace": f"  {CODE}\r\n",
            "double quotes": f'"{CODE}"',
            "single quotes and spaces": f" '{CODE}' ",
            "encoded code": quote(CODE, safe=""),
            "redirect url": REDIRECT_URL,
            "redirect url, code last": f"mymap://oauth2redirect/de?scope=openid&code={CODE}",
            "quoted redirect url": f'"{REDIRECT_URL}"',
            "encoded code in url": f"mymap://oauth2redirect/de?code={quote(CODE, safe='')}&scope=openid",
            "encoded url": quote(REDIRECT_URL, safe=""),
            "fragment": f"mymap://oauth2redirect/de#state=abc&code={CODE}",
            "json": json.dumps({"code": CODE, "scope": "openid"}),
            "encoded json": quote(json.dumps({"code": CODE}), safe=""),
            "json in text": f'response: {{"state": "abc", "code": "{CODE}", "scope": "openid"}}',
            "console text": f"Failed to launch '{REDIRECT_URL}' because the scheme has no registered handler.",
            "console text, code last": f"Failed to launch 'mymap://oauth2redirect/de?code={CODE}' because of x",
            "console url without quotes": f"Navigated to mymap://oauth2redirect/de?code={CODE} (blocked)",
            "label with colon": f"Code: {CODE}",
            "label with equals": f"code = '{CODE}'",
            "authorize url next to it": f"https://idp/authorize?response_type=code&code_challenge=abc\ncode={CODE}",
        }
        for name, paste in pastes.items():
            with self.subTest(name):
                self.assertEqual(CODE, extract_code(paste))

    def test_nothing_to_find(self):
        self.assertEqual("", extract_code(None))
        self.assertEqual("", extract_code(" \n"))
        self.assertEqual("garbage", extract_code(" garbage "))

    def test_other_values_named_code_are_not_the_code(self):
        for paste in ('{"message": "rate limited", "code": 429}', "error_code=500", "response_type=code", "?code=&a=b"):
            with self.subTest(paste):
                self.assertNotEqual(36, len(extract_code(paste)))


class TestConnectWithCode(unittest.TestCase):
    def test_sends_only_the_extracted_code(self):
        for paste in (CODE, REDIRECT_URL, f' "{CODE}" '):
            with self.subTest(paste):
                psa = manager()
                psa.connect_with_code(paste)
                self.assertEqual(CODE, psa._token_request.call_args.args[0]["code"])

    def test_rejects_garbage(self):
        for paste in (None, "", "garbage", "mymap://oauth2redirect/de?scope=openid", "?code=abc", CODE + "0",
                      '{"code": 429}', '{"code": ', "[" * 100000):
            with self.subTest(paste[:30] if paste else paste):
                psa = manager()
                with self.assertRaisesRegex(ValueError, "Invalid code length"):
                    psa.connect_with_code(paste)
                psa._token_request.assert_not_called()

    def test_error_does_not_repeat_the_input(self):
        with self.assertRaises(ValueError) as error:
            manager().connect_with_code("mymap://oauth2redirect/de?code=live-secret")
        self.assertNotIn("live-secret", str(error.exception))


class TestFinishOauth(unittest.TestCase):
    def test_rejected_input_is_neither_logged_nor_shown(self):
        config_oauth = get_config_oauth()
        psa = manager()
        setup = MagicMock()
        setup.connect.side_effect = psa.connect_with_code
        with patch.object(config_oauth, "callback_context", MagicMock(triggered=True)), \
                patch.object(config_oauth.config_views, "INITIAL_SETUP", setup), \
                self.assertLogs(config_oauth.logger, "ERROR") as logs:
            alert = config_oauth.finish_oauth(1, "mymap://oauth2redirect/de?code=live-secret")
        self.assertIn("Invalid code length", alert.children)
        self.assertNotIn("live-secret", alert.children)
        self.assertNotIn("live-secret", "\n".join(logs.output))


if __name__ == '__main__':
    unittest.main()
