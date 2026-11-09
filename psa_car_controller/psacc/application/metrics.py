"""/metrics in the Prometheus text format: what /health knows, and the counters of the status requests.

Only state PSACC already has, so a scrape never asks PSA and writes nothing. Counters start at 0 with each start of
the app, Prometheus takes that as a reset. A value that isn't known (no status yet, refresh loop not running) is left
out instead of shown as 0, which would read as fresh. The VIN label is masked but for its last 4 characters.
"""
from datetime import datetime, timezone
from typing import Iterable, List, Optional, Tuple

from psa_car_controller.common.sensitive_filter import mask_value
from psa_car_controller.psacc.model.car_status import status_age_seconds, status_updated_at

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

Sample = Tuple[dict, float]


def _escape(value) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _number(value) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    return repr(float(value))


class Metrics:
    def __init__(self):
        self.lines: List[str] = []

    def add(self, name: str, kind: str, description: str, samples: Iterable[Sample]):
        samples = [(labels, value) for labels, value in samples if value is not None]
        if not samples:
            return
        self.lines.append(f"# HELP {name} {description}")
        self.lines.append(f"# TYPE {name} {kind}")
        for labels, value in samples:
            text = ",".join(f'{key}="{_escape(label)}"' for key, label in labels.items())
            self.lines.append(f"{name}{{{text}}} {_number(value)}" if text else f"{name} {_number(value)}")

    def text(self) -> str:
        return "\n".join(self.lines) + "\n"


def _timestamp(date: Optional[datetime]) -> Optional[float]:
    if not isinstance(date, datetime):
        return None
    if date.tzinfo is None:
        date = date.replace(tzinfo=timezone.utc)
    return date.timestamp()


def _electric(status):
    for energy in getattr(status, "energy", None) or []:
        if getattr(energy, "type", None) == "Electric":
            return energy
    return None


def _vehicle_metrics(metrics: Metrics, psacc, now: datetime):
    cars = [(car, {"vin": mask_value(car.vin)}, psacc.status_requests(car.vin)) for car in psacc.vehicles_list]
    metrics.add("psacc_status_last_success_timestamp_seconds", "gauge",
                "When PSA last answered with a status of the car", [
                    (labels, _timestamp(requests.last_success)) for _, labels, requests in cars])
    metrics.add("psacc_status_success_age_seconds", "gauge", "Seconds since PSA last answered with a status", [
        (labels, (now - requests.last_success).total_seconds() if requests.last_success else None)
        for _, labels, requests in cars])
    metrics.add("psacc_status_data_timestamp_seconds", "gauge",
                "When the car sent the data of its status: a parked car sends nothing new for days", [
                    (labels, _timestamp(status_updated_at(car.status)) if car.status is not None else None)
                    for car, labels, _ in cars])
    metrics.add("psacc_status_data_age_seconds", "gauge", "Seconds since the car sent the data of its status", [
        (labels, status_age_seconds(car.status, now) if car.status is not None else None) for car, labels, _ in cars])
    metrics.add("psacc_status_consecutive_failures", "gauge", "Failed status requests in a row", [
        (labels, car.status_failures) for car, labels, _ in cars])
    metrics.add("psacc_status_outdated", "gauge", "1 when the status requests failed too often in a row", [
        (labels, car.status_outdated) for car, labels, _ in cars])
    metrics.add("psacc_status_requests_total", "counter", "Answers of PSA to status requests, by result", [
        ({**labels, "result": result}, count) for _, labels, requests in cars
        for result, count in sorted(requests.requests.items())])
    metrics.add("psacc_status_reads_total", "counter",
                "Status reads that didn't ask PSA: cache_hit, cache_miss (from_cache=1) and rate_limited", [
                    ({**labels, "result": result}, count) for _, labels, requests in cars
                    for result, count in sorted(requests.reads.items())])
    electric = [(labels, _electric(car.status)) for car, labels, _ in cars]
    metrics.add("psacc_battery_level_percent", "gauge", "Battery level in the stored status", [
        (labels, getattr(energy, "level", None)) for labels, energy in electric])
    metrics.add("psacc_charging", "gauge", "1 while the stored status says the car charges", [
        (labels, getattr(getattr(energy, "charging", None), "status", None) == "InProgress")
        for labels, energy in electric if energy is not None and getattr(energy, "charging", None) is not None])


def get_metrics(psacc, remote_client, remote_control: bool, api_authenticated: bool, now=None) -> str:
    now = now or datetime.now(timezone.utc)
    metrics = Metrics()
    metrics.add("psacc_api_authenticated", "gauge", "1 while there is a valid login for the PSA API",
                [({}, api_authenticated)])
    if psacc is not None:
        metrics.add("psacc_rate_limited", "gauge", "1 while PSA isn't asked because it answered 429",
                    [({}, psacc.is_rate_limited())])
        metrics.add("psacc_rate_limit_retry_seconds", "gauge",
                    "Seconds until PSA is asked again outside of the refresh loop", [({}, psacc.rest_retry_in())])
        metrics.add("psacc_next_refresh_seconds", "gauge", "Seconds until the refresh loop asks PSA",
                    [({}, psacc.next_refresh_in())])
        _vehicle_metrics(metrics, psacc, now)
    metrics.add("psacc_remote_control", "gauge", "1 when the remote control (MQTT) is on", [({}, remote_control)])
    if remote_control and remote_client is not None:
        metrics.add("psacc_mqtt_connected", "gauge", "1 while connected to the MQTT broker of PSA",
                    [({}, remote_client.is_mqtt_connected())])
        metrics.add("psacc_mqtt_auth_failures", "gauge", "Refused MQTT connections in a row",
                    [({}, remote_client.mqtt_auth_failures)])
    return metrics.text()
