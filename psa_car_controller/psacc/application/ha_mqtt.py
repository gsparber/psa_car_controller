"""Publish the cars and their remote commands to Home Assistant through the user's own MQTT broker.

Opt-in: nothing connects unless "[Home Assistant MQTT]" is enabled in config.ini with a host. It uses its own
paho client and never talks to the Stellantis broker. Entities come from MQTT discovery (retained configs), values are
sent in metric units (Home Assistant converts them to the units of its user). Topics carry a hash of the VIN, never the
VIN itself, and the VIN is never logged here.
"""
import hashlib
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, List, Optional

import paho.mqtt.client as mqtt

from psa_car_controller.common.utils import RateLimitException
from psa_car_controller.psa.remote_commands import RemoteCommand
from psa_car_controller.psacc.model.car import Car

logger = logging.getLogger(__name__)

ONLINE = "online"
OFFLINE = "offline"
ON = "ON"
OFF = "OFF"
LOCK = "LOCK"
UNLOCK = "UNLOCK"
PRESS = "PRESS"
# states are read from the cached car status, only the changed ones are sent
PUBLISH_INTERVAL = 60
HORN_COUNT = 1
LIGHTS_DURATION = 10  # s, the car flashes them about 10 s whatever is asked
RECONNECT_MIN_DELAY = 1
RECONNECT_MAX_DELAY = 120

# entity key (also its topic level) -> discovery fields; "json_attributes" adds an attributes topic
SENSORS = {
    "battery_level": {"name": "Battery", "device_class": "battery", "unit_of_measurement": "%",
                      "state_class": "measurement"},
    "fuel_level": {"name": "Fuel level", "unit_of_measurement": "%", "state_class": "measurement",
                   "icon": "mdi:gas-station"},
    "range": {"name": "Range", "device_class": "distance", "unit_of_measurement": "km",
              "state_class": "measurement"},
    "charging_status": {"name": "Charging status", "icon": "mdi:ev-station"},
    "charging_power": {"name": "Charging power", "device_class": "power", "unit_of_measurement": "kW",
                       "state_class": "measurement", "suggested_display_precision": 1},
    "odometer": {"name": "Odometer", "device_class": "distance", "unit_of_measurement": "km",
                 "state_class": "total_increasing"},
    "climate_status": {"name": "Climate status", "icon": "mdi:air-conditioner"},
    "last_command": {"name": "Last command", "icon": "mdi:remote", "entity_category": "diagnostic",
                     "json_attributes": True},
}
BUTTONS = {
    "wakeup": {"name": "Wake up", "icon": "mdi:refresh"},
    "horn": {"name": "Horn", "icon": "mdi:bullhorn"},
    "lights": {"name": "Flash lights", "icon": "mdi:car-light-high"},
}
SWITCHES = {
    "charging": {"name": "Charging", "icon": "mdi:ev-plug-type2"},
    "climate": {"name": "Climate", "icon": "mdi:air-conditioner"},
}
NUMBERS = {
    "charge_limit": {"name": "Charge limit", "unit_of_measurement": "%", "min": 0, "max": 100, "step": 1,
                     "mode": "slider", "icon": "mdi:battery-charging-80"},
}
ELECTRIC_ONLY = {"battery_level", "charging_status", "charging_power", "charging", "charge_limit"}
FUEL_ONLY = {"fuel_level"}


def node_id(vin: str) -> str:
    """Stable id of a car in topics and unique ids, without its VIN."""
    return "psacc_" + hashlib.sha256(vin.encode("utf-8")).hexdigest()[:12]


def charge_power_kw(rate_kmh, battery_kwh, level, range_km):
    """Estimated like the home page does (web/view/status_text.py, not imported: it would load the Dash app): the
    charging rate is range gained per hour, times the energy a km currently takes."""
    values = (rate_kmh, battery_kwh, level, range_km)
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 for v in values):
        return None
    return rate_kmh * battery_kwh * level / 100 / range_km


def state_text(value) -> str:
    """59.0 as "59": the API gives whole numbers as floats."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _read(getter):
    try:
        return getter()
    except (AttributeError, TypeError, IndexError, KeyError):
        return None


def car_states(car: Car, charge_limit=None) -> Dict[str, object]:
    """Values of the entities of a car from its cached status; None when unknown."""
    status = car.status
    if status is None:
        return {}
    elec = _read(lambda: status.get_energy("Electric"))
    fuel = _read(lambda: status.get_energy("Fuel"))
    states = {"odometer": _read(lambda: status.timed_odometer.mileage)}
    if car.has_battery():
        level = _read(lambda: elec.level)
        autonomy = _read(lambda: elec.autonomy)
        charging_status = _read(lambda: elec.charging.status)
        states["battery_level"] = level
        states["range"] = autonomy
        states["charging_status"] = charging_status
        states["charging"] = None if charging_status is None else (ON if charging_status == "InProgress" else OFF)
        power = None
        if charging_status == "InProgress":
            power = charge_power_kw(_read(lambda: elec.charging.charging_rate), car.battery_power, level, autonomy)
        states["charging_power"] = round(power, 2) if power is not None else (0 if charging_status else None)
        states["charge_limit"] = charge_limit
    if car.has_fuel():
        states["fuel_level"] = _read(lambda: fuel.level)
        if not car.has_battery():
            states["range"] = _read(lambda: fuel.autonomy)
    climate = _read(lambda: status.preconditionning.air_conditioning.status)
    states["climate_status"] = climate
    states["climate"] = None if climate is None else (OFF if climate in ("Disabled", "Finished") else ON)
    locked_state = _read(lambda: status.doors_state.locked_state)
    states["lock"] = None if not locked_state else ("UNLOCKED" if "Unlocked" in locked_state else "LOCKED")
    coordinates = _read(lambda: status.last_position.geometry.coordinates)
    if coordinates and len(coordinates) >= 2 and None not in coordinates[:2]:
        states["location"] = {"latitude": coordinates[1], "longitude": coordinates[0], "gps_accuracy": 0}
    return states


class HomeAssistantMqtt:
    """MQTT discovery publisher and command receiver for Home Assistant."""

    def __init__(self, config, psacc, charge_controls: Callable[[], object] = lambda: None,
                 remote_enabled: bool = True,
                 client_factory=lambda: mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)):
        self.config = config
        self.psacc = psacc
        self.charge_controls = charge_controls
        self.remote_enabled = remote_enabled
        self.base = config.base_topic.strip("/")
        self.prefix = config.discovery_prefix.strip("/")
        self.availability_topic = f"{self.base}/status"
        self.client = client_factory()
        self._published: Dict[str, str] = {}
        self._lock = threading.Lock()
        self._timer: Optional[threading.Timer] = None
        self._stopped = False
        # commands run here: they may refresh tokens or talk to the PSA broker, the paho loop must not wait for them
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ha-mqtt-command")

    # ---- connection
    def start(self):
        client = self.client
        if self.config.username:
            client.username_pw_set(self.config.username, self.config.password)
        if self.config.tls:
            client.tls_set()
        client.will_set(self.availability_topic, OFFLINE, qos=1, retain=True)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        client.reconnect_delay_set(RECONNECT_MIN_DELAY, RECONNECT_MAX_DELAY)
        logger.info("Home Assistant MQTT: connecting to %s:%s", self.config.host, self.config.port)
        client.connect_async(self.config.host, int(self.config.port), keepalive=60)
        client.loop_start()
        self.psacc.info_callback.append(self.publish_states)
        remote_client = getattr(self.psacc, "remote_client", None)
        if remote_client is not None:
            remote_client.command_callbacks.append(self.publish_command)
        self._schedule()

    def stop(self):
        self._stopped = True
        if self._timer is not None:
            self._timer.cancel()
        if self.publish_states in self.psacc.info_callback:
            self.psacc.info_callback.remove(self.publish_states)
        remote_client = getattr(self.psacc, "remote_client", None)
        if remote_client is not None and self.publish_command in remote_client.command_callbacks:
            remote_client.command_callbacks.remove(self.publish_command)
        try:
            self.client.publish(self.availability_topic, OFFLINE, qos=1, retain=True)
            self.client.disconnect()
        finally:
            self.client.loop_stop()
            self._executor.shutdown(wait=False)

    def _schedule(self):
        if self._stopped:
            return
        self._timer = threading.Timer(PUBLISH_INTERVAL, self._periodic)
        self._timer.daemon = True
        self._timer.start()

    def _periodic(self):
        try:
            self.publish_states()
        except Exception:  # pylint: disable=broad-except
            logger.exception("Home Assistant MQTT: publish states")
        self._schedule()

    def _on_connect(self, client, userdata, flags, reason_code, *_):  # pylint: disable=unused-argument
        if reason_code.is_failure:
            logger.error("Home Assistant MQTT: connection refused: %s", reason_code)
            return
        logger.info("Home Assistant MQTT: connected")
        client.subscribe(f"{self.base}/+/+/set")
        client.subscribe(f"{self.prefix}/status")
        self.publish_all()

    @staticmethod
    def _on_disconnect(client, userdata, flags, reason_code, *_):  # pylint: disable=unused-argument
        if reason_code.is_failure:
            logger.warning("Home Assistant MQTT: disconnected (%s), reconnecting", reason_code)

    def publish_all(self):
        """Availability, discovery and every state: after a (re)connect or a restart of Home Assistant."""
        with self._lock:
            self._published.clear()
        self.client.publish(self.availability_topic, ONLINE, qos=1, retain=True)
        self.publish_discovery()
        self.publish_states()

    def _publish(self, topic, payload, retain=True, force=False):
        if not isinstance(payload, str):
            payload = json.dumps(payload, default=str)
        with self._lock:
            if not force and self._published.get(topic) == payload:
                return
            self._published[topic] = payload
        self.client.publish(topic, payload, qos=1, retain=retain)

    # ---- discovery
    def _cars(self) -> List[Car]:
        return list(self.psacc.vehicles_list)

    def _charge_control(self, vin):
        charge_controls = self.charge_controls()
        return charge_controls.get(vin) if charge_controls is not None else None

    def _entities(self, car: Car):
        """(component, key, fields) of the entities a car offers."""
        def wanted(key):
            if key in ELECTRIC_ONLY and not car.has_battery():
                return False
            return not (key in FUEL_ONLY and not car.has_fuel())

        entities = [("sensor", key, fields) for key, fields in SENSORS.items() if wanted(key)]
        entities.append(("device_tracker", "location", {"name": "Location", "icon": "mdi:car"}))
        if self.remote_enabled:
            entities.append(("lock", "lock", {"name": "Doors"}))
        else:  # a lock entity needs a command topic
            entities.append(("binary_sensor", "lock", {"name": "Doors", "device_class": "lock"}))
        if self.remote_enabled:
            entities += [("button", key, fields) for key, fields in BUTTONS.items()]
            entities += [("switch", key, fields) for key, fields in SWITCHES.items() if wanted(key)]
            if self._charge_control(car.vin) is not None:
                entities += [("number", key, fields) for key, fields in NUMBERS.items() if wanted(key)]
        return entities

    def _device(self, car: Car, node: str) -> dict:
        return {"identifiers": [node], "name": car.label or car.brand or "Car", "manufacturer": car.brand,
                "model": car.label}

    def discovery_config(self, car: Car, component, key, fields) -> dict:
        node = node_id(car.vin)
        topic = f"{self.base}/{node}/{key}"
        config = {"unique_id": f"{node}_{key}", "object_id": f"{node}_{key}",
                  "availability_topic": self.availability_topic, "device": self._device(car, node)}
        fields = dict(fields)
        if fields.pop("json_attributes", False):
            config["json_attributes_topic"] = f"{topic}/attributes"
        config.update(fields)
        if component == "device_tracker":
            config.update({"json_attributes_topic": f"{topic}/attributes", "source_type": "gps"})
        elif component == "button":
            config.update({"command_topic": f"{topic}/set", "payload_press": PRESS})
        else:
            config["state_topic"] = f"{topic}/state"
        if component == "switch":
            config.update({"command_topic": f"{topic}/set", "payload_on": ON, "payload_off": OFF,
                           "state_on": ON, "state_off": OFF})
        elif component == "number":
            config["command_topic"] = f"{topic}/set"
        elif component == "lock":
            config.update({"command_topic": f"{topic}/set", "payload_lock": LOCK, "payload_unlock": UNLOCK,
                           "state_locked": "LOCKED", "state_unlocked": "UNLOCKED"})
        elif component == "binary_sensor":  # device class lock: on means unlocked
            config.update({"payload_on": "UNLOCKED", "payload_off": "LOCKED"})
        return config

    def publish_discovery(self):
        for car in self._cars():
            node = node_id(car.vin)
            for component, key, fields in self._entities(car):
                self._publish(f"{self.prefix}/{component}/{node}/{key}/config",
                              self.discovery_config(car, component, key, fields), force=True)

    # ---- states
    def publish_states(self):
        for car in self._cars():
            charge_control = self._charge_control(car.vin)
            limit = charge_control.percentage_threshold if charge_control is not None else None
            node = node_id(car.vin)
            for key, value in car_states(car, limit).items():
                if value is None:
                    continue
                if key == "location":
                    self._publish(f"{self.base}/{node}/location/attributes", value)
                else:
                    self._publish(f"{self.base}/{node}/{key}/state", state_text(value))

    def publish_command(self, command: RemoteCommand):
        """State of the last command of a car (sent, woken_up, success, failed, timeout...)."""
        topic = f"{self.base}/{node_id(command.vin)}/last_command"
        self._publish(f"{topic}/attributes", {"command": command.name, "error": command.error,
                                              "sent_at": command.sent_at, "retried": command.retried})
        self._publish(f"{topic}/state", command.status)

    # ---- commands
    def _on_message(self, client, userdata, msg):  # pylint: disable=unused-argument
        payload = msg.payload.decode("utf-8", errors="replace").strip()
        if msg.topic == f"{self.prefix}/status":
            if payload == ONLINE:  # Home Assistant restarted: it needs the configs and states again
                self.publish_all()
            return
        parts = msg.topic.split("/")
        if len(parts) < 3:
            return
        node, key = parts[-3], parts[-2]
        car = next((c for c in self._cars() if node_id(c.vin) == node), None)
        if car is None:
            logger.warning("Home Assistant MQTT: command for an unknown car %s", node)
            return
        self._executor.submit(self._run_command, car, key, payload)

    def _run_command(self, car: Car, key, payload):
        node = node_id(car.vin)
        try:
            if key == "charge_limit":
                self._set_charge_limit(car, payload)
                return
            self._send_remote_command(car, key, payload)
        except RateLimitException as e:
            logger.warning("Home Assistant MQTT: %s for %s refused: %s", key, node, e)
        except Exception as e:  # pylint: disable=broad-except
            logger.error("Home Assistant MQTT: %s for %s failed: %s", key, node, e)

    def _set_charge_limit(self, car: Car, payload):
        charge_control = self._charge_control(car.vin)
        if charge_control is None:
            logger.warning("Home Assistant MQTT: charge control isn't enabled (-c), can't set the charge limit")
            return
        value = int(float(payload))
        if not 0 <= value <= 100:
            raise ValueError(f"charge limit {value} not within 0-100")
        charge_control.percentage_threshold = value
        self.charge_controls().save_config()
        self.publish_states()

    def _command(self, remote, car: Car, key, payload) -> Optional[Callable[[], object]]:
        vin = car.vin
        commands = {
            ("wakeup", PRESS): lambda: remote.wakeup(vin),
            ("horn", PRESS): lambda: remote.horn(vin, HORN_COUNT),
            ("lights", PRESS): lambda: remote.lights(vin, LIGHTS_DURATION),
            ("charging", ON): lambda: remote.charge_now(vin, True),
            ("charging", OFF): lambda: remote.charge_now(vin, False),
            ("climate", ON): lambda: remote.preconditioning(vin, True),
            ("climate", OFF): lambda: remote.preconditioning(vin, False),
            ("lock", LOCK): lambda: remote.lock_door(vin, True),
            ("lock", UNLOCK): lambda: remote.lock_door(vin, False),
        }
        return commands.get((key, payload))

    def _send_remote_command(self, car: Car, key, payload):
        node = node_id(car.vin)
        remote = getattr(self.psacc, "remote_client", None)
        if not self.remote_enabled or remote is None:
            logger.warning("Home Assistant MQTT: remote control is disabled, %s ignored", key)
            return
        command = self._command(remote, car, key, payload)
        if command is None:
            logger.warning("Home Assistant MQTT: unknown command %s %s for %s", key, payload[:20], node)
            return
        busy = remote.commands.pending(car.vin)
        if busy:
            # the car runs one command at a time, like in the web UI
            logger.warning("Home Assistant MQTT: %s for %s ignored, the car is still busy with %s",
                           key, node, busy.get("name"))
            return
        logger.info("Home Assistant MQTT: %s %s for %s", key, payload, node)
        with remote.track_commands() as sent:
            command()
        for correlation_id in sent:
            sent_command = remote.commands.get(correlation_id)
            if sent_command is not None:
                self.publish_command(sent_command)
