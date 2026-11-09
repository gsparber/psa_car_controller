"""/metrics for Prometheus: the format, what is left out while unknown, the counters, and that a scrape never asks
PSA."""
import re
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from psa_car_controller.psacc.application import psa_client as psa_client_module
from psa_car_controller.psacc.application.metrics import CONTENT_TYPE, get_metrics
from tests.test_collector_api import VIN, Clock, get_client
from tests.test_status_refresh import api_error, get_status
from tests.utils import get_web_view

# name{labels} value, or a HELP/TYPE comment
LINE = re.compile(r'^(# (HELP|TYPE) [a-z_]+ .+|[a-z_]+(\{([a-z_]+="[^"]*",?)+\})? -?[0-9.e+-]+)$')


def parse(text):
    """{name: {labels: value}} of the samples, after checking every line."""
    samples = {}
    for line in text.splitlines():
        assert LINE.match(line), line
        if line.startswith("#"):
            continue
        name_labels, value = line.rsplit(" ", 1)
        name, _, labels = name_labels.partition("{")
        samples.setdefault(name, {})[labels.rstrip("}")] = float(value)
    return samples


class TestMetrics(unittest.TestCase):

    def setUp(self):
        clock = patch.object(psa_client_module, "time", Clock())
        clock.start()
        self.addCleanup(clock.stop)

    @staticmethod
    def metrics(client, remote_client=None, remote_control=False):
        return parse(get_metrics(client, remote_client, remote_control, True))

    def test_nothing_known_yet(self):
        samples = self.metrics(get_client())
        self.assertEqual({"": 1}, samples["psacc_api_authenticated"])
        self.assertEqual({"": 0}, samples["psacc_rate_limited"])
        # unknown is left out, a 0 would read as fresh
        for name in ("psacc_status_success_age_seconds", "psacc_status_data_age_seconds", "psacc_battery_level_percent",
                     "psacc_next_refresh_seconds", "psacc_status_requests_total"):
            self.assertNotIn(name, samples)

    def test_status_and_counters(self):
        client = get_client(get_status("2026-01-01T00:00:00Z", level=63), api_error(500), api_error(500))
        client.get_vehicle_info(VIN)
        client.get_vehicle_info(VIN)
        client.get_vehicle_info(VIN, cache=True)
        samples = self.metrics(client)
        vin = 'vin="***0000"'
        self.assertEqual(63, samples["psacc_battery_level_percent"][vin])
        self.assertLess(samples["psacc_status_success_age_seconds"][vin], 60)
        self.assertGreater(samples["psacc_status_data_age_seconds"][vin], 3600)
        self.assertEqual(1, samples["psacc_status_requests_total"][vin + ',result="success"'])
        self.assertEqual(2, samples["psacc_status_requests_total"][vin + ',result="http_500"'])
        self.assertEqual(1, samples["psacc_status_reads_total"][vin + ',result="cache_hit"'])
        self.assertEqual(1, samples["psacc_status_consecutive_failures"][vin])
        self.assertEqual(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp(),
                         samples["psacc_status_data_timestamp_seconds"][vin])

    def test_rate_limit(self):
        client = get_client(api_error(429))
        client.info_refresh_rate = 300
        client.get_vehicle_info(VIN)
        client.get_vehicle_info(VIN)
        samples = self.metrics(client)
        self.assertEqual({"": 1}, samples["psacc_rate_limited"])
        self.assertEqual({"": 600}, samples["psacc_rate_limit_retry_seconds"])
        self.assertEqual(1, samples["psacc_status_reads_total"]['vin="***0000",result="rate_limited"'])

    def test_mqtt(self):
        remote_client = MagicMock(mqtt_auth_failures=2)
        remote_client.is_mqtt_connected.return_value = False
        samples = self.metrics(get_client(), remote_client, remote_control=True)
        self.assertEqual({"": 0}, samples["psacc_mqtt_connected"])
        self.assertEqual({"": 2}, samples["psacc_mqtt_auth_failures"])
        self.assertNotIn("psacc_mqtt_connected", self.metrics(get_client()))

    def test_scrapes_never_ask_psa_and_keep_the_vin_masked(self):
        client = get_client(get_status("2026-01-01T00:00:00Z"))
        client.get_vehicle_info(VIN)
        client.api.side_effect = AssertionError("a scrape must not ask PSA")
        for _ in range(3):
            text = get_metrics(client, None, False, True)
        self.assertNotIn(VIN, text)
        self.assertEqual(1, client.api.call_count)

    def test_label_values_are_escaped(self):
        from psa_car_controller.psacc.application.metrics import Metrics  # pylint: disable=import-outside-toplevel
        metrics = Metrics()
        metrics.add("x_total", "counter", "x", [({"result": 'a"b\\c'}, 1)])
        self.assertIn('x_total{result="a\\"b\\\\c"} 1', metrics.text())

    def test_route(self):
        api = get_web_view("api")
        client = get_client(get_status("2026-01-01T00:00:00Z"))
        client.get_vehicle_info(VIN)
        client.is_authenticated = MagicMock(return_value=True)
        with patch.object(api, "APP", MagicMock(myp=client, remote_control=False)):
            response = api.app.test_client().get("/metrics")
        self.assertEqual(200, response.status_code)
        self.assertEqual(CONTENT_TYPE, response.headers["Content-Type"])
        self.assertIn("psacc_battery_level_percent", parse(response.get_data(as_text=True)))

    def test_route_before_the_first_configuration(self):
        api = get_web_view("api")
        app = MagicMock(remote_control=False)
        del app.myp
        with patch.object(api, "APP", app):
            response = api.app.test_client().get("/metrics")
        self.assertEqual(200, response.status_code)
        self.assertEqual({"": 0}, parse(response.get_data(as_text=True))["psacc_api_authenticated"])


if __name__ == "__main__":
    unittest.main()
