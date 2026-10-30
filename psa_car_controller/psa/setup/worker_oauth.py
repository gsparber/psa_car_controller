"""Login through a login service the user hosts: stelloauth's POST /worker, or a service with the same API."""
import ipaddress
import logging
from typing import Optional
from urllib.parse import quote, quote_plus, urlparse

import requests

from psa_car_controller.common.sensitive_filter import MASK, sensitive_filter

logger = logging.getLogger(__name__)

# the web UI of stelloauth builds its own URL without our PKCE challenge, its code would be refused
WORKER_PATH = "/worker"
CONNECT_TIMEOUT_S = 10
WORKER_TIMEOUT_S = 300
MAX_MESSAGE_LENGTH = 300
HTTP_OK = 200
HTTP_TOO_MANY_REQUESTS = 429
LOCAL_SUFFIXES = (".localhost", ".local", ".lan", ".internal", ".home.arpa")


class WorkerLoginError(Exception):
    """The login service gave no code. The message has no credentials and can be shown to the user."""


def worker_endpoint(worker_url: str) -> str:
    url = worker_url.strip().rstrip("/")
    return url if url.endswith(WORKER_PATH) else url + WORKER_PATH


def worker_host(worker_url: str) -> Optional[str]:
    """The host alone, to show or log: the URL may hold a username and password."""
    try:
        return urlparse(worker_url.strip()).hostname
    except ValueError:
        return None


def _is_local_host(host: str) -> bool:
    host = host.lower().rstrip(".")
    if host == "localhost" or host.endswith(LOCAL_SUFFIXES):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # no dot: a container of the same compose project or a machine of the network
        return "." not in host
    return address.is_loopback or address.is_private or address.is_link_local


def is_insecure_worker_url(worker_url: str) -> bool:
    """True when credentials would cross a foreign network unencrypted: http:// to a non-local address."""
    host = worker_host(worker_url)
    return worker_url.strip().lower().startswith("http://") and not (host and _is_local_host(host))


def _clean_message(message, email: str, password: str) -> str:
    """Makes a text of the service safe to show and log, it could repeat what it received."""
    text = " ".join(str(message).split())
    for secret in (password, email):
        if secret:
            for value in (secret, quote(secret), quote_plus(secret)):
                text = text.replace(value, MASK)
    return sensitive_filter.mask(text)[:MAX_MESSAGE_LENGTH]


def _read_answer(response) -> dict:
    try:
        answer = response.json()
    except ValueError:
        return {}
    return answer if isinstance(answer, dict) else {}


def get_oauth_code_from_worker(worker_url: str, auth_url: str, email: str, password: str) -> str:
    """Asks the service to log in on auth_url (it holds our PKCE challenge) and returns the authorization code."""
    if not worker_url.strip().lower().startswith(("http://", "https://")):
        raise WorkerLoginError("The login service URL must start with https:// or http://")
    host = worker_host(worker_url)
    if is_insecure_worker_url(worker_url):
        logger.warning("Sending the account credentials to the login service %s without encryption, use https", host)
    logger.info("Logging in through the login service %s", host)
    try:
        # no redirects: a 307/308 would send the credentials to wherever it points to
        response = requests.post(worker_endpoint(worker_url),
                                 json={"url": auth_url, "email": email, "password": password},
                                 timeout=(CONNECT_TIMEOUT_S, WORKER_TIMEOUT_S), allow_redirects=False)
    except requests.exceptions.Timeout:
        raise WorkerLoginError(f"The login service {host} did not answer in time") from None
    except requests.exceptions.RequestException as e:
        # only the type: the exception holds the request and its text could hold the URL
        raise WorkerLoginError(f"Can't reach the login service {host} ({type(e).__name__})") from None

    answer = _read_answer(response)
    code = answer.get("code")
    if response.status_code == HTTP_OK and code and isinstance(code, str):
        return code
    message = answer.get("message")
    if response.status_code == HTTP_TOO_MANY_REQUESTS:
        reason = "rate limit of the service reached, try again later"
        if message:
            reason += ": " + _clean_message(message, email, password)
    elif message:
        reason = _clean_message(message, email, password)
    else:
        reason = "unexpected answer"
    raise WorkerLoginError(f"Login service {host}: {reason} (HTTP {response.status_code})")
