"""Charge page: start/stop, automatic stop (battery limit or time) and statistics about charged energy."""
import logging

import dash_bootstrap_components as dbc
from dash import dcc, html
from dash.dependencies import Input, Output, State
from dash.exceptions import PreventUpdate

from psa_car_controller.psacc.application.charging import Charging
from psa_car_controller.web.app import dash_app
from psa_car_controller.web.view import insights_data, status_text, ui
from psa_car_controller.web.view.car_access import PENDING_STORE, action_button, app, car_select, get_car, \
    is_charging, is_pending, read, remote_ready

logger = logging.getLogger(__name__)

CAR_SELECT = "charge-car"
STATUS_DIV = "charge-status"
POLL = "charge-poll"
SETTINGS_DIV = "charge-settings"
AUTO_SWITCH = "charge-auto"
LIMIT = "charge-limit"
STOP_TIME = "charge-stop-time"
SAVE = "charge-save"
PERIOD = "charge-period"
STATS_DIV = "charge-stats"
DEFAULT_LIMIT = 80
STATS_PERIODS = {"30d": "30 days", "365d": "12 months", "all": "All"}


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
    return html.Div([
        html.Div([
            html.Div([html.Span(status_text.format_int(elec.level, "%"), className="stat-value"),
                      html.Span(status_text.format_int(elec.autonomy, "km"), className="ms-2 text-body-secondary")]),
            html.Div([text, f" · {status_text.format_power(power)}" if power else None], className="charging-line"),
        ]),
        action_button("charge", car.vin, label, color="danger" if charging_now else "primary",
                      className="w-100 mt-3 big-button", disabled=waiting or not remote_ready()),
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
        dbc.Switch(id=AUTO_SWITCH, value=enabled, label="Stop charging automatically", className="mb-3 fs-6"),
        html.Div([
            html.Label(["Battery limit ", html.Strong(id="charge-limit-value", children=f"{limit} %")],
                       htmlFor=LIMIT),
            dcc.Slider(id=LIMIT, min=50, max=100, step=5, value=limit, marks={v: f"{v}" for v in range(50, 101, 10)},
                       allow_direct_input=False,
                       className="mb-3"),
            html.Label("Stop at (optional)", htmlFor=STOP_TIME),
            dbc.Input(id=STOP_TIME, type="time", value=f"{stop[0]:02d}:{stop[1]:02d}" if has_stop else "",
                      className="mb-3"),
        ], id="charge-auto-fields"),
        dbc.Button("Save", id=SAVE, color="primary", className="w-100", n_clicks=0),
        html.Small("PSACC can only start or stop the charge: when the car reports the limit or the stop time is "
                   "reached, it sends “stop”. The car must be reachable, and the battery may end a few % above the "
                   "limit because the car reports its level every few minutes.",
                   className="text-body-secondary d-block mt-3"),
    ], className="card-block")


def _stats(vin, period):
    charges = insights_data.in_period(insights_data.charge_rows(Charging.get_chargings(), vin), period)
    totals = insights_data.charge_totals(charges)
    currency = read(lambda: app().config.General.currency, "€")
    x, y = insights_data.series(charges, "kw", period)
    recent = sorted(charges, key=lambda c: c["start_at"], reverse=True)[:5]
    rows = [ui.info_row("bi-battery-charging",
                        f"{c['start_at'].astimezone():%d %b %H:%M}",
                        f"{ui.fmt(c['start_level'])} → {ui.fmt(c['end_level'])} % · {ui.fmt(c['kw'], 1)} kWh")
            for c in recent]
    return [
        ui.stat_grid([
            ui.stat_tile("Charged", ui.fmt(totals["energy"]), "kWh", "bi-lightning-charge"),
            ui.stat_tile("Sessions", ui.fmt(totals["count"]), "", "bi-plug"),
            ui.stat_tile("Cost", ui.fmt(totals["cost"], 2), currency, "bi-cash-coin"),
            ui.stat_tile("Price", ui.fmt(totals["price_per_kwh"], 2), f"{currency}/kWh", "bi-tag"),
        ]),
        ui.bar_chart(x, y, "kWh", color="#198754") if charges else html.P("No charge in this period.",
                                                                        className="text-body-secondary"),
        html.Div(rows, className="list-group mt-2") if rows else None,
    ]


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
        ui.section("Charged energy", [html.Div(id=STATS_DIV)],
                   extra=ui.period_chips(PERIOD, STATS_PERIODS, "30d")),
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


@dash_app.callback(Output(STATS_DIV, "children"), Input(CAR_SELECT, "value"), Input(PERIOD, "value"))
def show_stats(vin, period):
    if not vin or period not in insights_data.PERIODS:
        raise PreventUpdate
    return _stats(vin, period)


@dash_app.callback(Output("charge-auto-fields", "hidden"), Output("charge-limit-value", "children"),
                   Input(AUTO_SWITCH, "value"), Input(LIMIT, "value"))
def show_limit(enabled, limit):
    return not enabled, f"{limit} %"


@dash_app.callback(*ui.toast_outputs(), Input(SAVE, "n_clicks"),
                   State(CAR_SELECT, "value"), State(AUTO_SWITCH, "value"), State(LIMIT, "value"),
                   State(STOP_TIME, "value"), prevent_initial_call=True)
def save_settings(n_clicks, vin, enabled, limit, stop_time):
    if not n_clicks:
        raise PreventUpdate
    chc = read(lambda: app().chc.get(vin))
    if chc is None:
        return ui.toast_message("Charge control is not enabled", ok=False)
    stop = None
    if enabled and stop_time:
        try:
            stop = [int(part) for part in stop_time.split(":")[:2]]
        except ValueError:
            return ui.toast_message("Invalid stop time", ok=False)
    # "off" is the format charge control already uses: no limit (100 %) and no stop hour
    chc.percentage_threshold = int(limit) if enabled else 100
    chc.set_stop_hour(stop)
    app().chc.save_config()
    logger.info("charge control of %s: limit %s, stop %s", vin, chc.percentage_threshold, stop)
    if not enabled:
        return ui.toast_message("Automatic stop turned off")
    message = f"Stop at {chc.percentage_threshold} %" + (f" or {stop[0]:02d}:{stop[1]:02d}" if stop else "")
    return (message, "Saved", "success", True)
