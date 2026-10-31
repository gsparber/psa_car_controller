# FAQ 
## Frequent error
### 1. Can't get car model
The car model need to be known by the app. To find it, the app use the first 10 character of your VIN.
If the car isn't in the list we need to add it, to do that you need to edit the file here:

Go to [car_models.yml](https://github.com/flobz/psa_car_controller/blob/master/psa_car_controller/psacc/resources/car_models.yml)
and click on edit then copy cut an already existent model in the list and edit all properties that are incorrect for your model:
- Model
- First ten letter of the car VIN:  
- Useable Electric capacity in kw: [find it here](https://ev-database.org/cheatsheet/useable-battery-capacity-electric-car)
- Fuel capacity in liter
- abrp ref : [find it here](https://api.iternio.com/1/tlm/get_carmodels_list?api_key=32b2162f-9599-4647-8139-66e9f9528370)

Finally, set `Commit message` to :
```
feat: add model <replace by model name here>
```
And click on propose change. 

### 2. Error during activation 

If you have one of the following error : 
```shell
Error during activation: {'newversion': None, 'newversionurl': None, 'err': 'NOK:MAXNBTOOLS'}
Error during activation {'newversion': '2.0.0', 'newversionurl': 'http://m.inwebo.com/', 'err': 'NOK:FORBIDDEN'}
```

Your PSA account is locked because you make 20 sms activation. To unlock do this : 
1. Install on a smartphone mypeugeot, myopel etc, depending on your car brand
2. If the application is already installed uninstall and reinstall
3. You should be asked to give your credentials
4. Connect and test remote control, it should say that you need to reset your account
5. If the remote control work on your smartphone it will work with psa_car_controller

### 3. No data in dashboard
The app record your position and other information if "-r" argument is provided and if data are fetched.

Data are fetched in the following scenarios:
- charge control is enabled with "-c" program argument
- refresh is enabled with "-R"
- You use "get_vehicleinfo" API endpoint

With charge control and without "-R", the status is fetched every 5 minutes, and every 2 minutes while a car is charging.
"-R x" fetches it every x minutes in both cases. When PSA answers that there were too many requests,
the next fetch waits twice as long each time, up to one hour. Fetching more often can get the account blocked.


You need to make few trips to be able to see stats in the dashboard.

### 4. Permission error
It's happen if config file aren't writable by the user that launch the process.
To fix this go to the application directory and execute this command :
```
# if the user is launched by pi user do
sudo chown pi: -R . 
```

### 5. I don't receive SMS
The SMS authentication is used to be able to remote control your car.
 If your car doesn't have this functionality you should disable remote control when you start psa-car-controller 
by using `--remote-disable` argument.

### 6. SSL error
If you have an error more or less like this one:
```
[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: self signed certificate in certificate chain (_ssl.c:1056)
```
It's a problem on PSA server so don't open an issue, you can retry a second time and if it still doesn't work you just have to wait.
You can look to issue about this problem [here](https://github.com/flobz/psa_car_controller/search?q=ssl&type=issues).

### 7. AttributeError: 'PSACarController' object has no attribute 'chc'
The setup phase did't worked. Please retry the "user config" and "otp config".

### 8. AUTHENTICATION_FAILED
One of the following parameter is incorrect:
- email
- password
- country code
### 9. NEED_AUTHORIZATION error:
Don't open an issue, possible fix:
- wait for next day
- https://github.com/flobz/psa_car_controller/issues/395#issuecomment-1690043727
- https://github.com/flobz/psa_car_controller/issues/576#issuecomment-1631962500
### 10. No car position:
Look at this solution: https://github.com/flobz/psa_car_controller/issues/714#issuecomment-1868959995
### 11. Missing battery or charging data: check the raw status of the API
The API is moving the energy data from the deprecated `energy` list to a new `energies` list
(API changelog v4.10.1). PSACC reads `energy`, and falls back to `energies` when a status has no `energy` list.
To check what the API sends for your car, start PSACC once with `--log-raw-status`
(Docker: add it to `PSACC_OPTIONS`). The first status of each car is then logged as JSON, at the info level,
with the position, the links and ids replaced by `***` (tokens and VINs are masked by the log filter as usual).
Look for:
- `energy` and `energies`: which of the two lists is there, and whether they hold the same values
- `energies[].extension.electric.charging`: `status`, `plugged`, `chargingRate`, `remainingTime`, `nextDelayedTime`, `schedule`
- any field that looks like a charge limit (e.g. `chargingPowerLevel`, a target level in `schedule`)

Check the log before sharing it in an issue, and remove the option afterwards.
The charge limit of PSACC (`-c`) doesn't come from the API: PSACC stops the charge itself when the car reaches
the limit, using the delayed charge of the car, so the car needs to be reachable to stop
(see [#912](https://github.com/flobz/psa_car_controller/issues/912), [#1209](https://github.com/flobz/psa_car_controller/issues/1209)).
### 12. Which commands does my subscription cover? Help us find out
PSACC shows every command, and a command the car doesn't cover ends with `Not supported by this car`. The API lists
the services of a car (`/vehicle_services/YOURVIN`, field `rights`), but which service code a command needs is not
documented. Names like `Horn & Light` are only a hint, and no public report yet shows a refused command together with
the rights of the same car. So PSACC doesn't hide a command because of a missing code.

To help, start PSACC once with `--log-raw-status` (Docker: add it to `PSACC_OPTIONS`) and open an issue with:
- the log line `Raw vehicle rights`: VIN and ids are hidden, the serial numbers of the telematic units become `unit1`, `unit2`
- brand, model, year, and the services the brand app shows for the car (e-remote control, Connect Plus...)
- for each command you tried (charge, climate, horn, lights, doors): whether it worked or said `Not supported by this car`

A car that refuses a command is the most useful report. Check the log before sharing it, and remove the option afterwards.
