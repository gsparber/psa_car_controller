"""Home Assistant MQTT discovery (T40): the cars and their remote commands as Home Assistant entities, published
to the user's own broker. The paho client is a mock, no broker is needed."""
import json
import logging
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from paho.mqtt.client import ConnectFlags, MQTTMessage
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.reasoncodes import ReasonCode

from psa_car_controller.psa.RemoteClient import wakeup_limiter
from psa_car_controller.psa.connected_car_api import ApiClient
from psa_car_controller.psacc.application import ha_mqtt
from psa_car_controller.psacc.application.ha_mqtt import HomeAssistantMqtt, car_states, node_id
from psa_car_controller.psacc.model.car import Car, Cars
from psa_car_controller.psacc.repository.config_repository import ConfigRepository, HomeAssistantMqttConfig
from tests.data.car_status import ELECTRIC_CAR_STATUS
from tests.test_remote_commands import get_ready_remote_client, VIN

NODE = node_id(VIN)


class SyncExecutor:
    """Runs submitted commands right away instead of on the command thread."""

    @staticmethod
    def submit(fn, *args):
        fn(*args)

    def shutdown(self, wait=True):
        pass


def electric_car(vin=VIN):
    car = Car(vin, "vid", "Peugeot")
    car.status = ApiClient()._ApiClient__deserialize(json.loads(json.dumps(ELECTRIC_CAR_STATUS)), "Status")
    return car


def message(topic, payload):
    msg = MQTTMessage(topic=topic.encode())
    msg.payload = payload.encode()
    return msg


class HomeAssistantTestCase(unittest.TestCase):

    def setUp(self):
        wakeup_limiter.reset()
        self.addCleanup(wakeup_limiter.reset)
        self.car = electric_car()
        self.remote = get_ready_remote_client(self.car)
        self.psacc = SimpleNamespace(vehicles_list=self.remote.vehicles_list, info_callback=[],
                                     remote_client=self.remote)
        self.charge_control = SimpleNamespace(percentage_threshold=80)
        self.charge_controls = MagicMock()
        self.charge_controls.get.side_effect = lambda vin: self.charge_control if vin == VIN else None
        self.config = HomeAssistantMqttConfig(enabled=True, host="broker.local", username="user",
                                              password="broker-secret")
        self.client = MagicMock()
        self.ha = self.new_publisher()

    def new_publisher(self, remote_enabled=True):
        ha = HomeAssistantMqtt(self.config, self.psacc, lambda: self.charge_controls, remote_enabled=remote_enabled,
                               client_factory=lambda: self.client)
        ha._executor = SyncExecutor()
        self.addCleanup(ha.stop)
        return ha

    def published(self):
        """{topic: (payload, retain)} of the publishes to the Home Assistant broker."""
        return {c.args[0]: (c.args[1], c.kwargs.get("retain")) for c in self.client.publish.call_args_list}

    def connect(self):
        self.ha._on_connect(self.client, None, ConnectFlags(False), ReasonCode(PacketTypes.CONNACK, "Success"), None)

    def command(self, key, payload):
        self.ha._on_message(self.client, None, message(f"psa_car_controller/{NODE}/{key}/set", payload))

    def car_commands(self):
        return [call.args[0] for call in self.remote.mqtt_client.publish.call_args_list]


class TestConfig(unittest.TestCase):

    def test_disabled_by_default(self):
        config = ConfigRepository.config_file_to_dto(ConfigRepository.get_default_config())
        self.assertFalse(config.Home_Assistant_MQTT.enabled)
        self.assertFalse(config.Home_Assistant_MQTT.is_enable())

    def test_old_config_without_section_stays_disabled(self):
        config = ConfigRepository(General={}, Electricity_config={})
        self.assertFalse(config.Home_Assistant_MQTT.is_enable())

    def test_enabled_needs_a_host(self):
        self.assertFalse(HomeAssistantMqttConfig(enabled=True).is_enable())
        self.assertTrue(HomeAssistantMqttConfig(enabled=True, host="10.0.0.2").is_enable())

    def test_section_read_and_written_back(self):
        config = ConfigRepository.get_default_config()
        config["Home Assistant MQTT"]["enabled"] = "true"
        config["Home Assistant MQTT"]["host"] = "core-mosquitto"
        config["Home Assistant MQTT"]["port"] = "8883"
        dto = ConfigRepository.config_file_to_dto(config)
        self.assertTrue(dto.Home_Assistant_MQTT.is_enable())
        self.assertEqual(8883, dto.Home_Assistant_MQTT.port)
        written = dto.config_dto_to_config_file(ConfigRepository.get_default_config())
        self.assertEqual("core-mosquitto", written["Home Assistant MQTT"]["host"].value)
        self.assertEqual(8883, written["Home Assistant MQTT"]["port"].value)

    def test_settings_json_hides_the_broker_password(self):
        config = ConfigRepository(General={}, Electricity_config={},
                                  Home_Assistant_MQTT={"host": "h", "password": "broker-secret"})
        self.assertNotIn("broker-secret", config.model_dump_json(exclude={"Home_Assistant_MQTT": {"password"}}))


class TestStates(unittest.TestCase):

    def test_electric_car(self):
        states = car_states(electric_car(), charge_limit=80)
        self.assertEqual(59, states["battery_level"])
        self.assertEqual(122, states["range"])
        self.assertEqual(3196.5, states["odometer"])
        self.assertEqual("Disconnected", states["charging_status"])
        self.assertEqual("OFF", states["charging"])
        self.assertEqual(0, states["charging_power"])
        self.assertEqual(80, states["charge_limit"])
        self.assertIsNone(states["climate"])  # not in this status
        self.assertEqual({"latitude": 47.274, "longitude": -1.59008, "gps_accuracy": 0}, states["location"])
        self.assertIsNone(states["lock"])  # no doors state in this status

    def test_charging_power_estimated_while_charging(self):
        car = electric_car()
        charging = car.status.get_energy("Electric").charging
        charging.status, charging.charging_rate = "InProgress", 40
        states = car_states(car)
        self.assertEqual("ON", states["charging"])
        # 40 km/h x (battery kWh x 59 % / 122 km)
        self.assertAlmostEqual(40 * car.battery_power * 0.59 / 122, states["charging_power"], places=2)

    def test_climate(self):
        car = electric_car()
        car.status.preconditionning = SimpleNamespace(air_conditioning=SimpleNamespace(status="Enabled"))
        self.assertEqual(("ON", "Enabled"), (car_states(car)["climate"], car_states(car)["climate_status"]))
        car.status.preconditionning.air_conditioning.status = "Finished"
        self.assertEqual("OFF", car_states(car)["climate"])

    def test_lock_state(self):
        car = electric_car()
        car.status.doors_state = SimpleNamespace(locked_state=["Locked", "SuperLocked"])
        self.assertEqual("LOCKED", car_states(car)["lock"])
        car.status.doors_state = SimpleNamespace(locked_state=["Unlocked"])
        self.assertEqual("UNLOCKED", car_states(car)["lock"])

    def test_no_status_yet(self):
        self.assertEqual({}, car_states(Car(VIN, "vid", "Peugeot")))


class TestDiscovery(HomeAssistantTestCase):

    def test_start_sets_credentials_last_will_and_connects(self):
        self.ha.start()
        self.client.username_pw_set.assert_called_once_with("user", "broker-secret")
        self.client.will_set.assert_called_once_with("psa_car_controller/status", "offline", qos=1, retain=True)
        self.client.connect_async.assert_called_once_with("broker.local", 1883, keepalive=60)
        self.client.tls_set.assert_not_called()
        self.assertIn(self.ha.publish_states, self.psacc.info_callback)
        self.assertIn(self.ha.publish_command, self.remote.command_callbacks)

    def test_stop_goes_offline_and_unhooks(self):
        self.ha.start()
        self.ha.stop()
        self.assertEqual(("offline", True), self.published()["psa_car_controller/status"])
        self.client.loop_stop.assert_called_once()
        self.assertEqual([], self.psacc.info_callback)
        self.assertNotIn(self.ha.publish_command, self.remote.command_callbacks)

    def test_connect_publishes_retained_configs_and_states(self):
        self.connect()
        published = self.published()
        self.assertEqual(("online", True), published["psa_car_controller/status"])
        payload, retain = published[f"homeassistant/sensor/{NODE}/battery_level/config"]
        config = json.loads(payload)
        self.assertTrue(retain)
        self.assertEqual("%", config["unit_of_measurement"])
        self.assertEqual("battery", config["device_class"])
        self.assertEqual(f"psa_car_controller/{NODE}/battery_level/state", config["state_topic"])
        self.assertEqual("psa_car_controller/status", config["availability_topic"])
        self.assertEqual(f"{NODE}_battery_level", config["unique_id"])
        self.assertEqual(("59", True), published[f"psa_car_controller/{NODE}/battery_level/state"])
        # metric, Home Assistant converts
        self.assertEqual("km", json.loads(published[f"homeassistant/sensor/{NODE}/range/config"][0])
                         ["unit_of_measurement"])
        self.assertEqual("gps", json.loads(published[f"homeassistant/device_tracker/{NODE}/location/config"][0])
                         ["source_type"])
        for component, key in (("button", "wakeup"), ("button", "horn"), ("button", "lights"),
                               ("switch", "charging"), ("switch", "climate"), ("number", "charge_limit"),
                               ("lock", "lock")):
            self.assertIn(f"homeassistant/{component}/{NODE}/{key}/config", published)
        self.client.subscribe.assert_any_call("psa_car_controller/+/+/set")
        self.client.subscribe.assert_any_call("homeassistant/status")

    def test_vin_never_in_topics_or_payloads(self):
        self.connect()
        for topic, (payload, _) in self.published().items():
            self.assertNotIn(VIN, topic)
            self.assertNotIn(VIN, payload)

    def test_without_remote_control_no_commands(self):
        self.ha = self.new_publisher(remote_enabled=False)
        self.connect()
        topics = self.published()
        self.assertFalse([t for t in topics if "/button/" in t or "/switch/" in t or "/number/" in t])
        self.assertIn(f"homeassistant/binary_sensor/{NODE}/lock/config", topics)

    def test_no_charge_limit_without_charge_control(self):
        self.charge_controls = None
        self.connect()
        self.assertNotIn(f"homeassistant/number/{NODE}/charge_limit/config", self.published())

    def test_refused_connection_publishes_nothing(self):
        self.ha._on_connect(self.client, None, ConnectFlags(False),
                            ReasonCode(PacketTypes.CONNACK, "Not authorized"), None)
        self.client.publish.assert_not_called()

    def test_only_changed_states_are_published_again(self):
        self.connect()
        self.client.publish.reset_mock()
        self.ha.publish_states()
        self.client.publish.assert_not_called()
        self.car.status.get_energy("Electric").level = 60
        self.ha.publish_states()
        self.assertEqual({f"psa_car_controller/{NODE}/battery_level/state": ("60", True)}, self.published())

    def test_home_assistant_restart_gets_everything_again(self):
        self.connect()
        self.client.publish.reset_mock()
        self.ha._on_message(self.client, None, message("homeassistant/status", "online"))
        self.assertIn(f"homeassistant/sensor/{NODE}/battery_level/config", self.published())
        self.assertIn(f"psa_car_controller/{NODE}/battery_level/state", self.published())


class TestCommands(HomeAssistantTestCase):

    def test_lock_and_unlock(self):
        self.command("lock", "LOCK")
        self.assertEqual(1, len(self.car_commands()))
        self.assertTrue(self.car_commands()[0].endswith("/Doors"))

    def test_command_state_published(self):
        self.command("horn", "PRESS")
        published = self.published()
        self.assertEqual(("sent", True), published[f"psa_car_controller/{NODE}/last_command/state"])
        self.assertEqual("/Horn", json.loads(published[f"psa_car_controller/{NODE}/last_command/attributes"][0])
                         ["command"])

    def test_one_command_at_a_time(self):
        self.command("horn", "PRESS")
        self.command("lights", "PRESS")
        self.assertEqual(1, len(self.car_commands()))

    def test_wakeup_rate_limit_is_kept(self):
        for _ in range(8):
            for command in self.remote.commands.to_list(VIN):  # answered: the car isn't busy any more
                self.remote.commands.answer(command["correlation_id"], "0")
            with self.assertNoLogs(ha_mqtt.logger, level=logging.ERROR):
                self.command("wakeup", "PRESS")
        self.assertEqual(6, len(self.car_commands()))

    def test_charging_switch(self):
        self.command("charging", "ON")
        topic = self.car_commands()[0]
        self.assertTrue(topic.endswith("/VehCharge"))

    def test_charge_limit(self):
        self.command("charge_limit", "70")
        self.assertEqual(70, self.charge_control.percentage_threshold)
        self.charge_controls.save_config.assert_called_once()
        self.assertEqual(("70", True), self.published()[f"psa_car_controller/{NODE}/charge_limit/state"])

    def test_invalid_charge_limit_ignored(self):
        self.command("charge_limit", "150")
        self.assertEqual(80, self.charge_control.percentage_threshold)
        self.charge_controls.save_config.assert_not_called()

    def test_remote_disabled_ignores_commands(self):
        self.ha = self.new_publisher(remote_enabled=False)
        self.command("horn", "PRESS")
        self.assertEqual([], self.car_commands())

    def test_unknown_car_and_payload_logged_without_vin(self):
        with self.assertLogs(ha_mqtt.logger, level=logging.WARNING) as logs:
            self.ha._on_message(self.client, None, message("psa_car_controller/psacc_000000000000/horn/set", "PRESS"))
            self.command("horn", "BOGUS")
            self.command("lock", "LOCK")
            self.command("lock", "UNLOCK")  # busy
        self.assertEqual(3, len(logs.output))
        for line in logs.output:
            self.assertNotIn(VIN, line)


if __name__ == '__main__':
    unittest.main()
