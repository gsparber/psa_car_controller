"""Home page: the car at a glance plus the remote actions, laid out for phones first."""
import logging

import dash_bootstrap_components as dbc
from dash import dcc, html
from dash.dependencies import Input, Output, State
from dash.exceptions import PreventUpdate

from psa_car_controller.psacc.application.vehicle_picture import get_vehicle_picture
from psa_car_controller.psacc.model.car import Car
from psa_car_controller.psacc.model.car_status import status_age_seconds, status_updated_at
from psa_car_controller.psacc.repository.db import Database
from psa_car_controller.web import units
from psa_car_controller.web.app import dash_app
from psa_car_controller.web.view import status_text, ui
from psa_car_controller.web.view.car_access import PENDING_STORE, REMOTE_OFF_HINT, action_button, app, car_select, \
    command_supported, get_car, is_charging, is_pending, is_preconditioning, maintenance, read, remote_ready

logger = logging.getLogger(__name__)

POLL_MS = 30 * 1000

CAR_SELECT = "home-car"
STATUS_DIV = "home-status"
POLL = "home-poll"
ABRP_SWITCH = "home-abrp"
ABRP_OUT = "home-abrp-out"


def _soc_ring(level, range_km, unit=units.KM):
    value = level if isinstance(level, (int, float)) else 0
    return html.Div(
        html.Div([
            html.Div(status_text.format_int(level, "%"), className="soc-value"),
            html.Div(status_text.format_distance(range_km, unit), className="soc-range"),
        ], className="soc-inner"),
        className="soc-ring",
        style={"--soc": value},
        role="img",
        **{"aria-label": f"Battery {level} percent, range {status_text.format_distance(range_km, unit)}"},
    )


def _tile(action, vin, icon, title, subtitle, active=False, pending=False, disabled=False):
    # pylint: disable=too-many-arguments,too-many-positional-arguments
    if pending:
        subtitle = [dbc.Spinner(size="sm", spinner_class_name="me-1"), "Waiting for car…"]
    return action_button(action, vin,
                         [html.I(className=f"bi {icon} tile-icon"),
                          html.Div(title, className="tile-title"),
                          html.Div(subtitle, className="tile-sub")],
                         className="home-tile" + (" active" if active else ""), color="light",
                         disabled=disabled or pending)


def _picture(car: Car):
    pictures = get_vehicle_picture(app().myp)
    if pictures.get_cached(car.vin):
        return html.Img(src=dash_app.get_relative_path(f"/vehicle_picture/{car.vin}"), alt=car.label or "Car",
                        className="car-picture")
    pictures.fetch_in_background(car.vin)
    return None


def _updated(updated_at, age_seconds):
    """Time and age of the data ("updated 10:12 (5 min ago)"), None without a date."""
    text = status_text.format_updated(updated_at)
    if text is None:
        return None
    age = status_text.format_age(age_seconds)
    return ["updated ", ui.local_time(updated_at, text, "updated"), f" ({age})" if age else ""]


def _hero(car: Car, remote):
    elec = car.status.get_energy("Electric")
    fuel = car.status.get_energy("Fuel")
    unit = units.current()
    age = status_age_seconds(car.status)
    updated = _updated(status_updated_at(car.status), age)
    state = status_text.car_state_text(read(lambda: car.status.ignition.type),
                                       read(car.status.is_moving),
                                       read(lambda: car.status.kinetic.speed), unit)
    subtitle = [state, " · ", *updated] if state and updated else state or updated
    charging = None
    if car.has_battery():
        ring = _soc_ring(elec.level, elec.autonomy, unit)
        charging = status_text.charging_text(read(lambda: elec.charging.plugged), read(lambda: elec.charging.status),
                                             read(lambda: elec.charging.remaining_time))
        power = status_text.charge_power_kw(read(lambda: elec.charging.charging_rate), car.battery_power,
                                            elec.level, elec.autonomy) if is_charging(car) else None
        if power:
            charging = f"{charging} · {status_text.format_power(power)}"
    else:
        ring = _soc_ring(fuel.level, fuel.autonomy, unit)
    picture = _picture(car)
    privacy = status_text.privacy_text(read(lambda: car.status.privacy.state))
    refresh = action_button("refresh", car.vin, html.I(className="bi bi-arrow-clockwise"), color="link",
                            className="ms-auto icon-button", disabled=not remote,
                            title="Ask the car for a fresh status") \
        if command_supported(car.vin, "/VehCharge/state") else None
    return html.Div([
        html.Div([
            html.Div([html.H2(car.label or car.vin, className="h5 mb-0"),
                      html.Small(subtitle, className="text-body-secondary")]),
            refresh,
        ], className="d-flex align-items-start"),
        html.Div([html.I(className="bi bi-exclamation-triangle-fill me-1"), status_text.outdated_text(age)],
                 className="status-outdated small") if car.status_outdated else None,
        html.Div([html.I(className="bi bi-incognito me-1"), privacy],
                 className="text-center text-body-secondary small privacy-line") if privacy else None,
        html.Div([picture, ring], className="hero-visual" + (" with-picture" if picture is not None else "")),
        html.Div([html.I(className="bi bi-lightning-charge-fill me-1" if is_charging(car) else "bi bi-plug me-1"),
                  charging], className="text-center charging-line") if charging else None,
        html.Div(f"Fuel {status_text.format_int(fuel.level, '%')} · "
                 f"{status_text.format_distance(fuel.autonomy, unit)}",
                 className="text-center text-body-secondary") if car.is_hybrid() else None,
    ], className="home-hero")


def _tiles(car: Car, remote, pending):
    charging_now = is_charging(car)
    precond_on = is_preconditioning(car)
    precond_label = status_text.precond_text(read(lambda: car.status.preconditionning.air_conditioning.status))
    failure = read(lambda: car.status.preconditionning.air_conditioning.failure_cause)
    if failure:
        precond_label = status_text.precond_failure_text(failure)
    tiles = []
    if car.has_battery() and command_supported(car.vin, "/VehCharge"):
        tiles.append(_tile("charge", car.vin, "bi-ev-station", "Stop charging" if charging_now else "Charge now",
                           "Charging" if charging_now else "Not charging", active=charging_now,
                           pending=is_pending(pending, car.vin, "charge", charging_now), disabled=not remote))
    if command_supported(car.vin, "/ThermalPrecond"):
        tiles.append(_tile("precond", car.vin, "bi-snow", "Climate", precond_label or "–", active=precond_on,
                           pending=is_pending(pending, car.vin, "precond", precond_on), disabled=not remote))
    actions = html.Div(tiles, className="home-tiles")
    if not app().remote_control:
        return html.Div([actions, html.Small("Remote control is off. " + REMOTE_OFF_HINT,
                                             className="text-body-secondary")])
    if not remote:
        return html.Div([actions, html.Small(["Remote control is not set up. ",
                                              dcc.Link("Open settings", href=dash_app.get_relative_path("/config"))],
                                             className="text-body-secondary")])
    return actions


def _info(car: Car):
    unit = units.current()
    info = [ui.info_row("bi-speedometer2", "Mileage",
                        status_text.format_distance(read(lambda: car.status.timed_odometer.mileage), unit))]
    service = status_text.maintenance_text(maintenance(car.vin), unit)
    if service:
        info.append(ui.info_row("bi-wrench-adjustable", "Next service", service))
    if car.has_battery():
        soh = read(lambda: Database.get_last_soh_by_vin(car.vin))
        if soh:
            info.append(ui.info_row("bi-heart-pulse", "Battery health", status_text.format_int(soh, "%")))
        chc = read(lambda: app().chc.get(car.vin))
        if chc:
            stop = chc.get_stop_hour()
            parts = [f"{chc.percentage_threshold} %" if chc.percentage_threshold < 100 else None,
                     f"{stop[0]:02d}:{stop[1]:02d}" if stop and any(stop) else None]
            limit = " · ".join(filter(None, parts)) or "Off"
            info.append(dcc.Link([html.I(className="bi bi-battery-full me-3"),
                                  html.Span("Automatic stop", className="flex-grow-1"),
                                  html.Span(limit, className="text-body-secondary me-2"),
                                  html.I(className="bi bi-chevron-right text-body-secondary")],
                                 href=dash_app.get_relative_path("/charge"),
                                 className="list-group-item list-group-item-action d-flex align-items-center "
                                           "text-body"))
    pos = status_text.position(read(lambda: car.status.last_position.geometry.coordinates))
    if pos:
        info.append(ui.info_row("bi-geo-alt", "Location", html.I(className="bi bi-box-arrow-up-right"),
                                href=status_text.map_link_url(*pos)))
    if not app().offline:
        info.append(html.Div([
            html.I(className="bi bi-broadcast me-3"),
            html.Span("Send data to ABRP", className="flex-grow-1"),
            dbc.Switch(id=ABRP_SWITCH, value=car.vin in app().myp.abrp.abrp_enable_vin, className="mb-0"),
            # next to the switch: with its output outside of what a poll replaces, Dash calls toggle_abrp on each poll
            html.Div(id=ABRP_OUT, hidden=True),
        ], className="list-group-item d-flex align-items-center"))
    return html.Div(info, className="list-group home-info")


def render_status(car: Car, pending=None):
    if car.status is None:
        return dbc.Alert("No status received from the car yet.", color="warning")
    remote = remote_ready()
    return [_hero(car, remote), _tiles(car, remote, pending), _info(car)]


def get_home_layout():
    cars = app().myp.vehicles_list
    if not cars:
        return dbc.Alert("No car found for this account.", color="warning")
    return html.Div([
        car_select(CAR_SELECT),
        dcc.Interval(id=POLL, interval=POLL_MS),
        html.Div(render_status(cars[0]), id=STATUS_DIV),
    ], className="page")


@dash_app.callback(Output(STATUS_DIV, "children"),
                   Input(POLL, "n_intervals"), Input(CAR_SELECT, "value"), Input(PENDING_STORE, "data"))
def refresh_status(_, vin, pending):
    if not vin:
        raise PreventUpdate
    return render_status(get_car(vin), pending)


@dash_app.callback(Output(ABRP_OUT, "children"), Input(ABRP_SWITCH, "value"), State(CAR_SELECT, "value"),
                   prevent_initial_call=True)
def toggle_abrp(value, vin):
    car = get_car(vin)
    if value is None or value == (car.vin in app().myp.abrp.abrp_enable_vin):
        raise PreventUpdate
    app().myp.abrp.enable_abrp(car.vin, value)
    return ""
