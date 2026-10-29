"""Access token refresh: once per rejected token, retried after a 401, scheduled before it expires."""
import json
import threading
import time
import unittest
from http import HTTPStatus
from unittest.mock import DEFAULT, MagicMock, patch

from oauth2_client.credentials_manager import OAuthError, ServiceInformation
from requests import RequestException, Response

from psa_car_controller.common.utils import RateLimitException
from psa_car_controller.psa.RemoteCredentials import RemoteCredentials
from psa_car_controller.psa.connected_car_api.rest import ApiException
from psa_car_controller.psa.oauth import (OpenIdCredentialManager, Oauth2PSACCApiConfig, OauthAPIClient,
                                          REFRESH_BEFORE_EXPIRY, REFRESH_RETRY_DELAYS, REFRESH_RETRY_JITTER,
                                          refresh_retry_delay)
from psa_car_controller.psacc.application.psa_client import PSAClient

from tests.utils import get_rc


def get_manager() -> OpenIdCredentialManager:
    manager = OpenIdCredentialManager(ServiceInformation("https://idp.invalid/authorize",
                                                         "https://idp.invalid/token",
                                                         "client-id", "client-secret", ["openid"]))
    manager._access_token = "old-access-token"
    manager.refresh_token = "refresh-token"
    return manager


def unlimited(manager: OpenIdCredentialManager):
    # bypass the rate limit: it is shared by every manager of the test run
    return patch.object(manager, "_refresh_with_retries",
                        lambda: OpenIdCredentialManager._refresh_with_retries.__wrapped__(manager))


def new_tokens(manager: OpenIdCredentialManager, expires_in=None):
    def refresh_token():
        response = {"access_token": "new-access-token", "refresh_token": "new-refresh-token"}
        if expires_in is not None:
            response["expires_in"] = expires_in
        manager._process_token_response(response, False)
    return refresh_token


def invalid_grant():
    return OAuthError(HTTPStatus.BAD_REQUEST, "invalid_grant", "grant is invalid")


class TestSingleFlight(unittest.TestCase):

    def test_concurrent_callers_refresh_once(self):
        manager = get_manager()
        refreshing = threading.Event()

        def slow_refresh():
            refreshing.set()
            time.sleep(0.2)
            new_tokens(manager)()

        results = []
        with unlimited(manager), patch.object(manager, "_refresh_token", side_effect=slow_refresh) as refresh:
            first = threading.Thread(target=lambda: results.append(manager.refresh_token_now()))
            first.start()
            refreshing.wait(1)
            # this caller saw the same old token, before the first refresh ended
            second = threading.Thread(target=lambda: results.append(
                manager.refresh_token_now("old-access-token")))
            second.start()
            first.join()
            second.join()
        self.assertEqual([True, True], results)
        refresh.assert_called_once()

    def test_a_token_already_replaced_is_not_refreshed_again(self):
        manager = get_manager()
        manager._access_token = "new-access-token"
        with unlimited(manager), patch.object(manager, "_refresh_token") as refresh:
            self.assertTrue(manager.refresh_token_now("old-access-token"))
        refresh.assert_not_called()

    def test_skipped_refreshes_dont_count_in_the_rate_limit(self):
        manager = get_manager()
        manager._access_token = "new-access-token"
        with patch.object(manager, "_refresh_token") as refresh:
            for _ in range(10):
                self.assertTrue(manager.refresh_token_now("old-access-token"))
        refresh.assert_not_called()


class TestRejectedRefreshToken(unittest.TestCase):

    def test_is_not_sent_again(self):
        manager = get_manager()
        with unlimited(manager), patch.object(manager, "_refresh_token", side_effect=invalid_grant()) as refresh:
            self.assertFalse(manager.refresh_token_now())
            self.assertFalse(manager.refresh_token_now())
            self.assertFalse(manager.refresh_token_now("another-access-token"))
        refresh.assert_called_once()
        self.assertTrue(manager.needs_login)
        self.assertEqual("refresh-token", manager.rejected_refresh_token)
        self.assertIsInstance(manager.last_refresh_error, OAuthError)

    def test_a_new_refresh_token_is_sent(self):
        manager = get_manager()
        with unlimited(manager), patch.object(manager, "_refresh_token", side_effect=invalid_grant()):
            manager.refresh_token_now()
        manager.refresh_token = "another-refresh-token"
        with unlimited(manager), patch.object(manager, "_refresh_token", side_effect=new_tokens(manager)) as refresh:
            self.assertTrue(manager.refresh_token_now())
        refresh.assert_called_once()
        self.assertFalse(manager.needs_login)
        self.assertIsNone(manager.rejected_refresh_token)


def unauthorized():
    return ApiException(status=401, reason="Unauthorized")


class TestRetryAfter401(unittest.TestCase):

    def get_client(self, manager) -> PSAClient:
        client = PSAClient.__new__(PSAClient)  # bypass __init__, it does I/O
        client.manager = manager
        client.api_config = Oauth2PSACCApiConfig()
        client.api_config.set_refresh_callback(client._refresh_api_token)
        client.api_config.access_token = manager.access_token
        return client

    def test_the_request_is_sent_again_with_the_new_token(self):
        manager = get_manager()
        client = self.get_client(manager)
        sent_tokens = []

        def call_api(*_args):
            sent_tokens.append(client.api_config.access_token)
            if client.api_config.access_token == "old-access-token":
                raise unauthorized()
            return "vehicles"

        with unlimited(manager), patch.object(manager, "_refresh_token", side_effect=new_tokens(manager)), \
                patch.object(OauthAPIClient, "_ApiClient__call_api", side_effect=call_api):
            self.assertEqual("vehicles", OauthAPIClient(client.api_config).call_api("/user/vehicles", "GET"))
        self.assertEqual(["old-access-token", "new-access-token"], sent_tokens)

    def test_only_once(self):
        manager = get_manager()
        client = self.get_client(manager)
        with unlimited(manager), patch.object(manager, "_refresh_token", side_effect=new_tokens(manager)) as refresh, \
                patch.object(OauthAPIClient, "_ApiClient__call_api", side_effect=unauthorized()) as call_api:
            with self.assertRaises(ApiException):
                OauthAPIClient(client.api_config).call_api("/user/vehicles", "GET")
        self.assertEqual(2, call_api.call_count)
        refresh.assert_called_once()

    def test_the_rejected_token_is_passed_to_the_refresh(self):
        config = Oauth2PSACCApiConfig()
        config.access_token = "old-access-token"
        callback = MagicMock(return_value=False)
        config.set_refresh_callback(callback)
        with patch.object(OauthAPIClient, "_ApiClient__call_api", side_effect=unauthorized()):
            with self.assertRaises(ApiException):
                OauthAPIClient(config).call_api("/user/vehicles", "GET")
        callback.assert_called_once_with("old-access-token")


class TestRetryDelay(unittest.TestCase):

    def test_backoff_with_jitter(self):
        for failures, delay in enumerate(REFRESH_RETRY_DELAYS + (900, 900), start=1):
            for _ in range(20):
                self.assertTrue(delay <= refresh_retry_delay(failures) <= delay * (1 + REFRESH_RETRY_JITTER))


class FakeTimer:
    def __init__(self, delay, function):
        self.delay = delay
        self.function = function
        self.daemon = False
        self.cancelled = False

    def start(self):
        pass

    def cancel(self):
        self.cancelled = True


class TestScheduledRefresh(unittest.TestCase):

    def setUp(self):
        self.timers = []

        def new_timer(delay, function):
            self.timers.append(FakeTimer(delay, function))
            return self.timers[-1]

        patcher = patch("psa_car_controller.psa.oauth.threading.Timer", side_effect=new_timer)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_refreshed_before_it_expires(self):
        manager = get_manager()
        manager.start_scheduled_refresh()
        self.assertEqual([], self.timers)  # expiry unknown: refreshed on the next 401 only
        new_tokens(manager, expires_in=3600)()
        self.assertEqual(1, len(self.timers))
        self.assertAlmostEqual(3600 - REFRESH_BEFORE_EXPIRY, self.timers[0].delay, delta=5)

    def test_not_scheduled_when_not_started(self):
        manager = get_manager()
        new_tokens(manager, expires_in=3600)()
        self.assertEqual([], self.timers)

    def test_stop_cancels_it(self):
        manager = get_manager()
        manager.start_scheduled_refresh()
        new_tokens(manager, expires_in=3600)()
        manager.stop_scheduled_refresh()
        self.assertTrue(self.timers[0].cancelled)
        new_tokens(manager, expires_in=3600)()
        self.assertEqual(1, len(self.timers))

    def test_backs_off_while_the_server_is_unreachable(self):
        manager = get_manager()
        manager.start_scheduled_refresh()
        new_tokens(manager, expires_in=3600)()
        with unlimited(manager), patch.object(manager, "_refresh_token", side_effect=RequestException("down")), \
                patch("psa_car_controller.psa.oauth.time.sleep"):
            self.timers[-1].function()
            self.timers[-1].function()
        self.assertTrue(60 <= self.timers[1].delay <= 66)
        self.assertTrue(120 <= self.timers[2].delay <= 132)

    def test_backs_off_when_rate_limited(self):
        manager = get_manager()
        manager.start_scheduled_refresh()
        new_tokens(manager, expires_in=3600)()
        with patch.object(manager, "_refresh_with_retries", side_effect=RateLimitException("refresh")):
            self.timers[-1].function()
        self.assertTrue(60 <= self.timers[-1].delay <= 66)

    def test_stops_when_the_refresh_token_is_rejected(self):
        manager = get_manager()
        manager.start_scheduled_refresh()
        new_tokens(manager, expires_in=3600)()
        with unlimited(manager), patch.object(manager, "_refresh_token", side_effect=invalid_grant()):
            self.timers[-1].function()
        self.assertEqual(1, len(self.timers))

    def test_success_schedules_the_next_one(self):
        manager = get_manager()
        manager.start_scheduled_refresh()
        new_tokens(manager, expires_in=3600)()
        with unlimited(manager), patch.object(manager, "_refresh_token", side_effect=new_tokens(manager, 1800)):
            self.timers[-1].function()
        self.assertEqual(2, len(self.timers))
        self.assertAlmostEqual(1800 - REFRESH_BEFORE_EXPIRY, self.timers[-1].delay, delta=5)


def answer(status: int, body: dict) -> Response:
    response = Response()
    response.status_code = status
    response._content = json.dumps(body).encode()
    return response


def fake_api(manager: OpenIdCredentialManager, before_answer=None) -> list:
    """Answer POSTs like PSA: 401 to the old access token. Returns the tokens sent, in order."""
    session = manager._get_session()
    sent = []

    def post(_url, **_kwargs):
        token = session.headers["Authorization"][len("Bearer "):]
        sent.append(token)
        if token != "old-access-token":
            return answer(200, {})
        if before_answer:
            before_answer()
        return answer(401, {"moreInformation": "Token is invalid"})
    session.post = post
    return sent


class TestRefreshInsideARequest(unittest.TestCase):
    """manager.get/post refresh on a 401 too: same lock, rejected token and rate limit as the others."""

    def test_concurrent_requests_refresh_once(self):
        manager = get_manager()
        both_rejected = threading.Barrier(2)
        sent = fake_api(manager, lambda: both_rejected.wait(5))

        def slow_refresh():
            time.sleep(0.2)
            new_tokens(manager)()

        results = []
        with unlimited(manager), patch.object(manager, "_refresh_token", side_effect=slow_refresh) as refresh:
            threads = [threading.Thread(target=lambda: results.append(manager.post("https://api.invalid/token")))
                       for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        refresh.assert_called_once()
        self.assertEqual([200, 200], [response.status_code for response in results])
        self.assertEqual(["new-access-token"] * 2, sent[2:])

    def test_a_token_replaced_meanwhile_is_not_refreshed_again(self):
        manager = get_manager()
        sent = fake_api(manager, lambda: setattr(manager, "_access_token", "new-access-token"))
        with unlimited(manager), patch.object(manager, "_refresh_token") as refresh:
            self.assertEqual(200, manager.post("https://api.invalid/token").status_code)
        refresh.assert_not_called()
        self.assertEqual(["old-access-token", "new-access-token"], sent)

    def test_a_rejected_refresh_token_is_not_sent_again(self):
        manager = get_manager()
        sent = fake_api(manager)
        rejection = answer(400, {"error": "invalid_grant", "error_description": "grant is invalid"})
        with unlimited(manager), patch("oauth2_client.credentials_manager.requests.post",
                                       return_value=rejection) as token_endpoint:
            self.assertFalse(manager.refresh_token_now())
            with self.assertRaises(OAuthError) as raised:
                manager.post("https://api.invalid/token")
        token_endpoint.assert_called_once()
        self.assertEqual("invalid_grant", raised.exception.error)
        self.assertEqual(["old-access-token"], sent)

    def test_a_refusal_during_the_request_is_remembered(self):
        manager = get_manager()
        fake_api(manager)
        with unlimited(manager), patch.object(manager, "_refresh_token", side_effect=invalid_grant()) as refresh:
            for _ in range(2):
                with self.assertRaises(OAuthError):
                    manager.post("https://api.invalid/token")
        refresh.assert_called_once()
        self.assertTrue(manager.needs_login)
        self.assertEqual("refresh-token", manager.rejected_refresh_token)

    def test_the_rate_limit_applies(self):
        manager = get_manager()
        sent = fake_api(manager)
        with patch.object(manager, "_refresh_with_retries", side_effect=RateLimitException("refresh")), \
                patch.object(manager, "_refresh_token") as refresh, \
                self.assertRaises(RateLimitException):
            manager.post("https://api.invalid/token")
        refresh.assert_not_called()
        self.assertEqual(["old-access-token"], sent)

    def test_an_unreachable_token_endpoint_is_reported(self):
        manager = get_manager()
        fake_api(manager)
        with unlimited(manager), patch.object(manager, "_refresh_token", side_effect=RequestException("down")), \
                patch("psa_car_controller.psa.oauth.time.sleep"), self.assertRaises(RequestException):
            manager.post("https://api.invalid/token")
        self.assertFalse(manager.needs_login)

    def test_rotated_tokens_are_kept(self):
        manager = get_manager()
        sent = fake_api(manager)
        saved = []
        manager.refresh_callbacks.append(lambda: saved.append(manager.refresh_token))
        tokens = answer(200, {"access_token": "new-access-token", "refresh_token": "new-refresh-token"})
        with unlimited(manager), patch("oauth2_client.credentials_manager.requests.post", return_value=tokens):
            self.assertEqual(200, manager.post("https://api.invalid/token").status_code)
        self.assertEqual("new-refresh-token", manager.refresh_token)
        self.assertEqual(["new-refresh-token"], saved)
        self.assertEqual(["old-access-token", "new-access-token"], sent)

    def test_a_request_from_a_refresh_callback_doesnt_deadlock(self):
        manager = get_manager()
        fake_api(manager)
        errors = []

        def request_during_refresh():
            try:
                manager.post("https://api.invalid/token")
            except OAuthError as e:
                errors.append(e)
        manager.refresh_callbacks.append(request_during_refresh)

        def refresh():  # PSA still refuses the token the callback sends
            manager.refresh_token = "new-refresh-token"

        with unlimited(manager), patch.object(manager, "_refresh_token", side_effect=refresh) as refresh_token:
            thread = threading.Thread(target=manager.refresh_token_now, daemon=True)
            thread.start()
            thread.join(5)
        self.assertFalse(thread.is_alive(), "deadlock")
        refresh_token.assert_called_once()
        self.assertEqual(1, len(errors))

    def test_other_answers_dont_refresh(self):
        manager = get_manager()
        manager._get_session().post = lambda _url, **_kwargs: answer(401, {"moreInformation": "Something else"})
        with unlimited(manager), patch.object(manager, "_refresh_token") as refresh:
            self.assertEqual(401, manager.post("https://api.invalid/token").status_code)
        refresh.assert_not_called()

    def test_remote_token_refresh_doesnt_use_an_otp_when_the_grant_is_rejected(self):
        manager = get_manager()
        fake_api(manager)
        remote_client = get_rc()
        remote_client.manager = manager
        remote_client.remoteCredentials = RemoteCredentials("remote-refresh-token")
        remote_client.mqtt_client = MagicMock()
        remote_client.otp = MagicMock()
        # the access token was refused between the regular refresh and the remote token request
        with unlimited(manager), patch.object(manager, "_refresh_token", side_effect=invalid_grant()), \
                patch.object(manager, "refresh_token_now", wraps=manager.refresh_token_now) as refresh_now:
            refresh_now.side_effect = [True, DEFAULT]
            self.assertFalse(remote_client._refresh_remote_token(force=True))
        remote_client.otp.get_otp_code.assert_not_called()


if __name__ == '__main__':
    unittest.main()
