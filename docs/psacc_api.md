# PSACC API
These links will work only if PSACC is on your computer, if it isn't please replace localhost by the ip of PSACC server.

1. Get the car state
    
   http://localhost:5000/get_vehicleinfo/YOURVIN

2. Get the car state from cache to avoid to use PSA API too much

   http://localhost:5000/get_vehicleinfo/YOURVIN?from_cache=1

3. Stop charge

   http://localhost:5000/charge_now/YOURVIN/0

4. Set hour to stop the charge to 6 am

   http://localhost:5000/charge_control?vin=YOURVIN&hour=6&minute=0 

5. Change car charge threshold to 80%

   http://localhost:5000/charge_control?vin=YOURVIN&percentage=80 

6. See the dashboard (only if record is enabled)

   http://localhost:5000

7. Refresh car state (ask car to send its state):

   http://localhost:5000/wakeup/YOURVIN

8. Start (1)/Stop (0) preconditioning

   http://localhost:5000/preconditioning/YOURVIN/1 or 0

9. Change charge hour (for example: set it to 22h30)

   http://localhost:5000/charge_hour?vin=YOURVIN&hour=22&minute=30

10. Honk the horn

    http://localhost:5000/horn/YOURVIN/count

11. Flash the lights (Duration is always roughly 10 seconds, regardless of set duration)

    http://localhost:5000/lights/YOURVIN/duration

12. Lock (1)/Unlock (0) the doors
   
    http://localhost:5000/lock_door/YOURVIN/1 or 0

13. Get config

    http://localhost:5000/settings

14. Change config parameter in config.ini (you need to restart the app after)

    http://localhost:5000/settings/electricity_config?night_price=0.2

    http://localhost:5000/settings/general?currency=%C2%A3

15. Get battery SOH

    http://localhost:5000/battery/soh/YOURVIN

16. Get the vehicle charging sessions
   
   http://localhost:5000/vehicles/chargings

17. Get the vehicle trips:
   
   http://localhost:5000/vehicles/trips

18. Status of the remote commands

    The commands above (charge_now, charge_hour, wakeup, preconditioning, preconditioning_program POST, horn,
    lights, lock_door) answer as before. When a command was sent to the car, the response carries its id in the
    `X-Correlation-Id` header, look its status up with:

    http://localhost:5000/command/CORRELATION_ID

    Commands sent to a car within the last hour, oldest first:

    http://localhost:5000/commands/YOURVIN

    ```json
    {"correlation_id": "...", "vin": "YOURVIN", "name": "/Doors", "sent_at": 1761775200.0,
     "status": "woken_up", "final": false, "pending": true, "error": null,
     "retried": false, "retried_as": null,
     "updates": [{"at": 1761775203.1, "return_code": "901", "reason": null}]}
    ```

    `status`: `sent` → `woken_up` (code 901, the server wakes the car up, the result follows) → `success` (0),
    `failed` (113, 500 or refused token twice), `timeout` (300, the car didn't answer, e.g. its connectivity was
    disabled after a week without driving), `not_compatible` (the car doesn't offer this service) or `retried`
    (the remote token was expired, the command was sent again as `retried_as`). `error` tells why a command failed,
    from the answer's `reason` or `resp_data`. Unknown codes are listed in `updates` only.
    `pending` is true while the car hasn't answered, for at most 2 minutes: the car runs one command at a time, the
    web UI waits for it before sending the next one (the API doesn't). 10 s after a success, the car status is
    fetched again (at most once a minute per car, not while PSA rate limits the account).

19. Maintenance, subscription rights and private mode

    http://localhost:5000/vehicle_services/YOURVIN

    ```json
    {"maintenance": {"mileage": 12000, "days": 150, "updated_at": "2026-10-29T08:00:00Z"},
     "commands": {"/VehCharge": true, "/VehCharge/state": true, "/ThermalPrecond": true, "/Doors": false,
                  "/Horn": false, "/Lights": false},
     "rights": {"RCC": {"name": "Remote charge", "mqtt_services": ["VehCharge"]}},
     "privacy": "None"}
    ```

    Maintenance (from the `maintenance` link of the car list) and rights (remote services the subscription covers)
    are asked for with the first status refresh, then once a day, not while PSA rate limits the account. `null`
    while unknown, or when the account doesn't offer them (then they aren't asked for again until a restart).
    `commands` is `null` when the rights don't name any known command; the web UI hides only the commands set to
    `false`. `privacy` is the car's private mode (`None`, `Geolocation`, `Full`: position and live data paused).
    Not verified with a real account: the format of the rights and maintenance answers.

20. Trips as Stellantis records them

    http://localhost:5000/vehicles/YOURVIN/stellantis_trips?since=2026-10-01T00:00:00%2B00:00&max_pages=2

    `{"trips": [...], "more": false}`: trips longer than 100 m since `since` (optional, ISO 8601), the raw entries
    of the API, up to `max_pages` pages (1 by default, at most 10); `more` is true when pages were left.
    Unrelated to the trips computed from the recorded positions (`/vehicles/trips`). Answers are kept 10 minutes.
    404 `not_available` when the account has no trips endpoint, 429 while PSA rate limits the account.
