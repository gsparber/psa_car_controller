import unittest
from datetime import datetime, timedelta
from http import HTTPStatus
from unittest.mock import MagicMock
from unittest import mock

from oauth2_client.credentials_manager import OAuthError

from psa_car_controller.common.utils import RateLimitException
from psa_car_controller.psa.RemoteClient import KEEPALIVE_PERIOD, KEEPALIVE_RETRY_PERIOD, MAX_REFRESH_DELAY, \
    MQTT_TOKEN_TTL, TOKEN_ACCOUNT_FAILED, TOKEN_RATE_LIMITED, TOKEN_REMOTE_FAILED, RemoteClient
from psa_car_controller.psa.RemoteCredentials import RemoteCredentials
from psa_car_controller.psa.otp.otp import ConfigException

from tests.utils import get_rc


def get_remote_client(remote_refresh_token="remote_refresh_token"):
    remote_client: RemoteClient = get_rc()
    remote_client.manager = MagicMock()
    remote_client.manager.refresh_token_now.return_value = True
    remote_client.manager.needs_login = False
    res = MagicMock()
    res.json.return_value = {"access_token": "access_token"}
    remote_client.manager.post.return_value = res
    remote_client.remoteCredentials = RemoteCredentials(remote_refresh_token)
    remote_client.mqtt_client = MagicMock()
    return remote_client


class TestRefreshRemoteToken(unittest.TestCase):

    def test_forced_refresh_is_throttled(self):
        # GIVEN a remote client whose token was just refreshed
        remote_client = get_remote_client()
        remote_client.remote_token_last_update = datetime.now()

        # WHEN a forced refresh is requested (e.g. on mqtt disconnect rc 7)
        refreshed = remote_client._refresh_remote_token(force=True)

        # THEN the refresh is skipped: it happened less than MAX_REFRESH_DELAY ago
        self.assertTrue(refreshed)
        remote_client.manager.refresh_token_now.assert_not_called()
        remote_client.manager.post.assert_not_called()

    def test_forced_refresh_after_max_delay(self):
        # GIVEN a remote client whose token was refreshed MAX_REFRESH_DELAY ago
        remote_client = get_remote_client()
        remote_client.remote_token_last_update = datetime.now() - timedelta(seconds=MAX_REFRESH_DELAY)

        # WHEN a forced refresh is requested
        refreshed = remote_client._refresh_remote_token(force=True)

        # THEN the token is actually refreshed
        self.assertTrue(refreshed)
        remote_client.manager.refresh_token_now.assert_called_once()
        remote_client.manager.post.assert_called_once()
        self.assertEqual("access_token", remote_client.remoteCredentials.access_token)
        self.assertIsNotNone(remote_client.remote_token_last_update)

    def test_first_forced_refresh_is_not_throttled(self):
        # GIVEN a remote client which never refreshed its token
        remote_client = get_remote_client()
        self.assertIsNone(remote_client.remote_token_last_update)

        # WHEN a forced refresh is requested
        refreshed = remote_client._refresh_remote_token(force=True)

        # THEN the token is refreshed
        self.assertTrue(refreshed)
        remote_client.manager.refresh_token_now.assert_called_once()

    def test_unforced_refresh_respects_ttl(self):
        # GIVEN a remote client whose remote credentials were updated less than MQTT_TOKEN_TTL ago
        remote_client = get_remote_client()
        remote_client.remoteCredentials.refresh_token = "new_remote_refresh_token"

        # WHEN a refresh is requested without force
        refreshed = remote_client._refresh_remote_token()

        # THEN no refresh is performed
        self.assertTrue(refreshed)
        remote_client.manager.refresh_token_now.assert_not_called()

    def test_unforced_refresh_reuses_a_just_refreshed_token(self):
        # GIVEN a remote client whose remote credentials were never updated (PSA doesn't
        # return a new remote refresh token on a refresh, so last_update stays at the epoch)
        remote_client = get_remote_client()

        # WHEN two publishes ask for a refresh within MQTT_TOKEN_TTL
        self.assertTrue(remote_client._refresh_remote_token())
        self.assertTrue(remote_client._refresh_remote_token())

        # THEN only the first one refreshes: each refresh used to call refresh_token_now()
        # and burn its rate limit (6 per 30 min), so a wakeup per minute blocked commands
        remote_client.manager.refresh_token_now.assert_called_once()
        remote_client.manager.post.assert_called_once()

    def test_unforced_refresh_after_ttl(self):
        # GIVEN a remote client whose token was refreshed MQTT_TOKEN_TTL ago
        remote_client = get_remote_client()
        remote_client.remote_token_last_update = datetime.now() - timedelta(seconds=MQTT_TOKEN_TTL)

        # WHEN a refresh is requested without force
        refreshed = remote_client._refresh_remote_token()

        # THEN the token is refreshed
        self.assertTrue(refreshed)
        remote_client.manager.refresh_token_now.assert_called_once()

    def test_refresh_failure_is_recorded_until_next_success(self):
        # GIVEN a remote client whose account token refresh is rate limited
        remote_client = get_remote_client()
        remote_client.manager.refresh_token_now.side_effect = RateLimitException("refresh_token_now")

        # WHEN the token is refreshed
        refreshed = remote_client._refresh_remote_token(force=True)

        # THEN the failure is kept so the settings page can show it, though mqtt may still be connected
        self.assertFalse(refreshed)
        self.assertEqual(TOKEN_RATE_LIMITED, remote_client.token_error)

        # WHEN a later refresh succeeds
        remote_client.manager.refresh_token_now.side_effect = None
        self.assertTrue(remote_client._refresh_remote_token(force=True))

        # THEN the error is cleared
        self.assertIsNone(remote_client.token_error)

    def failing_client(self, **refresh):
        remote_client = get_remote_client()
        remote_client.manager.refresh_token_now.configure_mock(**refresh)
        self.addCleanup(lambda: remote_client.update_thread and remote_client.update_thread.cancel())
        return remote_client

    def test_each_failure_is_named(self):
        cases = [(TOKEN_ACCOUNT_FAILED, {"return_value": False}),
                 (TOKEN_ACCOUNT_FAILED, {"side_effect": OAuthError(HTTPStatus.BAD_GATEWAY, "server_error", "x")}),
                 (TOKEN_REMOTE_FAILED, {"side_effect": ConfigException("x")})]
        for error, refresh in cases:
            with self.subTest(error=error, refresh=refresh):
                remote_client = self.failing_client(**refresh)
                self.assertFalse(remote_client._refresh_remote_token(force=True))
                self.assertEqual(error, remote_client.token_error)

    def test_waiting_for_a_new_login_is_an_account_failure(self):
        # publishes fail here without even trying, so the socket alone would still say "connected"
        remote_client = self.failing_client()
        remote_client.manager.needs_login = True
        remote_client.manager.rejected_grant_retry_due.return_value = False
        self.assertFalse(remote_client._refresh_remote_token(force=True))
        self.assertEqual(TOKEN_ACCOUNT_FAILED, remote_client.token_error)

    def test_failure_retries_soon_and_for_real(self):
        # GIVEN a forced refresh failed while the next keepalive is hours away
        remote_client = self.failing_client(side_effect=RateLimitException("refresh_token_now"))
        remote_client.remote_token_last_update = datetime.now() - timedelta(seconds=MAX_REFRESH_DELAY + 1)
        remote_client._refresh_remote_token(force=True)

        # THEN the retry the settings page tells about is scheduled
        self.assertEqual(KEEPALIVE_RETRY_PERIOD, remote_client.update_thread.interval)

        # WHEN it runs, the token still being within its TTL
        remote_client.manager.refresh_token_now.side_effect = None
        remote_client._RemoteClient__keep_mqtt()

        # THEN it really refreshes, the error is gone and the regular cycle is back
        remote_client.manager.post.assert_called_once()
        self.assertIsNone(remote_client.token_error)
        self.assertEqual(KEEPALIVE_PERIOD, remote_client.update_thread.interval)

    def test_accepted_connection_clears_the_error(self):
        # the refresh of a disconnect fails during an outage, then paho reconnects with the same token
        remote_client = self.failing_client(return_value=False)
        remote_client._refresh_remote_token(force=True)
        client = MagicMock()

        remote_client._RemoteClient__on_mqtt_connect(client, None, None, MagicMock(is_failure=True))
        self.assertEqual(TOKEN_ACCOUNT_FAILED, remote_client.token_error)

        remote_client._RemoteClient__on_mqtt_connect(client, None, None, MagicMock(is_failure=False))
        self.assertIsNone(remote_client.token_error)

    def test_start_clears_the_error(self):
        # reconnect or a new OTP: the token is within its TTL, so the refresh in start() doesn't clear it
        remote_client = self.failing_client()
        remote_client.token_error = TOKEN_REMOTE_FAILED
        remote_client.remote_token_last_update = datetime.now()
        with mock.patch.object(remote_client, "load_otp", return_value=False):
            remote_client.start()
        self.assertIsNone(remote_client.token_error)

    def test_keepalive_period_is_well_under_token_lifetime(self):
        # Regression test for Sentry issue 150805222 / the daily invalid_grant: the remote
        # refresh token lives ~24h and __keep_mqtt is the only periodic refresh on an idle
        # car. The old 24h keepalive always fired a few seconds too late and killed the
        # chain daily.
        self.assertLess(KEEPALIVE_PERIOD, 24 * 3600 / 2,
                        "keepalive must run at least twice per remote token lifetime")

    def test_keep_mqtt_schedules_next_cycle(self):
        # GIVEN a remote client with at least one vehicle
        remote_client = get_remote_client()
        remote_client.vehicles_list = ["VR3UHZKX"]

        # WHEN the keepalive runs
        remote_client._RemoteClient__keep_mqtt()

        # THEN the next keepalive cycle is scheduled at KEEPALIVE_PERIOD
        self.assertIsNotNone(remote_client.update_thread)
        self.assertAlmostEqual(remote_client.update_thread.interval, KEEPALIVE_PERIOD)
        remote_client.update_thread.cancel()
        remote_client.update_thread = None

    def test_otp_failure_is_contained_in_refresh(self):
        # GIVEN a remote client whose remote refresh token is broken and whose otp
        # session fails to produce a code (e.g. NOK:ERR1 from the inwebo server)
        remote_client = get_remote_client()
        remote_client.manager.post.return_value.json.return_value = {"error": "invalid_grant"}
        remote_client.otp = MagicMock()
        remote_client.otp.get_otp_code.side_effect = ConfigException("NOK:ERR1")

        # WHEN the remote token is refreshed (no saved otp.bin on disk)
        with mock.patch("psa_car_controller.psa.RemoteClient.load_otp", return_value=None):
            refreshed = remote_client._refresh_remote_token()

        # THEN the ConfigException doesn't escape _refresh_remote_token (it used to
        # propagate up through publish/wakeup into unrelated exception handlers)
        self.assertFalse(refreshed)

    def test_get_otp_code_reload_recovers_from_config_exception(self):
        # GIVEN a remote client whose otp session is desynchronized from the server
        remote_client = get_remote_client()
        broken_session = MagicMock()
        broken_session.get_otp_code.side_effect = ConfigException("NOK:ERR1")
        remote_client.otp = broken_session

        # AND a saved session on disk that works
        saved_session = MagicMock()
        saved_session.get_otp_code.return_value = "otp_code"
        with mock.patch("psa_car_controller.psa.RemoteClient.load_otp", return_value=saved_session), \
                mock.patch("psa_car_controller.psa.RemoteClient.save_otp"):
            # WHEN get_otp_code is called
            otp_code = remote_client.get_otp_code()

        # THEN the session is reloaded from disk and the code is produced
        self.assertEqual("otp_code", otp_code)
        saved_session.get_otp_code.assert_called_once()

    def test_get_otp_code_raises_when_no_saved_session(self):
        # GIVEN a remote client whose otp session fails and no valid otp.bin on disk
        remote_client = get_remote_client()
        remote_client.otp = MagicMock()
        remote_client.otp.get_otp_code.side_effect = ConfigException("NOK:ERR1")

        # WHEN get_otp_code is called THEN the original ConfigException is re-raised
        # (the old code raised a misleading AttributeError on self.otp=None instead)
        with self.assertRaises(ConfigException):
            with mock.patch("psa_car_controller.psa.RemoteClient.load_otp", return_value=None):
                remote_client.get_otp_code()

    def test_no_refresh_while_waiting_for_a_new_login(self):
        # GIVEN a remote client whose PSA refresh token was just rejected
        remote_client = get_remote_client()
        remote_client.manager.needs_login = True
        remote_client.manager.rejected_grant_retry_due.return_value = False

        # WHEN a forced refresh is requested
        refreshed = remote_client._refresh_remote_token(force=True)

        # THEN PSA isn't asked again with the rejected token
        self.assertFalse(refreshed)
        remote_client.manager.refresh_token_now.assert_not_called()


if __name__ == '__main__':
    unittest.main()
