# Home Assistant through MQTT discovery

PSACC can publish your cars to Home Assistant through **your own MQTT broker** (for instance the Mosquitto add-on).
Home Assistant then creates a device per car with sensors and controls, without a second integration logging in to
your Stellantis account (no second OTP device).

It is **off by default**: nothing connects until you enable it. This client is separate from the connection PSACC
keeps with the Stellantis servers and only talks to the broker you configure.

## Setup

1. In Home Assistant, set up an MQTT broker and the MQTT integration (Settings → Devices & services → MQTT).
   Discovery must stay enabled (it is by default, prefix `homeassistant`).
2. In PSACC, open **Settings → Home Assistant**, fill in your broker and press **Save**. It applies right away, no
   restart: the old connection ends cleanly (availability `offline`), a new one connects and publishes discovery again.
   The page shows the state of the connection (*Connected*, *Connecting*, *Not connected* with the reason, *Off*).

   - **Password:** the field always stays empty, *Set* tells that one is stored. Empty keeps it, **Remove the stored
     password** removes it. After a change of the host or the port, or after turning TLS off, type it again: the web UI
     has no login, without this anyone who can open the page could send your stored password to their own server.
   - **TLS:** the certificate of the broker has to be trusted by the system. A red warning appears for a broker that
     isn't local with TLS off, because the password and the data of your cars would cross the network unencrypted.

   The settings are the `[Home Assistant MQTT]` section of `config.ini` (created with the defaults below on first
   start). Editing the file by hand still works and takes a restart:

   ```ini
   [Home Assistant MQTT]
   enabled = true
   # your broker, e.g. 192.168.1.10 or core-mosquitto
   host = 192.168.1.10
   port = 1883
   username = psacc
   password = secret
   # true to connect with TLS (the broker usually listens on 8883 then)
   tls = false
   discovery prefix = homeassistant
   base topic = psa_car_controller
   ```

   The password is stored in `config.ini` like the rest of the settings: protect the file. It is masked in the logs
   and left out of the `/settings` API answer. This section can't be changed through the `/settings/<section>` API.

## Entities

One device per car. Values are metric (%, km, kW): Home Assistant converts them to your units.

| Entity | Type | Notes |
|---|---|---|
| Battery | sensor | electric and hybrid cars |
| Fuel level | sensor | fuel and hybrid cars |
| Range | sensor | km |
| Charging status | sensor | as given by the car: `InProgress`, `Disconnected`, `Finished`, `Failure`… |
| Charging power | sensor | kW, estimated from the charging rate like on the home page |
| Odometer | sensor | km |
| Climate status | sensor | `Enabled`, `Disabled`, `Finished`… |
| Location | device_tracker | GPS position |
| Doors | lock | lock / unlock (a read-only binary sensor when remote control is disabled) |
| Last command | sensor | `sent`, `woken_up`, `success`, `failed`, `timeout`, `not_compatible`; attributes: command, error |
| Wake up | button | asks the car for a fresh status |
| Horn | button | |
| Flash lights | button | |
| Charging | switch | start / stop charging |
| Climate | switch | preconditioning on / off |
| Charge limit | number | threshold of PSACC's charge control, only when it runs (`-c`) |

The controls are only offered when remote control is enabled (no `--remote-disable` / `--offline`). They use the same
code as the web UI and the API, with the same limits:

- at most 6 wake ups per car in 20 minutes; more are refused and logged;
- one command at a time per car: a command sent while the car hasn't answered the previous one (at most 2 min) is
  ignored and logged;
- the answer of the car shows up in the *Last command* sensor.

States are published (retained) after each status refresh and every minute from the cached status, only when they
change: PSACC doesn't ask the Stellantis API more often because of Home Assistant.

## Topics

- `psa_car_controller/status`: `online` / `offline` (last will), availability of all entities.
- `homeassistant/<component>/<car id>/<entity>/config`: retained discovery configs.
- `psa_car_controller/<car id>/<entity>/state` (and `/attributes`): states.
- `psa_car_controller/<car id>/<entity>/set`: commands from Home Assistant.

`<car id>` is `psacc_` followed by a hash of the VIN: the VIN itself is never published or logged by this feature.
PSACC subscribes to `homeassistant/status` and publishes everything again when Home Assistant restarts.

## Removing it

Turn it off in the settings (or set `enabled = false` and restart). The retained discovery configs stay on the broker:
delete the device in Home Assistant (MQTT device page → Delete), or clear the retained
`homeassistant/+/psacc_…/+/config` topics with an MQTT client. The same goes for a car removed from your account, and
for the old broker or discovery prefix after changing them: their entities stay there as unavailable.
