"""Climate page: preconditioning on/off now, plus the four weekly schedules the car keeps."""
import logging

import dash_bootstrap_components as dbc
from dash import ALL, callback_context, dcc, html
from dash.dependencies import Input, Output, State
from dash.exceptions import PreventUpdate

from psa_car_controller.common.utils import validate_preconditioning_programs
from psa_car_controller.web.app import dash_app
from psa_car_controller.psa import precond_programs
from psa_car_controller.web.view import status_text, ui
from psa_car_controller.web.view.car_access import PENDING_STORE, action_button, app, car_select, get_car, \
    is_pending, is_preconditioning, read, remote_hint, remote_ready

logger = logging.getLogger(__name__)

CAR_SELECT = "climate-car"
STATUS_DIV = "climate-status"
POLL = "climate-poll"
PROGRAMS_DIV = "climate-programs"
SAVE = "climate-save"
PROG_ON = "climate-prog-on"
PROG_TIME = "climate-prog-time"
PROG_DAYS = "climate-prog-days"
PROG_DEL = "climate-prog-del"
PROG_CARD = "climate-prog-card"
VISIBLE = "climate-visible"
ADD = "climate-add"
ADD_WRAP = "climate-add-wrap"
EMPTY = "climate-empty"


def _status(car, pending):
    if car.status is None:
        return dbc.Alert("No status received from the car yet.", color="warning")
    precond_on = is_preconditioning(car)
    waiting = is_pending(pending, car.vin, "precond", precond_on)
    state = status_text.precond_text(read(lambda: car.status.preconditionning.air_conditioning.status)) or "Unknown"
    failure = read(lambda: car.status.preconditionning.air_conditioning.failure_cause)
    label = [dbc.Spinner(size="sm", spinner_class_name="me-2"), "Waiting for car…"] if waiting else \
        [html.I(className="bi bi-power me-2"), "Turn climate off" if precond_on else "Turn climate on"]
    remote = remote_ready()
    return html.Div([
        html.Div([html.I(className="bi bi-snow me-2"), "Climate ", html.Strong(state)], className="fs-5"),
        html.Small(f"Last failure: {status_text.precond_failure_text(failure)}",
                   className="text-danger") if failure else None,
        action_button("precond", car.vin, label, color="secondary" if precond_on else "primary",
                      className="w-100 mt-3 big-button", disabled=waiting or not remote),
        remote_hint(remote),
    ], className="card-block")


def current_programs(car):
    """Last schedules the car sent over MQTT, else the ones in the REST status, else all unset."""
    programs = read(lambda: app().myp.remote_client.get_preconditioning_program(car.vin))
    if not isinstance(programs, dict) or precond_programs.is_default(programs):
        programs = precond_programs.from_status(read(lambda: car.status.preconditionning.air_conditioning.programs))
    return programs


def _program_card(index, program, visible):
    is_set = precond_programs.is_set(program)
    time_value = f"{program['hour']:02d}:{program['minute']:02d}" if is_set else ""
    days = [i for i, flag in enumerate(program.get("day", [])) if flag]
    return html.Div([
        html.Div([
            html.Div([
                html.Label("Ready at", htmlFor={"type": PROG_TIME, "index": index}, className="form-label small mb-1"),
                dbc.Input(id={"type": PROG_TIME, "index": index}, type="time", value=time_value),
            ]),
            # a free slot starts switched on: whoever adds a schedule wants it to run
            dbc.Switch(id={"type": PROG_ON, "index": index}, value=bool(program.get("on")) or not is_set,
                       className="mb-0 ms-auto", label="On"),
            dbc.Button(html.I(className="bi bi-trash3"), id={"type": PROG_DEL, "index": index}, n_clicks=0,
                       color="link", className="icon-button text-danger ms-2", title="Remove schedule"),
        ], className="d-flex align-items-end mb-3"),
        dbc.Checklist(id={"type": PROG_DAYS, "index": index}, value=days, inline=True,
                      options=[{"label": day[:2], "value": i} for i, day in enumerate(precond_programs.DAYS)],
                      className="day-chips", inputClassName="btn-check",
                      labelClassName="btn btn-sm btn-outline-primary", labelCheckedClassName="active"),
    ], id={"type": PROG_CARD, "index": index}, className="card-block program-card", hidden=not visible)


def _programs(car):
    programs = current_programs(car)
    # The car always keeps four slots; only the ones in use are shown, "Add schedule" reveals a free one
    visible = [precond_programs.is_set(programs[key]) for key in precond_programs.KEYS]
    note = None
    if precond_programs.is_default(programs):
        note = html.Small("No schedule received from the car yet. Saving replaces the schedules stored in the car.",
                          className="text-body-secondary d-block mb-2")
    remote = remote_ready()
    return [dcc.Store(id=VISIBLE, data=visible),
            note,
            html.P("No schedule. Add one to have the cabin warm or cool when you leave.", id=EMPTY,
                   className="text-body-secondary", hidden=any(visible)),
            *[_program_card(i, programs[key], visible[i]) for i, key in enumerate(precond_programs.KEYS)],
            html.Div(dbc.Button([html.I(className="bi bi-plus-lg me-2"), "Add schedule"], id=ADD, n_clicks=0,
                                color="secondary", outline=True, className="w-100 mb-3 add-button"),
                     id=ADD_WRAP, hidden=all(visible)),
            dbc.Button("Save schedules", id=SAVE, color="primary", className="w-100 big-button", n_clicks=0,
                       disabled=not remote),
            remote_hint(remote)]


def get_climate_layout():
    cars = app().myp.vehicles_list
    if not cars:
        return dbc.Alert("No car found for this account.", color="warning")
    return html.Div([
        ui.page_title("Climate"),
        car_select(CAR_SELECT),
        dcc.Interval(id=POLL, interval=30 * 1000),
        ui.columns([html.Div(id=STATUS_DIV)],
                   [ui.section("Schedules", [html.Div(id=PROGRAMS_DIV, className="program-grid")])],
                   class_name="page-columns-wide-right"),
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


@dash_app.callback(Output(VISIBLE, "data"),
                   Input(ADD, "n_clicks"), Input({"type": PROG_DEL, "index": ALL}, "n_clicks"),
                   State(VISIBLE, "data"), prevent_initial_call=True)
def add_or_remove(_add, _deletes, visible):
    trigger = callback_context.triggered_id
    if not trigger or not callback_context.triggered[0]["value"]:
        raise PreventUpdate
    visible = list(visible)
    if trigger == ADD:
        if all(visible):
            raise PreventUpdate
        visible[visible.index(False)] = True
    else:
        visible[trigger["index"]] = False
    return visible


@dash_app.callback(Output({"type": PROG_CARD, "index": ALL}, "hidden"), Output(ADD_WRAP, "hidden"),
                   Output(EMPTY, "hidden"),
                   Input(VISIBLE, "data"), prevent_initial_call=True)
def show_cards(visible):
    return [not v for v in visible], all(visible), any(visible)


@dash_app.callback(*ui.toast_outputs(), Input(SAVE, "n_clicks"), State(CAR_SELECT, "value"),
                   State(VISIBLE, "data"),
                   State({"type": PROG_ON, "index": ALL}, "value"),
                   State({"type": PROG_TIME, "index": ALL}, "value"),
                   State({"type": PROG_DAYS, "index": ALL}, "value"),
                   prevent_initial_call=True)
def save_programs(n_clicks, vin, visible, on_flags, times, day_lists):
    # pylint: disable=too-many-arguments,too-many-positional-arguments
    if not n_clicks:
        raise PreventUpdate
    # a removed schedule is sent as unset, so the car frees the slot
    times = [time if shown else None for shown, time in zip(visible, times)]
    for shown, on, time, days in zip(visible, on_flags, times, day_lists):
        if shown and on and (not time or not days):
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
