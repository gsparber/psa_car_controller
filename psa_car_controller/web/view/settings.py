"""Settings page: shows the connection state; the login and OTP forms only appear when something needs fixing."""
import logging
from os import path

import dash_bootstrap_components as dbc
from dash import dcc, html, no_update
from dash.dependencies import Input, Output, State
from dash.exceptions import PreventUpdate

from psa_car_controller.common import language
from psa_car_controller.common.build_version import version_info
from psa_car_controller.psa.constants import BRAND, realm_info
from psa_car_controller.psa.oauth import rejected_grant_hint
from psa_car_controller.psa.otp.otp import CONFIG_NAME as OTP_CONFIG_NAME
from psa_car_controller.web import units
from psa_car_controller.psa.RemoteClient import TOKEN_ACCOUNT_FAILED, TOKEN_RATE_LIMITED, TOKEN_REMOTE_FAILED
from psa_car_controller.web.app import dash_app
from psa_car_controller.web.view import ui
from psa_car_controller.web.view.car_access import REMOTE_OFF_HINT, app, read
from psa_car_controller.web.view.config_views import login_form, otp_form

logger = logging.getLogger(__name__)

CONTENT = "settings-content"
STATE_STORE = "settings-state"
POLL = "settings-poll"
RECONNECT = "settings-reconnect"
UNITS = "settings-units"
LANGUAGE = "settings-language"
LANGUAGE_SAVED = "settings-language-saved"

# account states
NO_ACCOUNT, LOGIN_FAILED, LOGGED_IN = "none", "failed", "ok"
# remote control states
REMOTE_OFF, REMOTE_SETUP, REMOTE_PAUSED, REMOTE_ERROR, REMOTE_OK = "off", "setup", "paused", "error", "ok"

TOKEN_ERROR_TEXT = {
    # a rejected login never gets here, account_state shows it: this one can be an outage and heal itself
    TOKEN_ACCOUNT_FAILED: "The account token can't be refreshed, so commands don't reach the car. It retries on its "
                          "own. If this doesn't stop, log in again from Advanced setup below.",
    TOKEN_REMOTE_FAILED: "The remote token can't be refreshed. If reconnecting doesn't help, the OTP has probably "
                         "expired: request a new SMS code below.",
}


def _myp():
    return getattr(app(), "myp", None)


def account_state():
    if _myp() is None:
        return NO_ACCOUNT
    # is_good only tells about the start: PSA can reject the login any time later
    return LOGGED_IN if app().is_good and not read(lambda: _myp().manager.needs_login, False) else LOGIN_FAILED


def remote_state():
    if not app().remote_control:
        return REMOTE_OFF
    remote = read(lambda: _myp().remote_client)
    if remote is None or (remote.otp is None and not path.isfile(OTP_CONFIG_NAME)):
        return REMOTE_SETUP
    if remote.token_error == TOKEN_RATE_LIMITED:
        return REMOTE_PAUSED
    if read(lambda: remote.mqtt_client.is_connected(), False) and remote.token_error is None:
        return REMOTE_OK
    return REMOTE_ERROR


def current_state():
    account = account_state()
    if account != LOGGED_IN:
        return [account, None, None]
    remote = remote_state()
    error = read(lambda: _myp().remote_client.token_error) if remote == REMOTE_ERROR else None
    return [account, remote, error]


STATUS_ICON = {"ok": "bi-check-circle-fill", "warn": "bi-pause-circle-fill", "error": "bi-exclamation-circle-fill"}


def _status_row(icon, label, value, ok):
    """ok: True (green), False (red) or "warn" (orange, a temporary problem that heals itself)."""
    level = "warn" if ok == "warn" else "ok" if ok else "error"
    badge = html.Span([html.I(className=f"bi {STATUS_ICON[level]} me-1"), value], className=f"status-{level}")
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
                       "The car can't be read until you log in again."
                       + rejected_grant_hint(read(lambda: _myp().manager.last_refresh_error))], color="danger"),
            html.Div(login_form(_brand(), country=country), className="card-block")]


def _remote(state, error=None):
    if state == REMOTE_OFF:
        return [html.Div(html.Div([html.I(className="bi bi-broadcast me-3"),
                                   html.Span("Remote control", className="flex-grow-1"),
                                   html.Span("Off", className="text-body-secondary")],
                                  className="list-group-item d-flex align-items-center"), className="list-group"),
                html.Small(REMOTE_OFF_HINT, className="text-body-secondary")]
    if state == REMOTE_OK:
        return [html.Div(_status_row("bi-broadcast", "Remote control", "Connected", True), className="list-group")]
    if state == REMOTE_PAUSED:
        # no Reconnect nor OTP form: both would only refresh again and extend the rate limit
        return [html.Div(_status_row("bi-broadcast", "Remote control", "Paused", "warn"), className="list-group mb-3"),
                dbc.Alert("Too many token requests in a short time. Commands are paused until the limit is over, it "
                          "retries on its own every 30 min.", color="warning")]
    if state == REMOTE_SETUP:
        intro = html.P("To start charging or the climate from here, activate remote control with a code sent by "
                       "SMS and your app PIN.", className="text-body-secondary")
        return [intro, html.Div(otp_form(), className="card-block")]
    content = [html.Div(_status_row("bi-broadcast", "Remote control", "Not connected", False),
                        className="list-group mb-3"),
               dbc.Alert(TOKEN_ERROR_TEXT.get(error, "Can't reach the PSA remote server. If reconnecting doesn't help, "
                                                     "the OTP has probably expired: request a new SMS code below."),
                         color="danger"),
               dbc.Button([html.I(className="bi bi-arrow-repeat me-2"), "Reconnect"], id=RECONNECT, n_clicks=0,
                          color="secondary", className="w-100 big-button mb-3")]
    if error != TOKEN_ACCOUNT_FAILED:  # the OTP is fine then, a new one would only use up an SMS
        content.append(html.Div(otp_form(), className="card-block"))
    return content


def render(state):
    account, remote, error = state
    sections = [ui.section("Account", _account(account))]
    if remote is not None:
        sections.append(ui.section("Remote control", _remote(remote, error)))
    return sections


def _units():
    return ui.section("Units", [
        # same look as the period chips, but no browser persistence: config.ini holds the choice
        dbc.RadioItems(id=UNITS, value=units.current(),
                       options=[{"label": label, "value": key} for key, label in units.UNITS.items()],
                       inline=True, className="segmented", inputClassName="btn-check",
                       labelClassName="segment", labelCheckedClassName="active"),
        html.Small("For distances, speeds and consumptions on all pages. Recorded data stays in km.",
                   className="text-body-secondary d-block")])


def set_length_unit(unit):
    """Saves the display unit in config.ini (General / length unit); False when it can't be written."""
    unit = units.normalize(unit)
    config = app().config
    previous = config.General.length_unit
    config.General.length_unit = unit
    try:
        config.write_config()
    except (OSError, ValueError):
        logger.exception("Can't save the length unit")
        config.General.length_unit = previous
        return False
    return True


def _language():
    return ui.section("Language", [
        dbc.Select(id=LANGUAGE, value=language.current(),
                   options=[{"label": label, "value": key} for key, label in language.LANGUAGES.items()]),
        dcc.Store(id=LANGUAGE_SAVED),
        html.Small("For charts, dates and numbers on all pages. The texts of the app stay in English.",
                   className="text-body-secondary d-block")])


def set_language(value):
    """Saves the language in config.ini (General / language); False when it can't be written."""
    config = app().config
    previous = config.General.language
    config.General.language = value
    try:
        config.write_config()
    except (OSError, ValueError):
        logger.exception("Can't save the language")
        config.General.language = previous
        return False
    return True


def _more():
    version, url = version_info()
    return ui.section("More", [html.Div([
        ui.link_row("bi-journal-text", "Log", dash_app.get_relative_path("/log")),
        ui.link_row("bi-sliders", "Advanced setup (classic)", dash_app.get_relative_path("/v1/config")),
        ui.info_row("bi-github", "Version", version, href=url),
    ], className="list-group")])


def get_settings_layout():
    state = current_state()
    return html.Div([
        ui.page_title("Settings"),
        dcc.Store(id=STATE_STORE, data=state),
        dcc.Interval(id=POLL, interval=10 * 1000),
        html.Div(render(state), id=CONTENT),
        _units(),
        _language(),
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
    return ui.toast_message("Still not connected, see the message on this page", ok=False)


@dash_app.callback(*ui.toast_outputs(), Input(UNITS, "value"), prevent_initial_call=True)
def change_units(unit):
    if not unit or units.normalize(unit) == units.current():
        raise PreventUpdate
    if not set_length_unit(unit):
        return ui.toast_message("Can't save the setting, see the log", ok=False)
    return ui.toast_message("Units: " + ("miles" if units.normalize(unit) == units.MI else "kilometres"))


@dash_app.callback(*ui.toast_outputs(), Output(LANGUAGE_SAVED, "data"), Input(LANGUAGE, "value"),
                   prevent_initial_call=True)
def change_language(value):
    if not value or value == language.current():
        raise PreventUpdate
    if value not in language.LANGUAGES:
        logger.warning("Unknown language %r from the settings page", value)
        return (*ui.toast_message("Unknown language", ok=False), no_update)
    if not set_language(value):
        return (*ui.toast_message("Can't save the setting, see the log", ok=False), no_update)
    return (*ui.toast_message("Language: " + language.LANGUAGES[value]), value)


# only once it is saved: a choice that failed would look applied until the next page
dash_app.clientside_callback("function (value) { applyLanguage(value) }", Input(LANGUAGE_SAVED, "data"),
                             prevent_initial_call=True)
