import logging
import hashlib
import json
import secrets
import base64
import random
import re
import string
import threading
import time
from typing import Tuple

from http import HTTPStatus
from typing import Optional
from urllib.parse import parse_qs, unquote, urlsplit

from oauth2_client.credentials_manager import CredentialManager, OAuthError, ServiceInformation
from requests import Response, RequestException

from psa_car_controller.common.utils import rate_limit, RateLimitException, TIMEOUT_IN_S
from psa_car_controller.psa import connected_car_api
from psa_car_controller.psa.connected_car_api import ApiClient
from psa_car_controller.psa.connected_car_api.rest import ApiException
from psa_car_controller.psa.status_schema import STATUS_RESPONSE_TYPE, prepare_status

logger = logging.getLogger(__name__)

# PSA has answered invalid_grant during outages of its own, so a rejected refresh token is
# tried again now and then instead of being given up on, but not on every API call.
REJECTED_GRANT_RETRY_DELAY = 1800
# the access token is refreshed this long before it expires, so API calls don't run into a 401
REFRESH_BEFORE_EXPIRY = 300
# never schedule the next refresh sooner than this, whatever expires_in says
MIN_SCHEDULED_REFRESH_DELAY = 30
# delays between scheduled refresh attempts that failed without PSA rejecting the token
# (unreachable server, rate limit), plus up to REFRESH_RETRY_JITTER of random jitter
REFRESH_RETRY_DELAYS = (60, 120, 300, 600, 900)
REFRESH_RETRY_JITTER = 0.1
# what a pasted authorization code tends to come wrapped in
PASTE_WRAPPING = string.whitespace + "\"'`"
# text around a URL would end up in its last query value, it goes through CODE_IN_TEXT instead
UNQUOTED_URL_BREAK = re.compile(r"""[\s"'`]""")
CODE_IN_TEXT = re.compile(r"""(?<![\w-])code["']?\s*[:=]\s*["']?([^"'`&\s,;}#)>]+)""", re.IGNORECASE)
# a refresh replaces the refresh token, so a second tool using the same one gets it rejected
SHARED_TOKEN_HINT = "Is another tool running with the same token? Each tool needs its own login."
# the error of a refresh without a stored refresh token, the library uses the same one for no session
NO_TOKEN_ERROR = "no_token"


def rejected_grant_hint(error: Optional[Exception]) -> str:
    """What to add to the message about a failed refresh, empty unless PSA rejected the grant."""
    if isinstance(error, OAuthError) and error.error == "invalid_grant":
        return " " + SHARED_TOKEN_HINT
    return ""


def _needs_new_login(error: OAuthError) -> bool:
    # Only an explicit answer about the token itself. Anything else, even a 401, can be an outage
    # or a wrong client: the token is probably fine. The status PSA uses is unverified, so ignored.
    return error.error in ("invalid_grant", NO_TOKEN_ERROR)


def refresh_can_recover(error: Optional[Exception]) -> bool:
    """True if trying the same refresh token again later makes sense after this refresh error."""
    if isinstance(error, OAuthError):
        return not _needs_new_login(error)
    return isinstance(error, RequestException)


def refresh_retry_delay(failures: int) -> float:
    """Delay before the next attempt after `failures` failed scheduled refreshes in a row."""
    delay = REFRESH_RETRY_DELAYS[min(max(failures, 1), len(REFRESH_RETRY_DELAYS)) - 1]
    return delay * (1 + random.uniform(0, REFRESH_RETRY_JITTER))  # nosec B311: not for security


def generate_sha256_pkce(length: int) -> Tuple[str, str]:
    if not (43 <= length <= 128):
        raise ValueError("Invalid length: %d" % length)
    verifier = secrets.token_urlsafe(length)
    encoded = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode('ascii')).digest())
    challenge = encoded.decode('ascii')[:-1]
    return verifier, challenge


def _code_from_json(text: str) -> Optional[str]:
    if not text.startswith("{"):
        return None
    try:
        code = json.loads(text).get("code")
    except ValueError:
        return None
    # a number here is an HTTP status from a login worker, not a code
    return code if isinstance(code, str) else None


def _code_from_url(text: str) -> Optional[str]:
    if UNQUOTED_URL_BREAK.search(text):
        return None
    try:
        url = urlsplit(text)
    except ValueError:
        return None
    for part in (url.query, url.fragment):
        codes = parse_qs(part).get("code")
        if codes:
            return codes[0]
    return None


def _code_from_text(text: str) -> Optional[str]:
    match = CODE_IN_TEXT.search(text)
    return unquote(match.group(1)) if match else None


def extract_code(raw: Optional[str]) -> str:
    """Find the authorization code in what a user pasted: the code itself, the redirect URL
    (maybe URL-encoded as a whole), a JSON answer or a line of browser console text."""
    text = (raw or "").strip(PASTE_WRAPPING)
    decoded = unquote(text)
    for candidate in (text, decoded):
        code = _code_from_json(candidate) or _code_from_url(candidate) or _code_from_text(candidate)
        if code:
            return code
    return decoded


class OpenIdCredentialManager(CredentialManager):
    @staticmethod
    def create(service_information: ServiceInformation, scheme: str, country_code: str,
               proxies: Optional[dict] = None):
        manager = OpenIdCredentialManager(service_information, proxies)
        manager.redirect_uri = scheme + "://oauth2redirect/" + country_code.lower()
        return manager

    def __init__(self, service_information: ServiceInformation, proxies: Optional[dict] = None):
        super().__init__(service_information, proxies)
        self.refresh_callbacks = []
        self.code_verifier = None
        self.redirect_uri = None
        self.last_refresh_error: Optional[Exception] = None
        # monotonic time of the last refresh PSA rejected, None while the grant is fine
        self.grant_rejected_at: Optional[float] = None
        # the refresh token PSA rejected: it is only sent again by the rare retry above
        self.rejected_refresh_token: Optional[str] = None
        # one refresh at a time: the refresh thread, the remote client and web requests all
        # refresh on a 401, and each extra refresh burns the rate limit (6 per 30 min)
        self._refresh_lock = threading.RLock()
        # set under the lock: a request made by a refresh callback must not refresh again
        self._refreshing = False
        # monotonic time the access token expires, None if PSA didn't say
        self.token_expires_at: Optional[float] = None
        self._scheduled_refresh = False
        self._scheduled_refresh_failures = 0
        self._refresh_timer: Optional[threading.Timer] = None
        self._timer_lock = threading.Lock()

    def _grant_password_request_realm(self, login: str, password: str, realm: str) -> dict:
        return {"grant_type": 'password', "username": login, "scope": ' '.join(self.service_information.scopes),
                "password": password, "realm": realm}

    def generate_redirect_url(self):
        self.code_verifier, code_challenge = generate_sha256_pkce(64)
        return self.generate_authorize_url(self.redirect_uri, secrets.token_urlsafe(16),
                                           code_challenge=code_challenge, code_challenge_method="S256")

    def connect_with_code(self, code: Optional[str]):
        # the message must not repeat the input: users paste a live code or a whole URL here
        code = extract_code(code)
        if len(code) != 36:
            raise ValueError(f"Invalid code length: {len(code)} (expected 36)")
        self._token_request({"grant_type": 'authorization_code', "code": code,
                             "redirect_uri": self.redirect_uri, "code_verifier": self.code_verifier},
                            False)

    @staticmethod
    def _is_token_expired(response: Response) -> bool:
        if response.status_code == HTTPStatus.UNAUTHORIZED.value:
            logger.info("token expired, renew")
            try:
                json_data = response.json()
                return json_data.get('moreInformation') == 'Token is invalid'
            except ValueError:
                return False
        else:
            return False

    @property
    def access_token(self):
        return self._access_token

    @property
    def needs_login(self) -> bool:
        """True once PSA rejected the refresh token: only a new login can fix it."""
        return self.grant_rejected_at is not None

    def rejected_grant_retry_due(self) -> bool:
        return self.needs_login and time.monotonic() - self.grant_rejected_at >= REJECTED_GRANT_RETRY_DELAY

    def _refresh_token(self):
        # not the library's: on any 401 it forgets the refresh token, which only a new login gives back
        if not self.refresh_token:
            raise OAuthError(HTTPStatus.UNAUTHORIZED, NO_TOKEN_ERROR, "no refresh token stored")
        self._token_request(self._grant_refresh_token_request(self.refresh_token), False)

    def _process_token_response(self, token_response: dict, refresh_token_mandatory: bool):
        if self.refresh_token and not token_response.get("refresh_token"):
            # the library would store None: an answer without a new one leaves the old one valid
            token_response = {**token_response, "refresh_token": self.refresh_token}
        super()._process_token_response(token_response, refresh_token_mandatory)
        # a new login or a successful refresh: the grant is good again
        self.grant_rejected_at = None
        self.rejected_refresh_token = None
        self._scheduled_refresh_failures = 0
        try:
            expires_in = float(token_response.get("expires_in"))
        except (TypeError, ValueError):
            expires_in = None
        self.token_expires_at = time.monotonic() + expires_in if expires_in and expires_in > 0 else None
        if self._scheduled_refresh:
            self._schedule_refresh(self._delay_before_expiry())

    def refresh_token_now(self, rejected_access_token: Optional[str] = None) -> bool:
        """Refresh the access token, once for all the callers that saw it rejected at the same time.

        rejected_access_token is the access token the caller saw fail, the current one by default:
        if another caller replaced it meanwhile, there is nothing left to refresh.
        """
        if rejected_access_token is None:
            rejected_access_token = self.access_token
        with self._refresh_lock:
            if self._refreshing:
                logger.debug("already refreshing the access token")
                return False
            if not self.needs_login and self.access_token is not None \
                    and self.access_token != rejected_access_token:
                logger.debug("access token already refreshed meanwhile")
                return True
            if self.needs_login and self.refresh_token == self.rejected_refresh_token \
                    and not self.rejected_grant_retry_due():
                # don't send PSA a refresh token it already refused, until the rare retry
                logger.debug("The refresh token was rejected, waiting for a new login")
                return False
            self._refreshing = True
            try:
                return self._refresh_with_retries()
            finally:
                self._refreshing = False

    @rate_limit(6, 1800)
    def _refresh_with_retries(self) -> bool:
        # The PSA token endpoint sometimes drops the connection without a
        # response (RemoteDisconnected), leaving the caller with a stale
        # token that then fails every API call with 401 until the next
        # successful refresh. Retry a few times with backoff before giving up.
        self.last_refresh_error = None
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            sent_refresh_token = self.refresh_token
            try:
                self._refresh_token()
                logger.info("access token refreshed")
                for refresh_callback in self.refresh_callbacks:
                    refresh_callback()
                return True
            except OAuthError as e:
                self.last_refresh_error = e
                if not _needs_new_login(e):
                    logger.error("PSA refused to refresh the token, keeping it and trying again later: %s. "
                                 "If this doesn't stop, log in again from the config web page.", e)
                    if self.needs_login:
                        # a rejected token stays rejected, and its retry waits for the next delay
                        self.grant_rejected_at = time.monotonic()
                    break
                # the refresh token is rejected, only a new authentication can fix it
                if self.needs_login:
                    logger.warning("The refresh token is still rejected, waiting for a new login: %s", e)
                else:
                    logger.error("Can't refresh token, please reconnect from the config web page: %s.%s",
                                 e, rejected_grant_hint(e))
                self.grant_rejected_at = time.monotonic()
                self.rejected_refresh_token = sent_refresh_token
                break
            except RequestException as e:
                # the server is unreachable, the token itself might still be fine
                self.last_refresh_error = e
                if attempt < max_attempts:
                    delay = 2 ** attempt
                    logger.warning("Can't reach the PSA server to refresh the token: %s - retrying in %ss "
                                   "(attempt %d/%d)", e, delay, attempt, max_attempts)
                    time.sleep(delay)
                else:
                    logger.error("Can't reach the PSA server to refresh the token: %s", e)
        return False

    def start_scheduled_refresh(self):
        """Refresh the access token REFRESH_BEFORE_EXPIRY seconds before it expires, from now on."""
        self._scheduled_refresh = True
        self._schedule_refresh(self._delay_before_expiry())

    def stop_scheduled_refresh(self):
        self._scheduled_refresh = False
        self._schedule_refresh(None)

    def _delay_before_expiry(self) -> Optional[float]:
        if self.token_expires_at is None:
            # PSA didn't say when it expires: refreshed on the next 401 instead
            return None
        return max(self.token_expires_at - REFRESH_BEFORE_EXPIRY - time.monotonic(), MIN_SCHEDULED_REFRESH_DELAY)

    def _schedule_refresh(self, delay: Optional[float]):
        with self._timer_lock:
            if self._refresh_timer is not None:
                self._refresh_timer.cancel()
                self._refresh_timer = None
            if delay is None or not self._scheduled_refresh:
                return
            logger.debug("next access token refresh in %.0fs", delay)
            self._refresh_timer = threading.Timer(delay, self._scheduled_refresh_run)
            self._refresh_timer.daemon = True
            self._refresh_timer.start()

    def _scheduled_refresh_run(self):
        if not self._scheduled_refresh:
            return
        try:
            refreshed = self.refresh_token_now()
        except RateLimitException as e:
            logger.warning("Can't refresh the access token now: %s", e)
            refreshed = False
        if refreshed:
            # the new token response scheduled the next refresh
            return
        if self.needs_login:
            logger.error("The access token can't be refreshed anymore, please log in again from the config web page")
            return
        self._scheduled_refresh_failures += 1
        self._schedule_refresh(refresh_retry_delay(self._scheduled_refresh_failures))

    def _bearer_request(self, method, url, **kwargs):
        # the library refreshes on its own here, past the lock, the rejected token and the rate limit
        sent_access_token = self.access_token
        response = method(url, **kwargs)
        if self.refresh_token is None or not self._is_token_expired(response):
            return response
        if not self.refresh_token_now(sent_access_token):
            # not the 401: callers would take it for a refused remote token and use up an OTP code
            raise self.last_refresh_error or OAuthError(HTTPStatus.UNAUTHORIZED, "invalid_token",
                                                        "the access token can't be refreshed now")
        return method(url, **kwargs)

    def request(self, method, url, **kwargs):  # pylint: disable=W0221
        if "timeout" not in kwargs:
            kwargs["timeout"] = TIMEOUT_IN_S
        return super().request(method, url, **kwargs)


class Oauth2PSACCApiConfig(connected_car_api.Configuration):
    def __init__(self):
        super().__init__()
        # called with the access token PSA answered 401 to, True once the new one is set
        self.refresh_callback = lambda rejected_access_token: True

    def set_refresh_callback(self, callback):
        self.refresh_callback = callback


class OauthAPIClient(ApiClient):
    def deserialize(self, response, response_type):
        if response_type != STATUS_RESPONSE_TYPE:
            return super().deserialize(response, response_type)
        try:
            data = json.loads(response.data)
        except ValueError:
            data = response.data
        return self._ApiClient__deserialize(prepare_status(data), response_type)  # pylint: disable=no-member

    # pylint: disable=no-member,too-many-arguments,too-many-positional-arguments,too-many-locals
    def call_api(self, resource_path, method,
                 path_params=None, query_params=None, header_params=None,
                 body=None, post_params=None, files=None,
                 response_type=None, auth_settings=None, async_req=None,
                 _return_http_data_only=None, collection_formats=None,
                 _preload_content=True, _request_timeout=None):
        _request_timeout = _request_timeout or TIMEOUT_IN_S
        for attempt in range(0, 2):
            sent_access_token = self.configuration.access_token
            try:
                if not async_req:
                    return self._ApiClient__call_api(resource_path, method,
                                                     path_params, query_params, header_params,
                                                     body, post_params, files,
                                                     response_type, auth_settings,
                                                     _return_http_data_only, collection_formats,
                                                     _preload_content, _request_timeout)
                return self.pool.apply_async(self.__call_api, (resource_path,
                                                               method, path_params, query_params,
                                                               header_params, body,
                                                               post_params, files,
                                                               response_type, auth_settings,
                                                               _return_http_data_only,
                                                               collection_formats,
                                                               _preload_content, _request_timeout))
            except ApiException as e:
                if attempt == 1 or e.reason != 'Unauthorized' or not self.__refresh(sent_access_token):
                    raise e
                # refreshed: send the same request again, once, with the new token
        return None

    def __refresh(self, rejected_access_token: str) -> bool:
        try:
            return self.configuration.refresh_callback(rejected_access_token)
        except RateLimitException as e:
            # the caller gets the 401 rather than an unrelated rate limit error
            logger.warning("Can't refresh the access token now: %s", e)
            return False
