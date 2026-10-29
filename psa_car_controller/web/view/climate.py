"""Climate page: preconditioning on/off now, plus the four weekly schedules the car keeps."""
import logging

import dash_bootstrap_components as dbc
from dash import ALL, dcc, html
from dash.dependencies import Input, Output, State
from dash.exceptions import PreventUpdate

from psa_car_controller.common.utils import validate_preconditioning_programs
from psa_car_controller.web.app import dash_app
from psa_car_controller.web.view import precond_programs, status_text, ui
from psa_car_controller.web.view.car_access import PENDING_STORE, action_button, app, car_select, get_car, \
    is_pending, is_preconditioning, read, remote_ready

logger = logging.getLogger(__name__)

CAR_SELECT = "climate-car"
STATUS_DIV = "climate-status"
POLL = "climate-poll"
PROGRAMS_DIV = "climate-programs"
SAVE = "climate-save"
PROG_ON = "climate-prog-on"
PROG_TIME = "climate-prog-time"
PROG_DAYS = "climate-prog-days"


def _status(car, pending):
    if car.status is None:
        return dbc.Alert("No status received from the car yet.", color="warning")
    precond_on = is_preconditioning(car)
    waiting = is_pending(pending, car.vin, "precond", precond_on)
    state = status_text.precond_text(read(lambda: car.status.preconditionning.air_conditioning.status)) or "Unknown"
    failure = read(lambda: car.status.preconditionning.air_conditioning.failure_cause)
    label = [dbc.Spinner(size="sm", spinner_class_name="me-2"), "Waiting for car…"] if waiting else \
        [html.I(className="bi bi-power me-2"), "Turn climate off" if precond_on else "Turn climate on"]
    return html.Div([
        html.Div([html.I(className="bi bi-snow me-2"), "Climate ", html.Strong(state)], className="fs-5"),
        html.Small(f"Last failure: {status_text.precond_failure_text(failure)}",
                   className="text-danger") if failure else None,
        action_button("precond", car.vin, label, color="secondary" if precond_on else "primary",
                      className="w-100 mt-3 big-button", disabled=waiting or not remote_ready()),
    ], className="card-block")


def current_programs(car):
    """Last schedules the car sent over MQTT, else the ones in the REST status, else all unset."""
    programs = read(lambda: app().myp.remote_client.get_preconditioning_program(car.vin))
    if not isinstance(programs, dict) or precond_programs.is_default(programs):
        programs = precond_programs.from_status(read(lambda: car.status.preconditionning.air_conditioning.programs))
    return programs


def _program_card(index, program):
    is_set = precond_programs.is_set(program)
    time_value = f"{program['hour']:02d}:{program['minute']:02d}" if is_set else ""
    days = [i for i, flag in enumerate(program.get("day", [])) if flag]
    return html.Div([
        html.Div([
            html.Strong(f"Schedule {index + 1}"),
            dbc.Switch(id={"type": PROG_ON, "index": index}, value=bool(program.get("on")) and is_set,
                       className="mb-0 ms-auto", label="On"),
        ], className="d-flex align-items-center mb-2"),
        html.Label("Ready at", className="form-label small mb-1"),
        dbc.Input(id={"type": PROG_TIME, "index": index}, type="time", value=time_value, className="mb-2"),
        dbc.Checklist(id={"type": PROG_DAYS, "index": index}, value=days, inline=True,
                      options=[{"label": day[:2], "value": i} for i, day in enumerate(precond_programs.DAYS)],
                      className="day-chips", inputClassName="btn-check",
                      labelClassName="btn btn-sm btn-outline-primary", labelCheckedClassName="active"),
    ], className="card-block program-card")


def _programs(car):
    programs = current_programs(car)
    note = None
    if precond_programs.is_default(programs):
        note = html.Small("No schedule received from the car yet. Saving replaces the schedules stored in the car.",
                          className="text-body-secondary d-block mb-2")
    return [note,
            *[_program_card(i, programs[key]) for i, key in enumerate(precond_programs.KEYS)],
            dbc.Button("Save schedules", id=SAVE, color="primary", className="w-100", n_clicks=0,
                       disabled=not remote_ready())]


def get_climate_layout():
    cars = app().myp.vehicles_list
    if not cars:
        return dbc.Alert("No car found for this account.", color="warning")
    return html.Div([
        ui.page_title("Climate"),
        car_select(CAR_SELECT),
        dcc.Interval(id=POLL, interval=30 * 1000),
        html.Div(id=STATUS_DIV),
        ui.section("Schedules", [html.Div(id=PROGRAMS_DIV)]),
    ], className="page")


@dash_app.callback(Output(STATUS_DIV, "children"),
                   Input(POLL, "n_intervals"), Input(CAR_SELECT, "value"), Input(PENDING_STORE, "data"))
def refresh_status(_, vin, pending):
    if not vin:
        raise PreventUpdate
    return _status(get_car(vin), pending)


@dash_app.callback(Output(PROGRAMS_DIV, "children"), Input(CAR_SELECT, "value"))
def show_programs(vin):
    if not vin:
        raise PreventUpdate
    return _programs(get_car(vin))


@dash_app.callback(*ui.toast_outputs(), Input(SAVE, "n_clicks"), State(CAR_SELECT, "value"),
                   State({"type": PROG_ON, "index": ALL}, "value"),
                   State({"type": PROG_TIME, "index": ALL}, "value"),
                   State({"type": PROG_DAYS, "index": ALL}, "value"),
                   prevent_initial_call=True)
def save_programs(n_clicks, vin, on_flags, times, day_lists):
    if not n_clicks:
        raise PreventUpdate
    for on, time, days in zip(on_flags, times, day_lists):
        if on and (not time or not days):
            return ui.toast_message("An active schedule needs a time and at least one day", ok=False)
    programs = validate_preconditioning_programs(precond_programs.build(on_flags, times, day_lists))
    try:
        sent = app().myp.remote_client.set_preconditioning_program(vin, programs)
    except Exception as e:  # pylint: disable=broad-except
        logger.exception("set_preconditioning_program failed")
        return ui.toast_message(f"Failed: {e}", ok=False)
    if not sent:
        return ui.toast_message("Not sent: the car can't be reached right now", ok=False)
    active = [precond_programs.program_text(programs[key]) for key in precond_programs.KEYS
              if programs[key]["on"]]
    return ui.toast_message("Schedules sent" + (f": {', '.join(active)}" if active else ", all off"))
