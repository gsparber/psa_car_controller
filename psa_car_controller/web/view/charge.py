"""Charge page: start/stop, automatic stop (battery limit or time) and the last charges."""
import logging

import dash_bootstrap_components as dbc
from dash import dcc, html
from dash.dependencies import Input, Output, State
from dash.exceptions import PreventUpdate

from psa_car_controller.psacc.application.charging import Charging
from psa_car_controller.psacc.model.car import Cars
from psa_car_controller.web import units
from psa_car_controller.web.app import dash_app
from psa_car_controller.web.view import insights_data, status_text, ui
from psa_car_controller.web.view.car_access import PENDING_STORE, action_button, app, car_select, get_car, \
    command_supported, is_charging, is_pending, not_in_subscription, read, remote_hint, remote_ready

logger = logging.getLogger(__name__)

CAR_SELECT = "charge-car"
STATUS_DIV = "charge-status"
POLL = "charge-poll"
SETTINGS_DIV = "charge-settings"
AUTO_SWITCH = "charge-auto"
LIMIT = "charge-limit"
STOP_TIME = "charge-stop-time"
SAVED = "charge-saved"
AUTO_FIELDS = "charge-auto-fields"
LIMIT_VALUE = "charge-limit-value"
CLEAR_TIME = "charge-clear-time"
RECENT_DIV = "charge-recent"
RECENT_COUNT = 10
DEFAULT_LIMIT = 80


def _status(car, pending):
    if car.status is None or not car.has_battery():
        return dbc.Alert("No battery status from the car yet.", color="warning")
    elec = car.status.get_energy("Electric")
    charging_now = is_charging(car)
    waiting = is_pending(pending, car.vin, "charge", charging_now)
    text = status_text.charging_text(read(lambda: elec.charging.plugged), read(lambda: elec.charging.status),
                                     read(lambda: elec.charging.remaining_time)) or "–"
    power = status_text.charge_power_kw(read(lambda: elec.charging.charging_rate), car.battery_power,
                                        elec.level, elec.autonomy) if charging_now else None
    label = [dbc.Spinner(size="sm", spinner_class_name="me-2"), "Waiting for car…"] if waiting else \
        [html.I(className="bi bi-stop-circle me-2" if charging_now else "bi bi-play-circle me-2"),
         "Stop charging" if charging_now else "Start charging"]
    remote = remote_ready()
    return html.Div([
        html.Div([
            html.Div([html.Span(ui.with_unit(elec.level, "%"), className="stat-value"),
                      html.Span(ui.distance(elec.autonomy, units.current()), className="ms-2 text-body-secondary")]),
            html.Div([text, " · ", *ui.power(power)] if power else [text], className="charging-line"),
        ]),
        *([action_button("charge", car.vin, label, color="danger" if charging_now else "primary",
                         className="w-100 mt-3 big-button", disabled=waiting or not remote), remote_hint(remote)]
          if command_supported(car.vin, "/VehCharge") else [not_in_subscription()]),
    ], className="card-block")


def _settings(car):
    chc = read(lambda: app().chc.get(car.vin))
    if chc is None:
        return dbc.Alert(["Automatic stop needs charge control: start PSACC with ", html.Code("-c"),
                          " (", html.Code("--charge-control"), ")."], color="secondary")
    stop = chc.get_stop_hour()
    has_stop = bool(stop) and any(stop)
    enabled = chc.percentage_threshold < 100 or has_stop
    limit = chc.percentage_threshold if chc.percentage_threshold < 100 else DEFAULT_LIMIT
    return html.Div([
        html.Div([
            dbc.Switch(id=AUTO_SWITCH, value=enabled, label="Stop charging automatically", className="mb-0 fs-6"),
            html.Small(id=SAVED, className="save-status ms-auto"),
        ], className="d-flex align-items-center"),
        dbc.Collapse(html.Div([
            html.Label(["Battery limit ", html.Strong(id=LIMIT_VALUE, children=f"{limit} %")], htmlFor=LIMIT),
            dcc.Slider(id=LIMIT, min=50, max=100, step=5, value=limit, marks={v: f"{v}" for v in range(50, 101, 10)},
                       allow_direct_input=False, className="mb-3"),
            html.Label("Stop at (optional)", htmlFor=STOP_TIME),
            dbc.InputGroup([
                dbc.Input(id=STOP_TIME, type="time", debounce=True,
                          value=f"{stop[0]:02d}:{stop[1]:02d}" if has_stop else ""),
                dbc.Button(html.I(className="bi bi-x-lg"), id=CLEAR_TIME, n_clicks=0, color="secondary",
                           outline=True, title="No stop time"),
            ]),
            html.Small("PSACC can only start or stop the charge: when the car reports the limit or the stop time is "
                       "reached, it sends “stop”. The car must be reachable, and the battery may end a few % above "
                       "the limit because the car reports its level every few minutes.",
                       className="text-body-secondary d-block mt-3"),
        ], className="pt-3"), id=AUTO_FIELDS, is_open=enabled),
    ], className="card-block")


def _charge_row(charge, currency):
    stop = charge["stop_at"]
    if stop:
        minutes = round((stop - charge["start_at"]).total_seconds() / 60)
        length = f"{minutes // 60} h {minutes % 60:02d}" if minutes >= 60 else f"{minutes} min"
    else:
        length = "in progress"
    when = [ui.local_datetime(charge["start_at"]), f" · {length}"]
    levels = [ui.fmt(charge['start_level']), " → ", ui.fmt(charge['end_level']), " %"]
    price = ui.with_unit(charge["price"], currency, 2)
    cost = [" · ", *price] if price != "–" else []
    energy = [*ui.with_unit(charge['kw'], "kWh", 1), *cost]
    return html.Div([
        html.I(className="bi bi-battery-charging me-3"),
        html.Div([html.Div(levels, className="fw-semibold"), html.Small(when, className="text-body-secondary")],
                 className="flex-grow-1"),
        html.Div(energy, className="text-end text-nowrap"),
    ], className="list-group-item d-flex align-items-center")


def _estimated_charge_row(charge):
    """Only the levels are known, and the dates frame the charge: no duration, no energy, no cost."""
    return html.Div([
        html.I(className="bi bi-battery-half me-3"),
        html.Div([html.Div([ui.fmt(charge['start_level']), " → ", ui.fmt(charge['end_level']), " %"],
                           className="fw-semibold"),
                  html.Small(["Between ", ui.local_datetime(charge["start_at"]), " and ",
                              ui.local_datetime(charge["stop_at"])], className="text-body-secondary")],
                 className="flex-grow-1"),
        html.Span("Estimated", className="badge text-bg-secondary ms-2",
                  title="Not recorded: the battery level rose between two trips"),
    ], className="list-group-item d-flex align-items-center")


def _estimated_charges(vin):
    try:
        return insights_data.estimated_charge_rows(Charging.get_estimated_chargings(Cars([get_car(vin)])), vin)
    except (IndexError, TypeError, ValueError):
        logger.debug("Can't estimate the charges of %s", vin, exc_info=True)
        return []


def _recent(vin):
    """Last charges only; totals and trends live on the Insights page."""
    charges = [c for c in insights_data.charge_rows(Charging.get_chargings(), vin) if c["start_at"]]
    charges += _estimated_charges(vin)
    if not charges:
        return html.P("No charge recorded yet.", className="text-body-secondary")
    currency = read(lambda: app().config.General.currency, "€")
    recent = sorted(charges, key=lambda c: c["start_at"], reverse=True)[:RECENT_COUNT]
    return html.Div([
        html.Div([_estimated_charge_row(c) if c.get("estimated") else _charge_row(c, currency) for c in recent],
                 className="list-group"),
        html.Div(ui.link_row("bi-bar-chart-line", "Charging statistics", dash_app.get_relative_path("/insights")),
                 className="list-group mt-3"),
    ])


def get_charge_layout():
    cars = app().myp.vehicles_list
    if not cars:
        return dbc.Alert("No car found for this account.", color="warning")
    return html.Div([
        ui.page_title("Charge"),
        car_select(CAR_SELECT),
        dcc.Interval(id=POLL, interval=30 * 1000),
        html.Div(id=STATUS_DIV),
        ui.section("Automatic stop", [html.Div(id=SETTINGS_DIV)]),
        ui.section("Recent charges", [html.Div(id=RECENT_DIV)]),
    ], className="page")


@dash_app.callback(Output(STATUS_DIV, "children"),
                   Input(POLL, "n_intervals"), Input(CAR_SELECT, "value"), Input(PENDING_STORE, "data"))
def refresh_status(_, vin, pending):
    if not vin:
        raise PreventUpdate
    return _status(get_car(vin), pending)


@dash_app.callback(Output(SETTINGS_DIV, "children"), Input(CAR_SELECT, "value"))
def show_settings(vin):
    if not vin:
        raise PreventUpdate
    return _settings(get_car(vin))


@dash_app.callback(Output(RECENT_DIV, "children"), Input(CAR_SELECT, "value"))
def show_recent(vin):
    if not vin:
        raise PreventUpdate
    return _recent(vin)


@dash_app.callback(Output(AUTO_FIELDS, "is_open"), Output(LIMIT_VALUE, "children"),
                   Input(AUTO_SWITCH, "value"), Input(LIMIT, "value"))
def show_limit(enabled, limit):
    return bool(enabled), f"{limit} %"


@dash_app.callback(Output(STOP_TIME, "value"), Input(CLEAR_TIME, "n_clicks"), prevent_initial_call=True)
def clear_stop_time(n_clicks):
    if not n_clicks:
        raise PreventUpdate
    return ""


def _parse_time(value):
    if not value:
        return None
    hour, minute = (int(part) for part in value.split(":")[:2])
    return [hour, minute]


@dash_app.callback(Output(SAVED, "children"), Output(SAVED, "className"),
                   Input(AUTO_SWITCH, "value"), Input(LIMIT, "value"), Input(STOP_TIME, "value"),
                   State(CAR_SELECT, "value"), prevent_initial_call=True)
def save_settings(enabled, limit, stop_time, vin):
    """Every change is saved right away; "off" is no limit (100 %) and no stop hour, as charge control stores it."""
    chc = read(lambda: app().chc.get(vin))
    if chc is None or limit is None:
        raise PreventUpdate
    try:
        stop = _parse_time(stop_time) if enabled else None
    except ValueError:
        return "Invalid time", "save-status ms-auto text-danger"
    threshold = int(limit) if enabled else 100
    current_stop = chc.get_stop_hour()
    if threshold == chc.percentage_threshold and stop == (current_stop if current_stop and any(current_stop) else None):
        raise PreventUpdate
    chc.percentage_threshold = threshold
    chc.set_stop_hour(stop)
    app().chc.save_config()
    logger.info("charge control of %s: limit %s, stop %s", vin, threshold, stop)
    return [html.I(className="bi bi-check2 me-1"), "Saved"], "save-status ms-auto saved"
