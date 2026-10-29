"""Settings page: shows the connection state; the login and OTP forms only appear when something needs fixing."""
import logging
from os import path

import dash_bootstrap_components as dbc
from dash import dcc, html
from dash.dependencies import Input, Output, State
from dash.exceptions import PreventUpdate

from psa_car_controller import __version__
from psa_car_controller.psa.constants import BRAND, realm_info
from psa_car_controller.psa.otp.otp import CONFIG_NAME as OTP_CONFIG_NAME
from psa_car_controller.web.app import dash_app
from psa_car_controller.web.view import ui
from psa_car_controller.web.view.car_access import app, read
from psa_car_controller.web.view.config_views import login_form, otp_form

logger = logging.getLogger(__name__)

CONTENT = "settings-content"
STATE_STORE = "settings-state"
POLL = "settings-poll"
RECONNECT = "settings-reconnect"

# account states
NO_ACCOUNT, LOGIN_FAILED, LOGGED_IN = "none", "failed", "ok"
# remote control states
REMOTE_OFF, REMOTE_SETUP, REMOTE_ERROR, REMOTE_OK = "off", "setup", "error", "ok"


def _myp():
    return getattr(app(), "myp", None)


def account_state():
    if _myp() is None:
        return NO_ACCOUNT
    return LOGGED_IN if app().is_good else LOGIN_FAILED


def remote_state():
    if not app().remote_control:
        return REMOTE_OFF
    remote = read(lambda: _myp().remote_client)
    if remote is None or (remote.otp is None and not path.isfile(OTP_CONFIG_NAME)):
        return REMOTE_SETUP
    if read(lambda: remote.mqtt_client.is_connected(), False):
        return REMOTE_OK
    return REMOTE_ERROR


def current_state():
    account = account_state()
    return [account, remote_state() if account == LOGGED_IN else None]


def _status_row(icon, label, value, ok):
    badge = html.Span([html.I(className="bi bi-check-circle-fill me-1" if ok else "bi bi-exclamation-circle-fill me-1"),
                       value], className="status-ok" if ok else "status-error")
    return html.Div([html.I(className=f"bi {icon} me-3"), html.Span(label, className="flex-grow-1"), badge],
                    className="list-group-item d-flex align-items-center")


def _brand():
    realm = read(lambda: _myp().realm)
    return next((key for key, brand in BRAND.items() if brand["realm"] == realm), None)


def _account(state):
    if state == NO_ACCOUNT:
        return [html.P("Log in with the account of your brand's app to connect your car.",
                       className="text-body-secondary"),
                html.Div(login_form(), className="card-block")]
    app_name = read(lambda: realm_info[_myp().realm]["app_name"], "PSA account")
    country = read(lambda: _myp().country_code)
    row = _status_row("bi-person-circle", app_name + (f" · {country}" if country else ""),
                      "Connected" if state == LOGGED_IN else "Not connected", state == LOGGED_IN)
    if state == LOGGED_IN:
        return [html.Div(row, className="list-group")]
    return [html.Div(row, className="list-group mb-3"),
            dbc.Alert([html.Strong("Login expired or rejected. "),
                       "The car can't be read until you log in again."], color="danger"),
            html.Div(login_form(_brand(), country=country), className="card-block")]


def _remote(state):
    if state == REMOTE_OFF:
        return [html.Div(html.Div([html.I(className="bi bi-broadcast me-3"),
                                   html.Span("Remote control", className="flex-grow-1"),
                                   html.Span("Off", className="text-body-secondary")],
                                  className="list-group-item d-flex align-items-center"), className="list-group"),
                html.Small("Started with --remote-disable or --offline.", className="text-body-secondary")]
    if state == REMOTE_OK:
        return [html.Div(_status_row("bi-broadcast", "Remote control", "Connected", True), className="list-group")]
    if state == REMOTE_SETUP:
        intro = html.P("To start charging or the climate from here, activate remote control with a code sent by "
                       "SMS and your app PIN.", className="text-body-secondary")
        return [intro, html.Div(otp_form(), className="card-block")]
    return [html.Div(_status_row("bi-broadcast", "Remote control", "Not connected", False),
                     className="list-group mb-3"),
            dbc.Alert(["Can't reach the PSA remote server. If reconnecting doesn't help, the OTP has probably "
                       "expired: request a new SMS code below."], color="danger"),
            dbc.Button([html.I(className="bi bi-arrow-repeat me-2"), "Reconnect"], id=RECONNECT, n_clicks=0,
                       color="secondary", className="w-100 big-button mb-3"),
            html.Div(otp_form(), className="card-block")]


def render(state):
    account, remote = state
    sections = [ui.section("Account", _account(account))]
    if remote is not None:
        sections.append(ui.section("Remote control", _remote(remote)))
    return sections


def _more():
    version = "v" + __version__
    return ui.section("More", [html.Div([
        ui.link_row("bi-journal-text", "Log", dash_app.get_relative_path("/log")),
        ui.link_row("bi-sliders", "Advanced setup (classic)", dash_app.get_relative_path("/v1/config")),
        ui.info_row("bi-github", "Version", version,
                    href="https://github.com/flobz/psa_car_controller/releases/tag/" + version),
    ], className="list-group")])


def get_settings_layout():
    state = current_state()
    return html.Div([
        ui.page_title("Settings"),
        dcc.Store(id=STATE_STORE, data=state),
        dcc.Interval(id=POLL, interval=10 * 1000),
        html.Div(render(state), id=CONTENT),
        _more(),
    ], className="page")


@dash_app.callback(Output(CONTENT, "children"), Output(STATE_STORE, "data"),
                   Input(POLL, "n_intervals"), State(STATE_STORE, "data"), prevent_initial_call=True)
def refresh(_, shown):
    # re-render only when the state changes, so a half filled form isn't wiped by the poll
    state = current_state()
    if state == shown:
        raise PreventUpdate
    return render(state), state


@dash_app.callback(*ui.toast_outputs(), Input(RECONNECT, "n_clicks"), prevent_initial_call=True)
def reconnect(n_clicks):
    if not n_clicks:
        raise PreventUpdate
    app().start_remote_control()
    if remote_state() == REMOTE_OK:
        return ui.toast_message("Remote control connected")
    return ui.toast_message("Still not connected, set up the OTP again", ok=False)
