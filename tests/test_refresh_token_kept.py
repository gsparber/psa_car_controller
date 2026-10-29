"""A failed refresh must not cost the stored refresh token: only a new login could bring it back.

The OAuth library forgets the refresh token and the session on any 401 of the token endpoint,
whatever the reason, and on an answer without a refresh token. save_config then wrote null.
PSA's status for an invalid token is unverified, so 400 and 401 invalid_grant count the same.
"""
import json
import os
import tempfile
import time
import unittest
from http import HTTPStatus
from unittest.mock import MagicMock, patch

from oauth2_client.credentials_manager import OAuthError, ServiceInformation
from requests import RequestException, Response

from psa_car_controller.psa.oauth import (OpenIdCredentialManager, REJECTED_GRANT_RETRY_DELAY, refresh_can_recover,
                                          rejected_grant_hint)
from psa_car_controller.psacc.application.car_controller import PSACarController
from psa_car_controller.psacc.application.psa_client import PSAClient
from tests.test_remote_command_api import get_api_view

TOKEN = "stored-refresh-token"


def get_manager() -> OpenIdCredentialManager:
    manager = OpenIdCredentialManager(ServiceInformation("https://idp.invalid/authorize",
                                                         "https://idp.invalid/token",
                                                         "client-id", "client-secret", ["openid"]))
    manager._access_token = "old-access-token"
    manager.refresh_token = TOKEN
    return manager


def token_answer(status: int, body) -> Response:
    response = Response()
    response.status_code = status
    response._content = body.encode() if isinstance(body, str) else json.dumps(body).encode()
    return response


def refresh(manager: OpenIdCredentialManager, status: int, body):
    # the real library code down to the HTTP call, without the rate limit shared by the test run
    with patch("oauth2_client.credentials_manager.requests.post", return_value=token_answer(status, body)) as post, \
            patch.object(manager, "_refresh_with_retries",
                         lambda: OpenIdCredentialManager._refresh_with_retries.__wrapped__(manager)), \
            patch("oauth2_client.credentials_manager._logger"):
        return manager.refresh_token_now(), post


TEMPORARY_ANSWERS = {
    "401 invalid_client": (401, {"error": "invalid_client"}),
    "401 without an error code": (401, {"httpCode": "401", "moreInformation": "Unauthorized"}),
    "401 html": (401, "<html>Unauthorized</html>"),
    "400 invalid_request": (400, {"error": "invalid_request"}),
    "429": (429, {"error": "too_many_requests"}),
    "500": (500, "<html>Internal error</html>"),
    "503 json": (503, {"error": "temporarily_unavailable"}),
}
REJECTIONS = {
    "400 invalid_grant": (400, {"error": "invalid_grant"}),
    "401 invalid_grant": (401, {"error": "invalid_grant"}),
}


class TestTemporaryRefusal(unittest.TestCase):

    def test_keeps_the_tokens_and_needs_no_login(self):
        for name, (status, body) in TEMPORARY_ANSWERS.items():
            with self.subTest(name):
                manager = get_manager()
                refreshed, _ = refresh(manager, status, body)
                self.assertFalse(refreshed)
                self.assertEqual(TOKEN, manager.refresh_token)
                self.assertEqual("old-access-token", manager.access_token)
                self.assertFalse(manager.needs_login)
                self.assertIsNone(manager.rejected_refresh_token)
                self.assertEqual("", rejected_grant_hint(manager.last_refresh_error))
                self.assertTrue(refresh_can_recover(manager.last_refresh_error))

    def test_the_next_attempt_sends_the_token_and_succeeds(self):
        manager = get_manager()
        refresh(manager, 401, {"error": "invalid_client"})
        refreshed, post = refresh(manager, 200, {"access_token": "new", "refresh_token": "rotated"})
        self.assertTrue(refreshed)
        self.assertEqual(TOKEN, post.call_args.kwargs["data"]["refresh_token"])
        self.assertEqual("rotated", manager.refresh_token)
        self.assertEqual("new", manager.access_token)

    def test_is_logged_without_the_token(self):
        manager = get_manager()
        with self.assertLogs("psa_car_controller.psa.oauth", level="ERROR") as logs:
            refresh(manager, 401, {"error": "invalid_client"})
        self.assertIn("invalid_client", logs.output[0])
        self.assertNotIn(TOKEN, logs.output[0])

    def test_the_scheduled_refresh_keeps_trying(self):
        manager = get_manager()
        manager._scheduled_refresh = True
        with patch("oauth2_client.credentials_manager.requests.post",
                   return_value=token_answer(401, {"error": "invalid_client"})), \
                patch("oauth2_client.credentials_manager._logger"), \
                patch.object(manager, "_refresh_with_retries",
                             lambda: OpenIdCredentialManager._refresh_with_retries.__wrapped__(manager)), \
                patch.object(manager, "_schedule_refresh") as schedule:
            manager._scheduled_refresh_run()
        schedule.assert_called_once()
        self.assertIsNotNone(schedule.call_args.args[0])
        self.assertEqual(TOKEN, manager.refresh_token)

    def test_does_not_shorten_the_wait_of_a_rejected_token(self):
        manager = get_manager()
        manager.grant_rejected_at = time.monotonic() - REJECTED_GRANT_RETRY_DELAY
        manager.rejected_refresh_token = TOKEN
        refresh(manager, 503, {"error": "temporarily_unavailable"})
        self.assertTrue(manager.needs_login)
        self.assertEqual(TOKEN, manager.rejected_refresh_token)
        self.assertFalse(manager.rejected_grant_retry_due())


class TestRejection(unittest.TestCase):

    def test_needs_a_login_but_keeps_the_token_for_the_retry(self):
        for name, (status, body) in REJECTIONS.items():
            with self.subTest(name):
                manager = get_manager()
                refreshed, _ = refresh(manager, status, body)
                self.assertFalse(refreshed)
                self.assertTrue(manager.needs_login)
                self.assertEqual(TOKEN, manager.rejected_refresh_token)
                self.assertEqual(TOKEN, manager.refresh_token)
                self.assertNotEqual("", rejected_grant_hint(manager.last_refresh_error))
                self.assertFalse(refresh_can_recover(manager.last_refresh_error))

    def test_not_sent_again_before_the_delay(self):
        for name, (status, body) in REJECTIONS.items():
            with self.subTest(name):
                manager = get_manager()
                refresh(manager, status, body)
                refreshed, post = refresh(manager, 200, {"access_token": "new", "refresh_token": "rotated"})
                self.assertFalse(refreshed)
                post.assert_not_called()

    def test_the_rare_retry_sends_the_token(self):
        for name, (status, body) in REJECTIONS.items():
            with self.subTest(name):
                manager = get_manager()
                refresh(manager, status, body)
                manager.grant_rejected_at -= REJECTED_GRANT_RETRY_DELAY
                refreshed, post = refresh(manager, 200, {"access_token": "new", "refresh_token": "rotated"})
                self.assertTrue(refreshed)
                self.assertEqual(TOKEN, post.call_args.kwargs["data"]["refresh_token"])
                self.assertFalse(manager.needs_login)


class TestNoStoredToken(unittest.TestCase):
    # a config.json damaged by the old behaviour: nothing to send, only a login can help

    def test_needs_a_login_without_calling_psa(self):
        manager = get_manager()
        manager.refresh_token = None
        refreshed, post = refresh(manager, 200, {"access_token": "new", "refresh_token": "rotated"})
        self.assertFalse(refreshed)
        post.assert_not_called()
        self.assertTrue(manager.needs_login)
        self.assertFalse(refresh_can_recover(manager.last_refresh_error))
        self.assertEqual("", rejected_grant_hint(manager.last_refresh_error))


class TestAnswerWithoutRefreshToken(unittest.TestCase):

    def test_keeps_the_stored_one(self):
        manager = get_manager()
        refreshed, _ = refresh(manager, 200, {"access_token": "new"})
        self.assertTrue(refreshed)
        self.assertEqual("new", manager.access_token)
        self.assertEqual(TOKEN, manager.refresh_token)

    def test_a_new_one_replaces_it(self):
        manager = get_manager()
        refresh(manager, 200, {"access_token": "new", "refresh_token": "rotated"})
        self.assertEqual("rotated", manager.refresh_token)


class TestRefreshCanRecover(unittest.TestCase):

    def test_errors(self):
        self.assertTrue(refresh_can_recover(RequestException("down")))
        self.assertTrue(refresh_can_recover(OAuthError(HTTPStatus.UNAUTHORIZED, "invalid_client")))
        self.assertFalse(refresh_can_recover(OAuthError(HTTPStatus.BAD_REQUEST, "invalid_grant")))
        self.assertFalse(refresh_can_recover(None))


class TestStartupRetry(unittest.TestCase):

    def test_a_temporary_refusal_at_startup_is_retried(self):
        controller = PSACarController.__new__(PSACarController)
        controller.myp = MagicMock()
        controller.myp.manager.last_refresh_error = OAuthError(HTTPStatus.UNAUTHORIZED, "invalid_client")
        self.assertTrue(controller._PSACarController__refresh_can_recover())
        controller.myp.manager.last_refresh_error = OAuthError(HTTPStatus.BAD_REQUEST, "invalid_grant")
        self.assertFalse(controller._PSACarController__refresh_can_recover())


class TestApiAnswer(unittest.TestCase):

    def test_only_a_rejected_token_asks_for_a_login(self):
        api_view = get_api_view()
        with api_view.app.test_request_context():
            temporary = api_view.handle_oauth_error(OAuthError(HTTPStatus.UNAUTHORIZED, "invalid_client"))
            rejected = api_view.handle_oauth_error(OAuthError(HTTPStatus.UNAUTHORIZED, "invalid_grant"))
            self.assertNotIn("reconnect", temporary[0].get_json()["message"])
            self.assertIn("reconnect", rejected[0].get_json()["message"])
        self.assertEqual((503, 503), (temporary[1], rejected[1]))


class TestSaveConfig(unittest.TestCase):

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = os.path.join(directory.name, "config.json")

    def get_client(self, refresh_token) -> PSAClient:
        client = PSAClient.__new__(PSAClient)  # bypass __init__, it does I/O
        client._config_hash = None
        client.manager = MagicMock(refresh_token=refresh_token, proxies=None)
        client.service_information = MagicMock(client_secret="secret")
        client.abrp = {}
        client.remote_client = MagicMock()
        client.remote_client.remoteCredentials.refresh_token = "remote-token"
        client.account_info = MagicMock(customer_id="customer", client_id="client", realm="realm", country_code="DE")
        client.weather_api = None
        return client

    def write(self, config):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(config)

    def saved(self) -> dict:
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def test_null_does_not_replace_the_stored_token(self):
        self.write(json.dumps({"refresh_token": TOKEN, "client_id": "client"}))
        self.get_client(None).save_config(self.path)
        saved = self.saved()
        self.assertEqual(TOKEN, saved["refresh_token"])
        self.assertEqual("remote-token", saved["remote_refresh_token"])

    def test_the_warning_does_not_show_the_token(self):
        self.write(json.dumps({"refresh_token": TOKEN}))
        with self.assertLogs("psa_car_controller.psacc.application.psa_client", level="WARNING") as logs:
            self.get_client(None).save_config(self.path)
        self.assertNotIn(TOKEN, "".join(logs.output))

    def test_a_token_replaces_the_stored_one(self):
        self.write(json.dumps({"refresh_token": TOKEN}))
        self.get_client("rotated").save_config(self.path)
        self.assertEqual("rotated", self.saved()["refresh_token"])

    def test_null_is_saved_when_there_is_nothing_to_keep(self):
        for stored in (None, '{"refresh_token": null}', "[]", "not json"):
            with self.subTest(stored):
                if stored is None:
                    if os.path.exists(self.path):
                        os.remove(self.path)
                else:
                    self.write(stored)
                self.get_client(None).save_config(self.path)
                self.assertIsNone(self.saved()["refresh_token"])

    def test_unchanged_config_is_not_written_again(self):
        self.write(json.dumps({"refresh_token": TOKEN}))
        client = self.get_client(None)
        client.save_config(self.path)
        with patch("builtins.open", wraps=open) as opened:
            client.save_config(self.path)
        self.assertNotIn("wb", [call.args[1] for call in opened.call_args_list if len(call.args) > 1])


if __name__ == '__main__':
    unittest.main()
