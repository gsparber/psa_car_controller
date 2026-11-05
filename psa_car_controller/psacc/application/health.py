from datetime import datetime, timezone
from os import environ
from typing import Optional, Tuple

from psa_car_controller.common.sensitive_filter import mask_value
from psa_car_controller.psa.RemoteClient import RemoteClient
from psa_car_controller.psa.oauth import AUTH_LOGIN_REQUIRED, AUTH_TEMPORARY, AUTH_TEMPORARY_MESSAGE
from psa_car_controller.psacc.model.car_status import status_age_seconds

# paho retries after 1, 2, 4, 8... s (max 120 s), so this is reached about 30 s after
# the refusals start: how long until a restart is up to the healthcheck settings
DEFAULT_MAX_MQTT_AUTH_FAILURES = 5
# seconds since the last answer of PSA with a status, while the refresh loop runs: 3 refreshes at the default rate
DEFAULT_MAX_STATUS_AGE = 900
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


def get_max_status_age() -> int:
    return int(environ.get("PSACC_HEALTH_MAX_STATUS_AGE", DEFAULT_MAX_STATUS_AGE))


def _iso(date: Optional[datetime]) -> Optional[str]:
    return date.isoformat() if date else None


def _age(date: Optional[datetime], now: datetime) -> Optional[int]:
    return max(0, int((now - date).total_seconds())) if date else None


def _vehicle_supply(psacc, car, now: datetime) -> dict:
    requests = psacc.status_requests(car.vin)
    return {
        "vin": mask_value(car.vin),
        # the request: an answer with the same old status still counts, the car had nothing new to send
        "last_status_attempt": _iso(requests.last_attempt),
        "last_status_success": _iso(requests.last_success),
        "status_success_age_seconds": _age(requests.last_success, now),
        # the data: a parked car sends nothing new, so an old date alone is no error
        "data_age_seconds": status_age_seconds(car.status, now) if car.status is not None else None,
        "consecutive_failures": car.status_failures,
        "last_error": requests.last_error,
        "status_outdated": car.status_outdated,
    }


def get_data_supply(psacc, max_status_age: int, now: Optional[datetime] = None) -> dict:
    """How the statuses of the cars come in, from what the status requests recorded: no PSA call.

    "degraded" is a hint for monitoring, it never makes /health unhealthy: restarting doesn't help while PSA fails.
    """
    now = now or datetime.now(timezone.utc)
    retry_in = psacc.rest_retry_in()
    next_refresh_in = psacc.next_refresh_in()
    vehicles = [_vehicle_supply(psacc, car, now) for car in psacc.vehicles_list]
    problems = []
    if psacc.is_rate_limited():
        problems.append("rate_limited")
    if any(vehicle["status_outdated"] for vehicle in vehicles):
        problems.append("status_requests_failing")
    # only while the refresh loop runs: without it, nothing asks PSA unless a client does
    if next_refresh_in is not None and any(
            vehicle["last_status_attempt"] is not None and (vehicle["status_success_age_seconds"] is None or
                                                            vehicle["status_success_age_seconds"] > max_status_age)
            for vehicle in vehicles):
        problems.append("no_recent_status")
    return {
        "data_supply": "degraded" if problems else "ok",
        "data_supply_problems": problems,
        "rate_limit": {
            "active": psacc.is_rate_limited(),
            # outside of the refresh loop, PSA isn't asked before this, the stored status is the answer
            "retry_in_seconds": int(retry_in),
            "next_refresh_in_seconds": None if next_refresh_in is None else int(next_refresh_in),
        },
        "vehicles": vehicles,
    }


# pylint: disable-next=too-many-arguments,too-many-positional-arguments
def get_health(remote_client: Optional[RemoteClient], remote_control: bool,
               max_auth_failures: int, api_authenticated: bool = True,
               api_auth_problem: Optional[str] = None, psacc=None) -> Tuple[dict, int]:
    """Return the /health body and HTTP status.

    Only looks at state the mqtt callbacks already recorded: no PSA call, so it is cheap to poll.
    Unhealthy (503) once the broker refused the remote credentials max_auth_failures times in a row.
    api_authenticated is reported but never makes it unhealthy: a rejected refresh token needs a new
    login from the config web page, which restarting the container can't do. Neither does a temporary
    failure need a restart: api_auth_problem tells the two apart.
    """
    auth = _api_auth_fields(api_authenticated, api_auth_problem)
    if psacc is not None:
        auth.update(get_data_supply(psacc, get_max_status_age()))
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
