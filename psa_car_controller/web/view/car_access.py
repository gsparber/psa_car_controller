"""Car lookup, status reads and remote commands shared by the home, charge and climate pages."""
import logging
import threading
import time

import dash_bootstrap_components as dbc
from dash import ALL, callback_context, dcc, html, no_update
from dash.dependencies import Input, Output, State
from dash.exceptions import PreventUpdate

from psa_car_controller.common.utils import RateLimitException
from psa_car_controller.psa.remote_commands import FAILED, NOT_COMPATIBLE, PENDING_TIMEOUT, RC_WAKEUP_FORWARDED, \
    RETRIED, SUCCESS, TIMEOUT
from psa_car_controller.psacc.application.car_controller import PSACarController
from psa_car_controller.psacc.model.car import Car
from psa_car_controller.web.app import dash_app
from psa_car_controller.web.view import ui

logger = logging.getLogger(__name__)

# Cars a request is sending a command to right now
_sending = set()
_sending_lock = threading.Lock()
# Commands sent from any page, so a tile can show "waiting for car" until the car reports the new state
PENDING_STORE = "app-pending"
ACTION = "car-action"
# How long a tile waits for the car to report the state it was asked for
PENDING_TIMEOUT_S = 120
# Last command sent from the UI and the line following its answers: sent -> car woken up -> result
COMMAND_STORE = "app-command"
COMMAND_STATUS = "app-command-status"
COMMAND_POLL = "app-command-poll"
COMMAND_POLL_MS = 3000
# How long the result stays on screen
COMMAND_SHOWN_S = 8
REMOTE_OFF_HINT = ["Started with ", ui.flag("--remote-disable"), " or ", ui.flag("--offline"), "."]
COMMAND_LABELS = {"/VehCharge/state": "Status update", "/VehCharge": "Charge", "/ThermalPrecond": "Climate",
                  "/Doors": "Doors", "/Horn": "Horn", "/Lights": "Lights"}
RESULTS = {SUCCESS: ("Done", "bi-check-circle-fill", "text-success"),
           FAILED: ("Failed", "bi-x-circle-fill", "text-danger"),
           TIMEOUT: ("Timeout, the car didn't answer", "bi-clock-history", "text-warning"),
           NOT_COMPATIBLE: ("Not supported by this car", "bi-slash-circle", "text-danger"),
           None: ("No answer from the car", "bi-question-circle", "text-warning")}


def app() -> PSACarController:
    return PSACarController()


def get_car(vin) -> Car:
    cars = app().myp.vehicles_list
    return cars.get_car_by_vin(vin) or cars[0]


def remote_ready():
    return bool(app().remote_control and getattr(app().myp, "remote_client", None))


def remote_hint(ready):
    """Why the remote commands are locked, None when they aren't."""
    if ready:
        return None
    hint = ["Remote control is off. ", *REMOTE_OFF_HINT] if not app().remote_control else \
        ["Remote control is not set up. ", dcc.Link("Open settings", href=dash_app.get_relative_path("/config"))]
    return html.Small(hint, className="remote-hint d-block text-body-secondary")


def maintenance(vin):
    services = getattr(app().myp, "vehicle_services", None)
    return services.maintenance(vin) if services is not None else None


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


def command_label(name):
    return COMMAND_LABELS.get(name, (name or "Command").strip("/"))


def send_command(action, car: Car):
    """Run a remote command. Returns (message, ok, target state the car should report or None, correlation id)."""
    remote = app().myp.remote_client
    with _sending_lock:
        busy = remote.commands.pending(car.vin)
        if busy is None and car.vin in _sending:
            busy = {"name": None}  # another request is sending right now, its command isn't listed yet
        if not busy:
            _sending.add(car.vin)
    if busy:
        # the car runs one command at a time; the wait ends with its answer or after PENDING_TIMEOUT
        return f"The car is still busy with: {command_label(busy['name'])}, try again shortly", False, None, None
    try:
        with remote.track_commands() as sent:
            message, ok, target = _send_command(remote, action, car)
    finally:
        with _sending_lock:
            _sending.discard(car.vin)
    return message, ok, target, sent[-1] if ok and sent else None


def _send_command(remote, action, car: Car):
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


def command_status():
    """Line above the bottom bar following the last command sent from the UI."""
    return html.Div([dcc.Store(id=COMMAND_STORE, storage_type="session"),
                     dcc.Interval(id=COMMAND_POLL, interval=COMMAND_POLL_MS, disabled=True),
                     html.Div(id=COMMAND_STATUS, className="command-status", hidden=True, role="status",
                              **{"aria-live": "polite"})])


def current_command(commands, correlation_id):
    """The command, or the one it was sent again as after an expired token."""
    command = commands.get_dict(correlation_id)
    for _ in range(3):
        if command is None or command["status"] != RETRIED or not command["retried_as"]:
            break
        command = commands.get_dict(command["retried_as"]) or command
    return command


def _step(label, done, current=False):
    if current:
        marker = dbc.Spinner(size="sm", spinner_class_name="me-1")
    else:
        marker = html.I(className="bi bi-check2 me-1" if done else "bi bi-circle me-1")
    return html.Span([marker, label], className="command-step" + ("" if done or current else " text-body-secondary"))


def render_command(command, label):
    """(children, finished at or None) of the status line: sent -> car woken up -> result."""
    woken = any(update["return_code"] == RC_WAKEUP_FORWARDED for update in command["updates"])
    waiting = command["pending"]
    steps = [_step("Sent", True, current=waiting and not woken)]
    if woken:
        steps.append(_step("Car woken up", True, current=waiting))
    finished_at = None
    if not waiting:
        result = command["status"] if command["final"] else None
        text, icon, color = RESULTS.get(result, RESULTS[FAILED])
        if result in (FAILED, NOT_COMPATIBLE, TIMEOUT) and command["error"]:
            text = f"{text}: {command['error']}"
        steps.append(html.Span([html.I(className=f"bi {icon} me-1"), text], className=f"command-step {color}"))
        last_update = command["updates"][-1]["at"] if command["updates"] else None
        finished_at = last_update if command["final"] and last_update else command["sent_at"] + PENDING_TIMEOUT
    separator = html.I(className="bi bi-chevron-right command-sep")
    line = [steps[0]]
    for step in steps[1:]:
        line.extend([separator, step])
    return [html.Strong(label, className="me-2"), *line], finished_at


@dash_app.callback(Output(COMMAND_STATUS, "children"), Output(COMMAND_STATUS, "hidden"),
                   Output(COMMAND_POLL, "disabled"),
                   Input(COMMAND_POLL, "n_intervals"), Input(COMMAND_STORE, "data"))
def follow_command(_, sent):
    if not sent or not remote_ready():
        return no_update, True, True
    command = current_command(app().myp.remote_client.commands, sent["id"])
    if command is None:  # forgotten after an hour or a restart
        return no_update, True, True
    children, finished_at = render_command(command, sent["label"])
    if finished_at is not None and time.time() - finished_at > COMMAND_SHOWN_S:
        return no_update, True, True
    return children, False, False


@dash_app.callback(*ui.toast_outputs(), Output(PENDING_STORE, "data"), Output(COMMAND_STORE, "data"),
                   Input({"type": ACTION, "action": ALL, "vin": ALL}, "n_clicks"),
                   State(PENDING_STORE, "data"),
                   prevent_initial_call=True)
def run_action(_clicks, pending):
    trigger = callback_context.triggered_id
    # Re-rendered buttons (status poll) fire this callback with n_clicks back at 0
    if not trigger or not callback_context.triggered[0]["value"]:
        raise PreventUpdate
    car = get_car(trigger["vin"])
    message, ok, target, correlation_id = send_command(trigger["action"], car)
    if ok and target is not None:
        pending = add_pending(pending, car.vin, trigger["action"], target)
    else:
        pending = no_update
    if correlation_id:
        # the status line shows what happens to the command, no toast on top of it
        label = message.removesuffix(" sent").removesuffix(" requested")
        return (no_update,) * len(ui.TOAST_OUTPUTS) + (pending, {"id": correlation_id, "label": label})
    return (*ui.toast_message(message, ok), pending, no_update)
