from os import environ
from typing import Optional, Tuple

from psa_car_controller.psa.RemoteClient import RemoteClient
from psa_car_controller.psa.oauth import AUTH_LOGIN_REQUIRED, AUTH_TEMPORARY, AUTH_TEMPORARY_MESSAGE

# paho retries after 1, 2, 4, 8... s (max 120 s), so this is reached about 30 s after
# the refusals start: how long until a restart is up to the healthcheck settings
DEFAULT_MAX_MQTT_AUTH_FAILURES = 5
API_AUTH_MESSAGE = {
    AUTH_TEMPORARY: AUTH_TEMPORARY_MESSAGE,
    AUTH_LOGIN_REQUIRED: "No valid login for the PSA API, please log in again from the config web page",
}


def get_max_mqtt_auth_failures() -> int:
    return int(environ.get("PSACC_HEALTH_MAX_MQTT_AUTH_FAILURES", DEFAULT_MAX_MQTT_AUTH_FAILURES))


def _api_auth_fields(api_authenticated: bool, api_auth_problem: Optional[str]) -> dict:
    fields = {"api_authenticated": api_authenticated}
    if api_auth_problem is not None:
        fields["api_auth_problem"] = api_auth_problem
        fields["api_auth_message"] = API_AUTH_MESSAGE[api_auth_problem]
    return fields


def get_health(remote_client: Optional[RemoteClient], remote_control: bool,
               max_auth_failures: int, api_authenticated: bool = True,
               api_auth_problem: Optional[str] = None) -> Tuple[dict, int]:
    """Return the /health body and HTTP status.

    Only looks at state the mqtt callbacks already recorded: no PSA call, so it is cheap to poll.
    Unhealthy (503) once the broker refused the remote credentials max_auth_failures times in a row.
    api_authenticated is reported but never makes it unhealthy: a rejected refresh token needs a new
    login from the config web page, which restarting the container can't do. Neither does a temporary
    failure need a restart: api_auth_problem tells the two apart.
    """
    auth = _api_auth_fields(api_authenticated, api_auth_problem)
    if not remote_control or remote_client is None:
        return {**auth, "remote_control": False}, 200
    last_connect = remote_client.mqtt_last_connect
    body = {
        **auth,
        "remote_control": True,
        "mqtt_connected": remote_client.is_mqtt_connected(),
        "consecutive_auth_failures": remote_client.mqtt_auth_failures,
        "last_connect": last_connect.isoformat() if last_connect else None,
    }
    status = 503 if remote_client.mqtt_auth_failures >= max_auth_failures else 200
    return body, status
