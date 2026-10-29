"""Access token refresh: once per rejected token, retried after a 401, scheduled before it expires."""
import threading
import time
import unittest
from http import HTTPStatus
from unittest.mock import MagicMock, patch

from oauth2_client.credentials_manager import OAuthError, ServiceInformation
from requests import RequestException

from psa_car_controller.common.utils import RateLimitException
from psa_car_controller.psa.connected_car_api.rest import ApiException
from psa_car_controller.psa.oauth import (OpenIdCredentialManager, Oauth2PSACCApiConfig, OauthAPIClient,
                                          REFRESH_BEFORE_EXPIRY, REFRESH_RETRY_DELAYS, REFRESH_RETRY_JITTER,
                                          refresh_retry_delay)
from psa_car_controller.psacc.application.psa_client import PSAClient


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


if __name__ == '__main__':
    unittest.main()
