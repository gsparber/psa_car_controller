"""Car lookup, status reads and remote commands shared by the home, charge and climate pages."""
import logging
import time

import dash_bootstrap_components as dbc
from dash import ALL, callback_context, dcc, no_update
from dash.dependencies import Input, Output, State
from dash.exceptions import PreventUpdate

from psa_car_controller.common.utils import RateLimitException
from psa_car_controller.psacc.application.car_controller import PSACarController
from psa_car_controller.psacc.model.car import Car
from psa_car_controller.web.app import dash_app
from psa_car_controller.web.view import ui

logger = logging.getLogger(__name__)

# Commands sent from any page, so a tile can show "waiting for car" until the car reports the new state
PENDING_STORE = "app-pending"
ACTION = "car-action"
# How long a tile waits for the car to report the state it was asked for
PENDING_TIMEOUT_S = 120


def app() -> PSACarController:
    return PSACarController()


def get_car(vin) -> Car:
    cars = app().myp.vehicles_list
    return cars.get_car_by_vin(vin) or cars[0]


def remote_ready():
    return bool(app().remote_control and getattr(app().myp, "remote_client", None))


def read(getter, default=None):
    """The status tree has many optional nodes, read one leaf without a cascade of None checks."""
    try:
        value = getter()
    except (AttributeError, TypeError, IndexError, KeyError):
        return default
    return default if value is None else value


def is_charging(car):
    return read(lambda: car.status.get_energy("Electric").charging.status) == "InProgress"


def is_preconditioning(car):
    status = read(lambda: car.status.preconditionning.air_conditioning.status)
    return status is not None and status != "Disabled"


def car_select(select_id):
    """Car picker, hidden with a single car. persistence keeps the choice per page across visits."""
    cars = app().myp.vehicles_list
    return dbc.Select(id=select_id, value=cars[0].vin, persistence=True, persistence_type="local",
                      className="mb-3" if len(cars) > 1 else "d-none",
                      options=[{"label": car.label or car.vin, "value": car.vin} for car in cars])


def pending_store():
    return dcc.Store(id=PENDING_STORE, storage_type="session", data={})


def is_pending(pending, vin, action, current_state):
    entry = (pending or {}).get(vin, {}).get(action)
    if not entry or time.time() - entry["at"] > PENDING_TIMEOUT_S:
        return False
    return entry.get("target") is None or entry["target"] != current_state


def add_pending(pending, vin, action, target):
    pending = pending or {}
    pending.setdefault(vin, {})[action] = {"at": time.time(), "target": target}
    return pending


def send_command(action, car: Car):
    """Run a remote command. Returns (message, ok, target state the car should report or None)."""
    remote = app().myp.remote_client
    try:
        if action == "refresh":
            remote.wakeup(car.vin)
            return "Status update requested", True, None
        if action == "charge":
            target = not is_charging(car)
            remote.charge_now(car.vin, target)
            return ("Start charging" if target else "Stop charging") + " sent", True, target
        if action == "precond":
            target = not is_preconditioning(car)
            remote.preconditioning(car.vin, target)
            return ("Climate on" if target else "Climate off") + " sent", True, target
    except RateLimitException:
        return "Too many requests, try again later", False, None
    except Exception as e:  # pylint: disable=broad-except
        logger.exception("command %s failed", action)
        return f"Failed: {e}", False, None
    raise ValueError(f"unknown action {action}")


def action_button(action, vin, children, **kwargs):
    """Button that sends a remote command; the shared callback below handles every page."""
    return dbc.Button(children, id={"type": ACTION, "action": action, "vin": vin}, n_clicks=0, **kwargs)


@dash_app.callback(*ui.toast_outputs(), Output(PENDING_STORE, "data"),
                   Input({"type": ACTION, "action": ALL, "vin": ALL}, "n_clicks"),
                   State(PENDING_STORE, "data"),
                   prevent_initial_call=True)
def run_action(_clicks, pending):
    trigger = callback_context.triggered_id
    # Re-rendered buttons (status poll) fire this callback with n_clicks back at 0
    if not trigger or not callback_context.triggered[0]["value"]:
        raise PreventUpdate
    car = get_car(trigger["vin"])
    message, ok, target = send_command(trigger["action"], car)
    if ok and target is not None:
        pending = add_pending(pending, car.vin, trigger["action"], target)
    else:
        pending = no_update
    return (*ui.toast_message(message, ok), pending)
