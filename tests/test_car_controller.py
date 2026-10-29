import json
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock
from unittest.mock import MagicMock

from requests import RequestException

from psa_car_controller.psacc.application.car_controller import PSACarController
from psa_car_controller.psacc.application.psa_client import PSAClient

VIN = "VR3UHZKX00000001"


def new_psa_client():
    psacc = MagicMock()
    psacc.info_callback = []
    psacc.remote_client.mqtt_client = None
    return psacc


class TestStartRemoteControl(unittest.TestCase):
    def setUp(self):
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        self.charge_config = os.path.join(tmp_dir.name, "charge_config.json")
        # skip the singleton and __init__: they parse the command line and read config.ini
        self.app = PSACarController.__new__(PSACarController)
        self.app.args = SimpleNamespace(remote_disable=False, web_conf=False, charge_control=self.charge_config)
        self.app.chc = None
        self.app.myp = new_psa_client()

    def write_charge_config(self, percentage_threshold):
        with open(self.charge_config, "w", encoding="utf-8") as f:
            json.dump({VIN: {"percentage_threshold": percentage_threshold, "stop_hour": [9, 0]}}, f)

    def test_restart_keeps_one_charge_control_per_car(self):
        # start, then OTP config: start_remote_control runs again with the same PSA client
        self.write_charge_config(80)
        self.app.start_remote_control()
        self.write_charge_config(70)
        self.app.start_remote_control()
        self.assertEqual([self.app.chc.get(VIN).process], self.app.myp.info_callback)
        self.assertEqual(70, self.app.chc.get(VIN).percentage_threshold)

    def test_login_stops_charge_control_of_previous_client(self):
        # login: load_app replaces the PSA client before start_remote_control runs again
        self.write_charge_config(80)
        self.app.start_remote_control()
        previous_client = self.app.myp
        self.app.myp = new_psa_client()
        self.app.start_remote_control()
        self.assertEqual([], previous_client.info_callback)
        self.assertEqual([self.app.chc.get(VIN).process], self.app.myp.info_callback)

    def test_unreadable_config_keeps_running_charge_control(self):
        self.write_charge_config(80)
        self.app.start_remote_control()
        running = self.app.chc
        with open(self.charge_config, "w", encoding="utf-8") as f:
            f.write("{not json")
        with self.assertRaises(ValueError):
            self.app.start_remote_control()
        self.assertIs(running, self.app.chc)
        self.assertEqual([running.get(VIN).process], self.app.myp.info_callback)


class TestLoadAppAgain(unittest.TestCase):
    """load_app runs at startup and again after each login from the config web page."""

    def setUp(self):
        self.app = PSACarController.__new__(PSACarController)
        self.app.args = SimpleNamespace(debug=0, config="config.json", web_conf=True, offline=True, record=False,
                                        refresh=None, remote_disable=True, charge_control=None)
        self.app.config = MagicMock()
        self.app.chc = None
        self.app.saving_config = False
        self.app.retry_timer = None
        self.clients = []
        self.timers = []

        def new_client(**_):
            client = MagicMock()
            client.manager.refresh_token_now.return_value = False
            client.manager.last_refresh_error = RequestException("dns not up yet")
            self.clients.append(client)
            return client

        def new_timer(*_args, **_kwargs):
            timer = MagicMock()
            self.timers.append(timer)
            return timer

        module = "psa_car_controller.psacc.application.car_controller"
        for target, kwargs in ((f"{module}.my_logger", {}),
                               (f"{module}.path.isfile", {"return_value": True}),
                               (f"{module}.PSAClient.load_config", {"side_effect": new_client}),
                               (f"{module}.Database.check_db_access", {"return_value": True}),
                               (f"{module}.atexit.register", {}),
                               (f"{module}.Charging", {}),
                               (f"{module}.threading.Timer", {"side_effect": new_timer})):
            patcher = mock.patch(target, **kwargs)
            self.addCleanup(patcher.stop)
            patcher.start()

    def test_login_stops_the_replaced_client(self):
        self.app.load_app()
        self.app.load_app()
        self.assertEqual(2, len(self.clients))
        # its refresh thread and mqtt connection would otherwise keep running next to the new ones
        self.clients[0].stop.assert_called_once()
        self.clients[1].stop.assert_not_called()

    def test_refresh_argument_sets_the_rate_also_while_charging(self):
        self.app.args.offline = False
        self.app.args.refresh = 10
        self.app.load_app()
        self.assertEqual(600, self.clients[0].info_refresh_rate)
        self.assertEqual(600, self.clients[0].charging_refresh_rate)

    def test_login_keeps_a_single_config_saving_loop(self):
        self.app.load_app()
        self.app.load_app()
        self.assertEqual(1, len(self.timers))
        self.assertEqual(2, self.clients[1].save_config.call_count + self.clients[0].save_config.call_count)

    def test_login_keeps_a_single_connection_retry_loop(self):
        self.app.args.offline = False
        self.app.load_app()
        self.app.load_app()
        retry_timers = [timer for timer in self.timers if timer.daemon is True]
        self.assertEqual(2, len(retry_timers))
        retry_timers[0].cancel.assert_called_once()
        retry_timers[1].cancel.assert_not_called()


class TestPSAClientStop(unittest.TestCase):
    def test_stop_ends_the_refresh_loop(self):
        client = PSAClient.__new__(PSAClient)
        client.info_refresh_rate = 0.05
        client.refresh_thread = None
        client.vehicles_list = []
        client.info_callback = []
        client.remote_client = MagicMock()
        client.manager = MagicMock()
        client.start_refresh_thread()
        timer = client.refresh_thread
        client.stop()
        time.sleep(0.2)
        self.assertIs(timer, client.refresh_thread)
        self.assertFalse(timer.is_alive())
        client.remote_client.stop.assert_called_once()
        client.manager.stop_scheduled_refresh.assert_called_once()


if __name__ == '__main__':
    unittest.main()
