# Use PSACC as the vehicle of evcc

[evcc](https://evcc.io) can read the car from PSACC over HTTP. It then needs no Stellantis tokens of its own, and
the car is only polled once, by PSACC.

## Don't share a refresh token between tools

The PSA login appears to rotate refresh tokens: a refresh answers with a new token, and the one sent is likely used up
(this comes from the behaviour of other tools, it hasn't been measured on a real account). If so, PSACC, evcc, the Home
Assistant integration or any other tool using the same refresh token end up locking each other out: the first one to
refresh gets the new token, the others are answered `invalid_grant` ("grant is invalid"). Restarting doesn't help, as
they keep sending a token that was already used.

So don't copy tokens from one tool to another, log in once per tool, or use a single tool as below.

evcc shows the same error when its Peugeot, Citroën, DS, Opel or Vauxhall vehicle has a token that isn't valid anymore
([#34850](https://github.com/evcc-io/evcc/issues/34850), [#25874](https://github.com/evcc-io/evcc/issues/25874)),
which doesn't always mean that another tool used it: PSA also rejects tokens during its outages. If PSACC can't
refresh its token, log in again from the config web page.

## Security

**The API of PSACC has no authentication.** Whoever reaches its port can read the position of the car and send it
commands such as unlocking the doors. Keep it inside your LAN: don't forward the port to the internet, and use a VPN
to reach it from outside.

## Configuration

Add this to `evcc.yaml`, with the address of PSACC and your VIN in place of `psacc.local:5000` and `YOURVIN`, then
select the vehicle in your loadpoint:

```yaml
vehicles:
  - name: psacc
    type: custom
    title: e-208
    capacity: 50 # kWh
    soc:
      source: http
      uri: http://psacc.local:5000/get_vehicleinfo/YOURVIN?from_cache=1
      jq: '.energy[] | select(.type == "Electric") | .level'
      cache: 60s
    range:
      source: http
      uri: http://psacc.local:5000/get_vehicleinfo/YOURVIN?from_cache=1
      jq: '.energy[] | select(.type == "Electric") | .autonomy'
      cache: 60s
    status:
      source: http
      uri: http://psacc.local:5000/get_vehicleinfo/YOURVIN?from_cache=1
      jq: >-
        .energy[] | select(.type == "Electric") | .charging
        | if .status == "InProgress" then "C" elif .plugged then "B" else "A" end
      cache: 60s
    odometer:
      source: http
      uri: http://psacc.local:5000/get_vehicleinfo/YOURVIN?from_cache=1
      jq: .timed_odometer.mileage
      cache: 60s
```

Check it with `evcc vehicle`.

- `from_cache=1` answers with the status PSACC already has, so evcc doesn't add requests to the PSA API. The status is
  as recent as the refresh rate of PSACC (`-R`) and what the car sent, and stays the last one received when PSACC
  can't reach the API or lost its login: `data_age_seconds` in the same answer has its age.
- `cache: 60s` makes evcc ask PSACC once for all four values.
- `select(.type == "Electric")` picks the battery of a plug-in hybrid, whose answer also has a fuel entry.
- `status` is the charge state evcc expects: `A` not plugged, `B` plugged, `C` charging.
- If PSACC has no status yet and the PSA API doesn't answer, `/get_vehicleinfo` answers with an error (502 or 503),
  which evcc shows as an error for this vehicle.

### Optional: send commands to the car

These also need the remote control of PSACC to be set up, and each one goes through the PSA servers to the car. Add
them to the vehicle only if you need them, since the charger is what normally starts and stops a charge in evcc:

```yaml
    wakeup:
      source: http
      uri: http://psacc.local:5000/wakeup/YOURVIN
    chargeEnable:
      source: http
      uri: 'http://psacc.local:5000/charge_now/YOURVIN/{{if .chargeenable}}1{{else}}0{{end}}'
```

Check them with `evcc vehicle --wakeup`, `--start` and `--stop`. Too many wakeups drain the 12 V battery of the car.

Tested with evcc 0.318.1 and the answer of PSACC for a status from its test data, not with a real car.
