import logging

import os
from datetime import datetime
from functools import wraps

from flask import abort, jsonify, make_response, request, send_file, Response as FlaskResponse
from pydantic import BaseModel

from oauth2_client.credentials_manager import OAuthError

from psa_car_controller.common.utils import AuthenticationRequiredException, RateLimitException, \
    validate_preconditioning_programs
from urllib3.exceptions import HTTPError

from psa_car_controller.psa.RemoteClient import RemoteException
from psa_car_controller.psa.oauth import AUTH_LOGIN_REQUIRED, refresh_can_recover, rejected_grant_hint
from psa_car_controller.psa.connected_car_api.rest import ApiException
from psa_car_controller.psacc.application.vehicle_services import MAX_TRIPS_PAGES, UNSUPPORTED_STATUSES
from psa_car_controller.psacc.application.car_controller import PSACarController
from psa_car_controller.psacc.application.health import get_health, get_max_mqtt_auth_failures
from psa_car_controller.psacc.repository.db import Database
from psa_car_controller.web.app import app

from psa_car_controller.psacc.model.car import Cars
from psa_car_controller.psacc.model.car_status import status_age_seconds
from psa_car_controller.psacc.repository.trips import Trips

from psa_car_controller.psacc.application.charging import Charging
from psa_car_controller.psacc.application.vehicle_picture import get_vehicle_picture

import json

from psa_car_controller.web.tools.utils import convert_to_number_if_number_else_return_str

logger = logging.getLogger(__name__)

STYLE_CACHE = None
APP = PSACarController()


def json_response(json: str, status=200):
    return app.response_class(
        response=json,
        status=status,
        mimetype='application/json'
    )


# correlation id of the command a remote control endpoint sent: look its status up on /command/<id>
CORRELATION_ID_HEADER = "X-Correlation-Id"


def tracks_command(view):
    """Add the correlation id of the command the view publishes as a header, the body stays as it was."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        with APP.myp.remote_client.track_commands() as sent:
            response = make_response(view(*args, **kwargs))
        if sent:
            response.headers[CORRELATION_ID_HEADER] = sent[-1]
        return response
    return wrapper


@app.errorhandler(AuthenticationRequiredException)
def handle_authentication_required(error):
    """Return a machine-readable 503 instead of a 500 traceback.

    Without this, an expired refresh token surfaces as
    "TypeError: can only concatenate str (not NoneType) to str" deep inside the
    generated API client, which tells the caller nothing about what to do.
    """
    logger.error("API call rejected, not authenticated: %s", error)
    return jsonify({"error": "authentication_required", "message": str(error)}), 503


@app.errorhandler(OAuthError)
def handle_oauth_error(error):
    """An expired or revoked grant needs a new login, not a retry."""
    logger.error("API call rejected, OAuth error: %s", error)
    if refresh_can_recover(error):
        message = f"OAuth error, the login is kept, please try again later: {error}."
    else:
        message = f"OAuth error, please reconnect from the config web page: {error}.{rejected_grant_hint(error)}"
    return jsonify({"error": "authentication_required", "message": message}), 503


@app.route('/health')
def health():
    # myp is not set yet when the app waits for its first configuration (--web-conf)
    myp = getattr(APP, "myp", None)
    body, status = get_health(myp.remote_client if myp else None, APP.remote_control,
                              get_max_mqtt_auth_failures(), myp.is_authenticated() if myp else False,
                              myp.auth_problem() if myp else AUTH_LOGIN_REQUIRED)
    return jsonify(body), status


@app.route('/get_vehicles')
def get_vehicules():
    response = app.response_class(
        response=json.dumps(APP.myp.get_vehicles(), default=lambda car: car.to_dict()),
        status=200,
        mimetype='application/json'
    )
    return response


@app.route('/get_vehicleinfo/<string:vin>')
def get_vehicle_info(vin):
    from_cache = int(request.args.get('from_cache', 0)) == 1
    info = APP.myp.get_vehicle_info(vin, from_cache)
    if info is None:
        # Every attempt failed upstream; say so rather than raising AttributeError.
        logger.warning("get_vehicle_info returned no data for %s", vin)
        return jsonify({"error": "no_data", "message": "No vehicle info available from the API"}), 502
    data = info.to_dict()
    data["data_age_seconds"] = status_age_seconds(info)
    response = app.response_class(
        response=json.dumps(data, default=str),
        status=200,
        mimetype='application/json'
    )
    car = APP.myp.vehicles_list.get_car_by_vin(vin)
    if car is not None and car.status_outdated:
        # the API failed for a while, this is the last status it returned
        response.headers["X-Status-Outdated"] = str(car.status_failures)
    return response


@app.route("/style.json")
def get_style():
    global STYLE_CACHE
    if not STYLE_CACHE:
        with open(app.root_path + "/assets/style.json", "r", encoding="utf-8") as f:
            res = json.loads(f.read())
            STYLE_CACHE = res
    url_root = request.url_root
    STYLE_CACHE["sprite"] = url_root + "assets/sprites/osm-liberty"
    return jsonify(STYLE_CACHE)


@app.route('/charge_now/<string:vin>/<int:charge>')
@tracks_command
def charge_now(vin, charge):
    return jsonify(APP.myp.remote_client.charge_now(vin, charge != 0))


@app.route('/charge_hour')
@tracks_command
def change_charge_hour():
    return jsonify(APP.myp.remote_client.change_charge_hour(request.args['vin'],
                                                            request.args['hour'],
                                                            request.args['minute']))


@app.route('/wakeup/<string:vin>')
@tracks_command
def wakeup(vin):
    try:
        return jsonify(APP.myp.remote_client.wakeup(vin))
    except RateLimitException:
        return jsonify({"error": "Wakeup rate limit exceeded"})


@app.route('/preconditioning/<string:vin>/<int:activate>')
@tracks_command
def preconditioning(vin, activate):
    try:
        return jsonify(APP.myp.remote_client.preconditioning(vin, activate))
    except RemoteException as error:
        return json_response(json.dumps({"error": str(error)}), status=409)


# Read (GET) or set (POST) the four preconditioning schedules. The POST body is
# a JSON object with program1..program4, each {day:[7], hour, minute, on}.
@app.route('/preconditioning_program/<string:vin>', methods=['GET', 'POST'])
@tracks_command
def preconditioning_program(vin):
    if request.method == 'POST':
        try:
            programs = validate_preconditioning_programs(request.get_json(force=True))
        except (ValueError, TypeError) as error:
            return json_response(json.dumps({"error": str(error)}), status=400)
        published = APP.myp.remote_client.set_preconditioning_program(vin, programs)
        if not published:
            # The command never reached the car (e.g. the remote token could not
            # be refreshed). Report it so the caller doesn't treat it as applied.
            return json_response(json.dumps({"error": "command not sent to vehicle"}), status=502)
        return jsonify(programs)
    return jsonify(APP.myp.remote_client.get_preconditioning_program(vin))


@app.route('/position/<string:vin>')
def get_position(vin):
    res = APP.myp.get_vehicle_info(vin)
    try:
        coordinates = res.last_position.geometry.coordinates
    except AttributeError:
        return jsonify({'error': 'last_position not available from api'})
    longitude, latitude = coordinates[:2]
    if len(coordinates) == 3:  # altitude is not always available
        altitude = coordinates[2]
    else:
        altitude = None
    return jsonify(
        {"longitude": longitude, "latitude": latitude, "altitude": altitude,
         "url": f"https://maps.google.com/maps?q={latitude},{longitude}"})


# Set a battery threshold and schedule an hour to stop the charge
@app.route('/charge_control')
def get_charge_control():
    logger.info(request)
    vin = request.args['vin']
    if APP.chc:
        charge_control = APP.chc.get(vin)
        if charge_control is None:
            return jsonify({"error": "VIN not in list"})
        if 'hour' in request.args and 'minute' in request.args:
            charge_control.set_stop_hour([int(request.args["hour"]), int(request.args["minute"])])
        if 'percentage' in request.args:
            charge_control.percentage_threshold = int(request.args['percentage'])
        APP.chc.save_config()
        return jsonify(charge_control.get_dict())
    error = "Charge control not setup check your PSACC configuration and logs"
    logger.error(error)
    return jsonify({"error": error})


@app.route('/positions')
def get_recorded_position():
    return FlaskResponse(Database.get_recorded_position(), mimetype='application/json')


@app.route('/abrp')
def abrp():
    vin = request.args.get('vin', None)
    enable = request.args.get('enable', None)
    token = request.args.get('token', None)
    if vin is not None and enable is not None:
        if enable == '1':
            APP.myp.abrp.abrp_enable_vin.add(vin)
        else:
            APP.myp.abrp.abrp_enable_vin.discard(vin)
    if token is not None:
        APP.myp.abrp.token = token
    return jsonify(dict(APP.myp.abrp))


@app.route('/commands/<string:vin>')
def get_commands(vin):
    """Commands sent to the car within the last hour, oldest first, with the answers of the car."""
    return jsonify(APP.myp.remote_client.commands.to_list(vin))


@app.route('/command/<string:correlation_id>')
def get_command(correlation_id):
    command = APP.myp.remote_client.commands.get_dict(correlation_id)
    if command is None:
        return jsonify({"error": "unknown_command", "message": "No command with this id in the last hour"}), 404
    return jsonify(command)


@app.after_request
def after_request(response):
    header = response.headers
    header['Access-Control-Allow-Origin'] = '*'
    header['Access-Control-Expose-Headers'] = CORRELATION_ID_HEADER
    return response


@app.route('/horn/<string:vin>/<int:count>')
@tracks_command
def horn(vin, count):
    try:
        return jsonify(APP.myp.remote_client.horn(vin, count))
    except RateLimitException:
        return jsonify({"error": "Horn rate limit exceeded"})


@app.route('/lights/<string:vin>/<int:duration>')
@tracks_command
def lights(vin, duration):
    try:
        return jsonify(APP.myp.remote_client.lights(vin, duration))
    except RateLimitException:
        return jsonify({"error": "Lights rate limit exceeded"})


@app.route('/lock_door/<string:vin>/<int:lock>')
@tracks_command
def lock_door(vin, lock):
    try:
        return jsonify(APP.myp.remote_client.lock_door(vin, lock))
    except RateLimitException:
        return jsonify({"error": "Locks rate limit exceeded"})


@app.route('/settings/<string:section>')
def settings_section(section: str):
    config_section: BaseModel = getattr(APP.config, section.capitalize())
    for key, value in request.args.items():
        typed_value = convert_to_number_if_number_else_return_str(value)
        setattr(config_section, key, typed_value)
        APP.config.write_config()
    return json_response(config_section.model_dump_json())


@app.route('/vehicles/trips')
def get_trips():
    try:
        if not APP.myp.vehicles_list:
            return jsonify({})
        car = APP.myp.vehicles_list[0]
        trips_by_vin = Trips.get_trips(Cars([car]))
        trips = trips_by_vin[car.vin]
        trips_as_dict = trips.get_trips_as_dict()
        return jsonify(trips_as_dict)
    except (IndexError, TypeError, KeyError):
        logger.debug("Failed to get trips, there is probably not enough data yet:", exc_info=True)
        return jsonify([])


@app.route('/vehicle_services/<string:vin>')
def get_vehicle_services(vin):
    car = APP.myp.vehicles_list.get_car_by_vin(vin)
    if car is None:
        return jsonify({"error": "unknown_vin"}), 404
    body = APP.myp.vehicle_services.to_dict(vin)
    privacy = getattr(getattr(car.status, "privacy", None), "state", None)
    body["privacy"] = privacy
    return jsonify(body)


@app.route('/vehicles/<string:vin>/stellantis_trips')
def get_stellantis_trips(vin):
    car = APP.myp.vehicles_list.get_car_by_vin(vin)
    if car is None:
        return jsonify({"error": "unknown_vin"}), 404
    try:
        since = request.args.get("since")
        since = datetime.fromisoformat(since) if since else None
        max_pages = min(max(int(request.args.get("max_pages", 1)), 1), MAX_TRIPS_PAGES)
    except ValueError as e:
        return jsonify({"error": "invalid_argument", "message": str(e)}), 400
    try:
        return jsonify(APP.myp.get_stellantis_trips(car, since, max_pages))
    except RateLimitException as e:
        return jsonify({"error": "rate_limited", "message": str(e)}), 429
    except ApiException as e:
        if e.status in UNSUPPORTED_STATUSES:
            return jsonify({"error": "not_available", "message": f"HTTP {e.status} from the API"}), 404
        logger.warning("stellantis trips of %s: HTTP %s %s", vin, e.status, e.reason)
        return jsonify({"error": "api_error", "message": f"HTTP {e.status} from the API"}), 502
    except (HTTPError, ValueError) as e:
        logger.warning("stellantis trips of %s: %s", vin, e)
        return jsonify({"error": "api_error", "message": str(e)}), 502


@app.route('/vehicles/chargings')
def get_chargings():
    try:
        chargings = Charging.get_chargings()
        return jsonify(chargings)
    except (IndexError, TypeError):
        logger.debug("Failed to get chargings, there is probably not enough data yet:", exc_info=True)
        return jsonify([])


@app.route('/vehicles/chargings/estimated')
def get_estimated_chargings():
    try:
        return jsonify(Charging.get_estimated_chargings(APP.myp.vehicles_list))
    except (IndexError, TypeError, ValueError):
        logger.debug("Failed to estimate chargings, there is probably not enough data yet:", exc_info=True)
        return jsonify([])


@app.route('/settings')
def settings():
    return json_response(APP.config.model_dump_json(exclude={"Home_Assistant_MQTT": {"password"},
                                                             "Login_Worker": {"url"}}))


@app.route('/battery/soh/<string:vin>')
def db(vin: str):
    soh = Database.get_last_soh_by_vin(vin)
    return jsonify({"soh": soh})


@app.route('/vehicle_picture/<string:vin>')
def vehicle_picture(vin):
    if APP.myp.vehicles_list.get_car_by_vin(vin) is None:
        abort(404)
    pictures = get_vehicle_picture(APP.myp)
    path = pictures.get_cached(vin)
    if path is None:
        pictures.fetch_in_background(vin)
        abort(404)
    return send_file(os.path.abspath(path), max_age=7 * 24 * 3600)


# The service worker must be served from the app root, its scope can't be wider than its own path
@app.route('/sw.js')
def service_worker():
    response = send_file(os.path.join(os.path.dirname(__file__), "..", "static", "sw.js"),
                         mimetype="text/javascript", max_age=0)
    response.headers["Cache-Control"] = "no-cache"
    return response
