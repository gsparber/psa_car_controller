"""The Home Assistant MQTT form of the settings page (T90): checks, the password that never comes back, and one client
after a change without a restart. The paho client is a mock, no broker is needed."""
import json
import logging
import os
import shutil
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.reasoncodes import ReasonCode

from psa_car_controller.psacc.application import car_controller, ha_mqtt
from psa_car_controller.psacc.application.car_controller import PSACarController
from psa_car_controller.psacc.application.ha_mqtt import BROKER_UNREACHABLE, HomeAssistantMqtt, parse_settings, \
    sends_unencrypted
from psa_car_controller.psacc.repository import config_repository
from psa_car_controller.psacc.repository.config_repository import ConfigRepository, HomeAssistantMqttConfig
from psa_car_controller.web import app as web_app
from psa_car_controller.web.view import ui
from tests.utils import get_web_view

SECRET = "br0ker-secret-value"
NEW_SECRET = "n3w-broker-secret-value"


def form(**changes):
    values = {"enabled": True, "host": "broker.local", "port": 1883, "username": "user", "password": "", "tls": False,
              "remove_password": False, "discovery_prefix": "homeassistant", "base_topic": "psa_car_controller"}
    values.update(changes)
    return values


def stored(**changes):
    values = {"enabled": True, "host": "broker.local", "username": "user", "password": SECRET}
    values.update(changes)
    return HomeAssistantMqttConfig(**values)


class TestParseSettings(unittest.TestCase):

    def field_errors(self, current=None, **changes):
        new, errors = parse_settings(form(**changes), current or HomeAssistantMqttConfig())
        self.assertEqual(new is None, bool(errors))
        return errors

    def test_valid_hosts(self):
        for host in ("192.168.1.10", "core-mosquitto", "core_mosquitto", "broker.example.com", "broker.example.com.",
                     "::1", "fd00::10", "1.example.com", " broker.local "):
            self.assertEqual({}, self.field_errors(host=host), host)

    def test_invalid_hosts(self):
        for host in ("mqtt://broker.local", "broker.local:1883", "[::1]", "http://x/y", "a b", "a/b", "-a.b", "a..b",
                     ".", "999.1.1.1", "1.2.3", "a\nb", "x" * 300, ["broker"], {"a": 1}, 5):
            self.assertIn("host", self.field_errors(host=host), host)

    def test_enabled_needs_a_host_disabled_doesnt(self):
        self.assertIn("host", self.field_errors(host=""))
        self.assertEqual({}, self.field_errors(host="", enabled=False))

    def test_a_wrong_host_is_refused_while_disabled_too(self):
        self.assertIn("host", self.field_errors(host="mqtt://x", enabled=False))

    def test_port_range(self):
        for port in (1, 1883, 65535, "8883", 8883.0):
            new, errors = parse_settings(form(port=port), HomeAssistantMqttConfig())
            self.assertEqual({}, errors, port)
            self.assertEqual(int(port), new.port)
        for port in (0, -1, 65536, 1883.5, "abc", "", None, True, [1883], "18 83"):
            self.assertIn("port", self.field_errors(port=port), port)

    def test_topics(self):
        for field in ("discovery_prefix", "base_topic"):
            for topic in ("", "/", "a+b", "a#", "$SYS", "a b", "a\nb", None, 5):
                self.assertIn(field, self.field_errors(**{field: topic}), (field, topic))
        new, _ = parse_settings(form(base_topic="/my/cars/"), HomeAssistantMqttConfig())
        self.assertEqual("my/cars", new.base_topic)

    def test_base_topic_is_not_the_discovery_prefix(self):
        # the app's own "online" would look like Home Assistant restarting, and publish everything again, forever
        self.assertIn("base_topic", self.field_errors(base_topic="/homeassistant/"))

    def test_a_field_that_config_ini_would_change_is_refused(self):
        for text in ("a\nb", "a\r\n[General]", "tab\tin", " spaces", "spaces ", "\ud800", "x" * 2000):
            self.assertIn("password", self.field_errors(password=text), repr(text[:9]))
        for text in ("a\nb", "a\r\n[General]", "tab\tin", "\ud800", "x" * 2000):
            self.assertIn("username", self.field_errors(username=text), repr(text[:9]))

    def test_spaces_around_a_user_name_are_a_typing_slip(self):
        new, _ = parse_settings(form(username=" user "), HomeAssistantMqttConfig())
        self.assertEqual("user", new.username)

    def test_errors_dont_repeat_what_was_typed(self):
        errors = self.field_errors(password=" " + SECRET, username=SECRET + "\nx", host=SECRET + "://",
                                   discovery_prefix=SECRET + "+", base_topic=SECRET + "#")
        self.assertEqual(5, len(errors))
        self.assertNotIn(SECRET, str(errors))

    def test_empty_password_keeps_the_stored_one(self):
        for empty in ("", None):
            new, errors = parse_settings(form(password=empty), stored())
            self.assertEqual({}, errors)
            self.assertEqual(SECRET, new.password)

    def test_a_new_password_replaces_it(self):
        new, _ = parse_settings(form(password=NEW_SECRET), stored())
        self.assertEqual(NEW_SECRET, new.password)

    def test_removal_is_explicit(self):
        new, errors = parse_settings(form(remove_password=True), stored())
        self.assertEqual({}, errors)
        self.assertIsNone(new.password)

    def test_a_truthy_removal_isnt_enough(self):
        new, _ = parse_settings(form(remove_password="false"), stored())
        self.assertEqual(SECRET, new.password)

    def test_removing_and_typing_at_once_is_refused(self):
        self.assertIn("password", self.field_errors(stored(), password=NEW_SECRET, remove_password=True))

    def test_stored_password_doesnt_follow_a_new_destination(self):
        # no login on the page: otherwise anyone who can open it could have the password sent to their own server
        for changes in ({"host": "attacker.example"}, {"port": 1884}):
            self.assertIn("password", self.field_errors(stored(), **changes), changes)
        self.assertIn("password", self.field_errors(stored(tls=True), tls=False))

    def test_typed_again_it_may_go_to_the_new_destination(self):
        new, errors = parse_settings(form(host="other.local", password=NEW_SECRET), stored())
        self.assertEqual({}, errors)
        self.assertEqual(("other.local", NEW_SECRET), (new.host, new.password))

    def test_same_destination_keeps_it_without_typing(self):
        for changes in ({"host": "BROKER.local"}, {"tls": True}, {"enabled": False}, {"username": "other"},
                        {"base_topic": "cars"}):
            new, errors = parse_settings(form(**changes), stored())
            self.assertEqual({}, errors, changes)
            self.assertEqual(SECRET, new.password, changes)

    def test_wrong_host_alone_doesnt_ask_for_the_password_too(self):
        self.assertEqual(["host"], list(self.field_errors(stored(), host="mqtt://x")))
        self.assertEqual(["port"], list(self.field_errors(stored(), port=0)))

    def test_password_needs_a_user_name(self):
        # the client only sends a password along with a user name, it would be ignored without telling
        self.assertIn("username", self.field_errors(password=NEW_SECRET, username=""))
        self.assertIn("username", self.field_errors(stored(), username=""))
        self.assertEqual({}, self.field_errors(stored(), username="", remove_password=True))

    def test_flags_only_from_real_booleans(self):
        new, _ = parse_settings(form(enabled="yes", tls=1), HomeAssistantMqttConfig())
        self.assertEqual((False, False), (new.enabled, new.tls))


class TestUnencryptedWarning(unittest.TestCase):

    def test_warns_only_without_tls_to_a_foreign_host(self):
        for host in ("broker.example.com", "8.8.8.8", " broker.example.com "):
            self.assertTrue(sends_unencrypted(host, False), host)
            self.assertFalse(sends_unencrypted(host, True), host)
        for host in ("127.0.0.1", "192.168.1.10", "core-mosquitto", "broker.local", "::1", "", None):
            self.assertFalse(sends_unencrypted(host, False), host)


class TestPasswordInConfig(unittest.TestCase):

    def test_not_in_the_text_of_a_config(self):
        self.assertNotIn(SECRET, repr(stored()))
        self.assertNotIn(SECRET, str(stored()))

    def test_every_accepted_password_comes_back_from_the_file(self):
        directory = tempfile.mkdtemp(prefix="psacc-t90-")
        self.addCleanup(shutil.rmtree, directory)
        name = os.path.join(directory, "config.ini")
        for password in ("#start", ";start", "a#b;c", "=x", "a=b", "%s %(x)s", '"quoted"', "'q'", "äöü€", "[General]",
                         "1234", "true", "None", "a b", "\\n"):
            config = ConfigRepository.config_file_to_dto(ConfigRepository.get_default_config())
            new, errors = parse_settings(form(password=password), HomeAssistantMqttConfig())
            self.assertEqual({}, errors, password)
            config.Home_Assistant_MQTT = new
            config.write_config(name)
            read = ConfigRepository.read_config(name).Home_Assistant_MQTT
            self.assertEqual(new, read, password)


class SettingsAppTestCase(unittest.TestCase):
    """A real controller and a real config file in a temp directory, without an account or a broker."""

    def setUp(self):
        directory = tempfile.mkdtemp(prefix="psacc-t90-")
        self.addCleanup(shutil.rmtree, directory)
        self.ini = os.path.join(directory, "config.ini")
        patcher = patch.object(config_repository, "CONFIG_FILENAME", self.ini)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.clients = []
        self.client_delay = 0
        client_patch = patch.object(ha_mqtt.mqtt, "Client", side_effect=self.new_client)
        client_patch.start()
        self.addCleanup(client_patch.stop)

        self.app = PSACarController.__new__(PSACarController)
        self.app.config = ConfigRepository.config_file_to_dto(ConfigRepository.get_default_config())
        self.app.home_assistant = None
        self.app.chc = None
        self.app.remote_control = True
        self.app.myp = SimpleNamespace(vehicles_list=[], info_callback=[], remote_client=None)
        self.addCleanup(self.stop_client)

    def new_client(self, *_):
        time.sleep(self.client_delay)
        client = MagicMock()
        client.is_connected.return_value = False
        self.clients.append(client)
        return client

    def stop_client(self):
        if self.app.home_assistant is not None:
            self.app.home_assistant.stop()

    def save(self, **changes):
        return self.app.save_home_assistant(form(**changes))

    def running(self):
        return [client for client in self.clients if client.loop_start.called and not client.loop_stop.called]

    def file(self):
        return ConfigRepository.read_config(self.ini).Home_Assistant_MQTT


class TestSaveAndReconnect(SettingsAppTestCase):

    def test_save_writes_the_file_and_connects(self):
        self.assertEqual({}, self.save(password=SECRET))
        self.assertEqual(("broker.local", SECRET), (self.file().host, self.file().password))
        self.assertEqual(1, len(self.running()))
        client = self.running()[0]
        client.connect_async.assert_called_once_with("broker.local", 1883, keepalive=60)
        client.username_pw_set.assert_called_once_with("user", SECRET)
        self.assertEqual(("psa_car_controller/status", "offline"), client.will_set.call_args.args)
        self.assertTrue(client.will_set.call_args.kwargs["retain"])

    def test_a_change_leaves_one_client_with_the_new_values(self):
        self.save(password=SECRET)
        first = self.running()[0]

        self.assertEqual({}, self.save(base_topic="cars"))

        self.assertEqual(2, len(self.clients))
        self.assertEqual(1, len(self.running()))
        self.assertIsNot(first, self.running()[0])
        first.publish.assert_called_with("psa_car_controller/status", "offline", qos=1, retain=True)
        first.disconnect.assert_called_once()
        first.loop_stop.assert_called_once()
        self.assertEqual("cars/status", self.running()[0].will_set.call_args.args[0])
        self.assertEqual(1, len(self.app.myp.info_callback))

    def test_new_client_publishes_discovery_again_on_connect(self):
        self.save()
        self.save(base_topic="cars")
        with patch.object(self.app.home_assistant, "publish_all") as publish_all:
            self.app.home_assistant._on_connect(self.running()[0], None, None,
                                                ReasonCode(PacketTypes.CONNACK, "Success"), None)
        publish_all.assert_called_once()

    def test_same_values_dont_reconnect(self):
        self.save(password=SECRET)
        self.assertEqual({}, self.save())
        self.assertEqual(1, len(self.clients))
        self.running()[0].disconnect.assert_not_called()

    def test_disabling_stops_the_client(self):
        self.save()
        self.assertEqual({}, self.save(enabled=False))
        self.assertEqual([], self.running())
        self.assertIsNone(self.app.home_assistant)
        self.assertFalse(self.file().enabled)

    def test_invalid_values_change_nothing(self):
        self.save(password=SECRET)
        before = self.app.config.Home_Assistant_MQTT
        with open(self.ini, encoding="utf-8") as f:
            on_disk = f.read()

        errors = self.save(host="mqtt://broker", port=99999, password=NEW_SECRET)

        self.assertEqual({"host", "port"}, set(errors))
        self.assertIs(before, self.app.config.Home_Assistant_MQTT)
        with open(self.ini, encoding="utf-8") as f:
            self.assertEqual(on_disk, f.read())
        self.assertEqual(1, len(self.clients))
        self.running()[0].disconnect.assert_not_called()

    def test_failed_write_keeps_the_old_config_and_client(self):
        self.save(password=SECRET)
        before = self.app.config.Home_Assistant_MQTT
        with patch.object(ConfigRepository, "_write", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.save(base_topic="cars")
        self.assertIs(before, self.app.config.Home_Assistant_MQTT)
        self.assertEqual(1, len(self.clients))
        self.assertEqual("psa_car_controller", self.file().base_topic)

    def test_saves_at_the_same_time_leave_one_client(self):
        barrier = threading.Barrier(8)
        results = []

        def save(index):
            barrier.wait()
            results.append(self.save(base_topic=f"cars{index}"))

        threads = [threading.Thread(target=save, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual([{}] * 8, results)
        self.assertEqual(1, len(self.running()))
        self.assertEqual(self.file().base_topic, self.app.config.Home_Assistant_MQTT.base_topic)
        self.assertEqual(self.file().base_topic + "/status", self.running()[0].will_set.call_args.args[0])
        self.assertEqual(1, len(self.app.myp.info_callback))

    def test_starts_at_the_same_time_leave_one_client(self):
        # a login (load_app) next to a save: without the lock each creates a client and only the last can be stopped
        self.save()
        self.client_delay = 0.02
        threads = [threading.Thread(target=self.app.start_home_assistant) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(1, len(self.running()))
        self.assertEqual(1, len(self.app.myp.info_callback))

    def test_a_save_doesnt_overlap_another(self):
        # the stored password is read at the start and written at the end: an overlap would bring an old one back
        overlaps = []
        inside = []
        parse = ha_mqtt.parse_settings

        def slow_parse(*args):
            inside.append(1)
            overlaps.append(len(inside))
            time.sleep(0.02)
            return parse(*args)

        def done(*_):
            inside.pop()

        self.save()
        with patch.object(car_controller, "parse_settings", side_effect=slow_parse), \
                patch.object(ConfigRepository, "_write", side_effect=done):
            threads = [threading.Thread(target=self.save, kwargs={"base_topic": f"cars{i}"}) for i in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual([1, 1, 1, 1], overlaps)

    def test_without_an_account_it_only_saves(self):
        del self.app.myp
        self.assertEqual({}, self.save(password=SECRET))
        self.assertEqual([], self.clients)
        self.assertEqual("broker.local", self.file().host)

    def test_start_failure_is_told_and_can_be_retried(self):
        with patch.object(HomeAssistantMqtt, "start", side_effect=ValueError("boom")):
            self.assertEqual({}, self.save())
        self.assertTrue(self.app.ha_start_failed)
        self.assertIsNone(self.app.home_assistant)
        self.assertEqual("broker.local", self.file().host)

        self.assertEqual({}, self.save())  # the same values, but nothing runs
        self.assertFalse(self.app.ha_start_failed)
        self.assertEqual(1, len(self.running()))


class TestClientStatus(SettingsAppTestCase):

    def setUp(self):
        super().setUp()
        self.save(password=SECRET)
        self.ha = self.app.home_assistant

    def test_refused_keeps_the_reason_over_the_disconnect(self):
        refused = ReasonCode(PacketTypes.CONNACK, "Not authorized")
        self.ha._on_connect(self.running()[0], None, None, refused, None)
        self.ha._on_disconnect(self.running()[0], None, None, ReasonCode(PacketTypes.DISCONNECT, "Unspecified error"))
        self.assertEqual("Refused by the broker: Not authorized", self.ha.last_error)

    def test_connect_failure_and_recovery(self):
        with self.assertLogs(ha_mqtt.logger, logging.WARNING) as logs:
            self.ha._on_connect_fail(self.running()[0], None)
            self.ha._on_connect_fail(self.running()[0], None)
        self.assertEqual(1, len(logs.records))  # it retries forever: once in the log
        self.assertEqual(BROKER_UNREACHABLE, self.ha.last_error)
        self.ha._on_connect(self.running()[0], None, None, ReasonCode(PacketTypes.CONNACK, "Success"), None)
        self.assertIsNone(self.ha.last_error)

    def test_stuck_paho_thread_doesnt_hold_up_the_save(self):
        # measured with a silent server: 59.5 s in the TLS handshake, with the lock of the logins held
        release = threading.Event()
        self.addCleanup(release.set)
        self.running()[0].loop_stop.side_effect = lambda: release.wait(10)
        started = time.monotonic()
        with patch.object(ha_mqtt, "STOP_TIMEOUT", 0.05):
            self.assertEqual({}, self.save(base_topic="cars"))
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual("cars/status", self.clients[-1].will_set.call_args.args[0])
        self.assertTrue(self.clients[-1].loop_start.called)

    def test_late_connect_of_a_stopped_client_publishes_nothing(self):
        client = self.running()[0]
        self.ha.stop()
        client.publish.reset_mock()
        self.ha._on_connect(client, None, None, ReasonCode(PacketTypes.CONNACK, "Success"), None)
        client.publish.assert_not_called()
        client.subscribe.assert_not_called()
        self.app.home_assistant = None

    def test_stopped_client_publishes_nothing_more(self):
        client = self.running()[0]
        self.ha.stop()
        client.publish.reset_mock()
        self.ha.publish_states()
        self.ha._publish("a/b", "x")
        with patch.object(ha_mqtt.threading, "Timer") as timer:
            self.ha._periodic()
        timer.assert_not_called()
        client.publish.assert_not_called()
        self.assertFalse(self.ha.is_connected())
        self.app.home_assistant = None


class TestSettingsPage(SettingsAppTestCase):
    """Through the real Dash dispatch, like the browser does it."""

    def setUp(self):
        super().setUp()
        get_web_view("api")  # flask refuses new routes once it handled a request, like the ones below
        self.settings = get_web_view("settings")
        patcher = patch.object(self.settings, "app", return_value=self.app)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.key = next(key for key in web_app.dash_app.callback_map
                        if f"{self.settings.HA_STORED_HINT}.children" in key)

    def post(self, n_clicks=1, content_type=None, **changes):
        values = form(**changes)
        names = {self.settings.HA_ENABLED: "enabled", self.settings.HA_TLS: "tls",
                 self.settings.HA_REMOVE_STORED: "remove_password"}
        names.update({input_id: field for field, input_id in self.settings.HA_INPUTS.items()})
        outputs = [dict(zip(("id", "property"), output.rsplit(".", 1)))
                   for output in self.key.strip(".").split("...")]
        for output in outputs:
            output["property"] = output["property"].split("@")[0]
        body = {"output": self.key, "outputs": outputs,
                "inputs": [{"id": self.settings.HA_SAVE, "property": "n_clicks", "value": n_clicks}],
                "state": [{"id": input_id, "property": "value", "value": values[field]}
                          for input_id, field in names.items()],
                "changedPropIds": [self.settings.HA_SAVE + ".n_clicks"]}
        client = web_app.app.test_client()
        if content_type:
            return client.post("/_dash-update-component", data=json.dumps(body), content_type=content_type)
        return client.post("/_dash-update-component", json=body)

    def answer(self, **changes):
        response = self.post(**changes)
        self.assertEqual(200, response.status_code)
        return response.get_data(as_text=True), response.get_json()["response"]

    def layout(self):
        with patch.object(self.settings, "current_state", return_value=[self.settings.LOGGED_IN, None, None]):
            layout = self.settings.get_settings_layout()
        return json.dumps(layout.to_plotly_json(), default=lambda o: o.to_plotly_json())

    def test_field_errors_come_back_per_field(self):
        _, response = self.answer(host="mqtt://x", port=70000)
        self.assertEqual("Not saved, check the marked fields", response[ui.TOAST]["children"])
        self.assertTrue(response[self.settings.HA_INPUTS["host"]]["invalid"])
        self.assertTrue(response[self.settings.HA_INPUTS["port"]]["invalid"])
        self.assertFalse(response[self.settings.HA_INPUTS["username"]]["invalid"])
        self.assertIn("without mqtt://", response[self.settings.HA_INPUTS["host"] + "-feedback"]["children"])
        self.assertIn("65535", response[self.settings.HA_INPUTS["port"] + "-feedback"]["children"])
        self.assertNotIn("value", response[self.settings.HA_INPUTS["password"]])  # what is typed stays in the form

    def test_password_is_in_no_answer_nor_log(self):
        with self.assertLogs(level=logging.DEBUG) as logs:
            logging.getLogger().debug("started")
            answers = [self.answer(password=SECRET)[0],  # new
                       self.answer()[0],  # kept, nothing changed
                       self.answer(base_topic="cars")[0],  # kept, reconnects
                       self.answer(host="mqtt://x", password=SECRET)[0],  # refused
                       self.answer(password=" " + SECRET)[0]]  # refused
            self.settings.refresh_home_assistant(1)
        self.assertEqual(SECRET, self.app.config.Home_Assistant_MQTT.password)
        for text in answers + [self.layout(), json.dumps(
                self.settings._ha_status().to_plotly_json(), default=lambda o: o.to_plotly_json())]:
            self.assertNotIn(SECRET, text)
        self.assertNotIn(SECRET, "\n".join(logs.output))

    def test_field_is_empty_and_hint_says_it_is_set(self):
        self.save(password=SECRET)
        layout = self.layout()
        self.assertIn("Set. Leave empty to keep it.", layout)
        password = self.settings._home_assistant()
        found = self.find(password, self.settings.HA_INPUTS["password"])
        self.assertEqual("", found.value)
        self.assertEqual("new-password", found.autoComplete)
        self.assertEqual("mt-1", self.find(password, self.settings.HA_REMOVE_ROW).className)

    def test_no_stored_password_no_removal(self):
        section = self.settings._home_assistant()
        self.assertEqual("d-none", self.find(section, self.settings.HA_REMOVE_ROW).className)
        self.assertIn("Not set.", self.layout())

    @classmethod
    def find(cls, component, wanted):
        if getattr(component, "id", None) == wanted:
            return component
        children = getattr(component, "children", None)
        for child in children if isinstance(children, (list, tuple)) else [children]:
            found = cls.find(child, wanted) if child is not None and not isinstance(child, str) else None
            if found is not None:
                return found
        return None

    def test_after_saving_the_field_is_cleared_and_the_hint_updated(self):
        _, response = self.answer(password=SECRET)
        self.assertEqual("", response[self.settings.HA_INPUTS["password"]]["value"])
        self.assertEqual("Set. Leave empty to keep it.", response[self.settings.HA_STORED_HINT]["children"])
        self.assertEqual("mt-1", response[self.settings.HA_REMOVE_ROW]["className"])
        self.assertEqual("Saved, connecting to the broker", response[ui.TOAST]["children"])

        _, response = self.answer(remove_password=True)
        self.assertIsNone(self.app.config.Home_Assistant_MQTT.password)
        self.assertIsNone(self.file().password)
        self.assertEqual("Not set.", response[self.settings.HA_STORED_HINT]["children"])
        self.assertEqual("d-none", response[self.settings.HA_REMOVE_ROW]["className"])
        self.assertFalse(response[self.settings.HA_REMOVE_STORED]["value"])

    def test_an_unclicked_button_saves_nothing(self):
        response = self.post(n_clicks=0)
        self.assertEqual(204, response.status_code)
        self.assertFalse(self.app.config.Home_Assistant_MQTT.enabled)

    def test_cross_site_form_post_cant_save(self):
        # a foreign page can only send text/plain without asking the browser first
        response = self.post(content_type="text/plain", password=SECRET)
        self.assertNotEqual(200, response.status_code)
        self.assertFalse(self.app.config.Home_Assistant_MQTT.enabled)

    def test_write_failure_is_told_and_logged_without_the_password(self):
        with patch.object(ConfigRepository, "_write", side_effect=OSError("disk full")), \
                self.assertLogs(self.settings.logger, logging.ERROR) as logs:
            _, response = self.answer(password=SECRET)
        self.assertEqual("Can't save the setting, see the log", response[ui.TOAST]["children"])
        self.assertNotIn(SECRET, "\n".join(logs.output))
        self.assertIsNone(self.app.config.Home_Assistant_MQTT.password)

    def status_text(self):
        return json.dumps(self.settings._ha_status().to_plotly_json(), default=lambda o: o.to_plotly_json())

    def test_status_of_each_state(self):
        self.assertIn('"Off"', self.status_text())
        self.save(password=SECRET)
        self.assertIn("Connecting", self.status_text())
        self.running()[0].is_connected.return_value = True
        self.assertIn("Connected", self.status_text())
        self.running()[0].is_connected.return_value = False
        self.app.home_assistant.last_error = "Refused by the broker: Not authorized"
        self.assertIn("Not connected", self.status_text())
        self.assertIn("Not authorized", self.status_text())

    def test_status_without_an_account(self):
        del self.app.myp
        self.save()
        self.assertIn("Starts after login", self.status_text())

    def test_warning_only_without_tls_to_a_foreign_host(self):
        self.assertIsNone(self.settings.warn_home_assistant(True, False, 1, "192.168.1.10"))
        self.assertIsNone(self.settings.warn_home_assistant(True, True, 1, "broker.example.com"))
        self.assertIsNone(self.settings.warn_home_assistant(False, False, 1, "broker.example.com"))
        self.assertIn("unencrypted", self.settings.warn_home_assistant(True, False, 1, "broker.example.com").children)
        self.assertIsNone(self.settings.warn_home_assistant(True, False, 1, None))


class TestSettingsApi(SettingsAppTestCase):
    """/settings has no login: the password never leaves through it and nothing is changed through it."""

    def setUp(self):
        super().setUp()
        api = get_web_view("api")
        patcher = patch.object(api.APP, "config", self.app.config)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.save(password=SECRET)
        self.client = api.app.test_client()

    def test_password_not_in_the_answer(self):
        self.assertNotIn(SECRET, self.client.get("/settings").get_data(as_text=True))

    def test_no_write_through_the_section_route(self):
        before = self.app.config.Home_Assistant_MQTT.model_copy()
        for section in ("Home_Assistant_MQTT", "home_assistant_mqtt", "Home_assistant_mqtt", "HOME_ASSISTANT_MQTT"):
            for key in ("password", "host"):
                try:
                    response = self.client.get(f"/settings/{section}?{key}=attacker.example")
                except AttributeError:
                    continue  # the test client hands the exception over: not reachable
                self.assertNotIn(SECRET, response.get_data(as_text=True))
        self.assertEqual(before, self.app.config.Home_Assistant_MQTT)
        self.assertEqual(before.host, self.file().host)
