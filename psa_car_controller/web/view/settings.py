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
from psa_car_controller.psacc.application.ha_mqtt import sends_unencrypted
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
HA_STATUS = "settings-ha-status"
HA_WARNING = "settings-ha-warning"
HA_ENABLED = "settings-ha-enabled"
HA_TLS = "settings-ha-tls"
HA_REMOVE_STORED = "settings-ha-remove-stored"
HA_REMOVE_ROW = "settings-ha-remove-row"
HA_STORED_HINT = "settings-ha-stored-hint"
HA_SAVE = "settings-ha-save"
# text fields of the form: field of parse_settings -> id
HA_INPUTS = {"host": "settings-ha-host", "port": "settings-ha-port", "username": "settings-ha-username",
             "password": "settings-ha-password", "discovery_prefix": "settings-ha-prefix",
             "base_topic": "settings-ha-base-topic"}

# account states
NO_ACCOUNT, LOGIN_FAILED, LOGIN_RETRYING, LOGGED_IN = "none", "failed", "retrying", "ok"
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
    if read(lambda: _myp().manager.needs_login, False):
        return LOGIN_FAILED
    if app().is_good:
        return LOGGED_IN
    # not connected since the start, which an outage explains as well as a rejected login
    return LOGIN_RETRYING if _myp().manager.refresh_failure_is_temporary() else LOGIN_FAILED


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
    if state == LOGIN_RETRYING:
        row = _status_row("bi-person-circle", app_name + (f" · {country}" if country else ""), "Retrying", "warn")
        return [html.Div(row, className="list-group mb-3"),
                dbc.Alert([html.Strong("Temporary connection problem. "),
                           "Your login is kept and it retries on its own, the car can't be read until then. "
                           "If this doesn't stop, log in again below."], color="warning"),
                html.Details([html.Summary("Log in again"),
                              html.Div(login_form(_brand(), country=country), className="card-block")])]
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


def _ha_status():
    config = app().config.Home_Assistant_MQTT
    client = getattr(app(), "home_assistant", None)
    icon = "bi-house"
    error = None
    if client is not None and client.is_connected():
        row = _status_row(icon, "Broker", "Connected", True)
    elif client is not None:
        error = client.last_error
        row = _status_row(icon, "Broker", "Not connected", False) if error else \
            _status_row(icon, "Broker", "Connecting", "warn")
    elif not config.is_enable():
        return html.Div(html.Div([html.I(className=f"bi {icon} me-3"), html.Span("Broker", className="flex-grow-1"),
                                  html.Span("Off", className="text-body-secondary")],
                                 className="list-group-item d-flex align-items-center"), className="list-group mb-3")
    elif app().ha_start_failed:
        error = "It can't start, see the log."
        row = _status_row(icon, "Broker", "Not connected", False)
    else:
        row = _status_row(icon, "Broker", "Starts after login", "warn")
    return html.Div([html.Div(row, className="list-group mb-3"),
                     dbc.Alert(error, color="danger", className="small") if error else None])


def _ha_warning(enabled, host, tls):
    if not enabled or not sends_unencrypted(host, tls):
        return None
    return dbc.Alert("This broker is not local and TLS is off: the broker password and your car's data would cross "
                     "the network unencrypted. Turn on TLS.", color="danger", className="small")


def _ha_password_hint(password):
    return "Set. Leave empty to keep it." if password else "Not set."


def _ha_field(field, label, value, extra=None, **kwargs):
    return html.Div([dbc.Label(label, html_for=HA_INPUTS[field]),
                     dbc.Input(id=HA_INPUTS[field], value=value, **kwargs),
                     dbc.FormFeedback(id=HA_INPUTS[field] + "-feedback", type="invalid"), *(extra or [])],
                    className="mb-3")


def _home_assistant():
    config = app().config.Home_Assistant_MQTT
    return ui.section("Home Assistant", [
        html.Div(_ha_status(), id=HA_STATUS),
        html.Div([
            dbc.Switch(id=HA_ENABLED, value=config.enabled, label="Publish to my MQTT broker", className="mb-3"),
            dbc.Row([
                dbc.Col(_ha_field("host", "Host", config.host or "", type="text", autoComplete="off",
                                  placeholder="192.168.1.10"), xs=8),
                dbc.Col(_ha_field("port", "Port", config.port, type="number", min=1, max=65535, step=1,
                                  inputMode="numeric"), xs=4)], className="g-2"),
            dbc.Switch(id=HA_TLS, value=config.tls, label="TLS (the broker usually listens on port 8883 then)",
                       className="mb-3"),
            html.Div(_ha_warning(config.enabled, config.host, config.tls), id=HA_WARNING),
            _ha_field("username", "User name", config.username or "", type="text", autoComplete="off"),
            # never filled in: the password stays on the server. new-password: no browser fills in the account's one
            _ha_field("password", "Password", "", type="password", autoComplete="new-password", extra=[
                html.Small(_ha_password_hint(config.password), id=HA_STORED_HINT,
                           className="text-body-secondary d-block mt-1"),
                html.Div(dbc.Checkbox(id=HA_REMOVE_STORED, value=False, label="Remove the stored password"),
                         id=HA_REMOVE_ROW, className="mt-1" if config.password else "d-none")]),
            _ha_field("discovery_prefix", "Discovery prefix", config.discovery_prefix, type="text", autoComplete="off"),
            _ha_field("base_topic", "Base topic", config.base_topic, type="text", autoComplete="off"),
            dbc.Button("Save", id=HA_SAVE, n_clicks=0, color="primary", className="w-100 big-button"),
        ], className="card-block"),
        html.Small("Sends your cars to Home Assistant through your own MQTT broker. Applied right away, no restart.",
                   className="text-body-secondary d-block")])


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
        _home_assistant(),
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


@dash_app.callback(Output(HA_STATUS, "children"), Input(POLL, "n_intervals"), prevent_initial_call=True)
def refresh_home_assistant(_):
    # on its own: the form next to it must stay as the user is filling it in
    return _ha_status()


# on leaving the field, not on each key. Not debounce on the input: a tap on Save may not leave it first on a phone
@dash_app.callback(Output(HA_WARNING, "children"), Input(HA_ENABLED, "value"), Input(HA_TLS, "value"),
                   Input(HA_INPUTS["host"], "n_blur"), State(HA_INPUTS["host"], "value"), prevent_initial_call=True)
def warn_home_assistant(enabled, tls, _, host):
    return _ha_warning(enabled, host, tls)


def _ha_form_result(errors):
    """invalid and feedback text of each field."""
    result = []
    for field in HA_INPUTS:
        result += [field in errors, errors.get(field, "")]
    return result


@dash_app.callback(*ui.toast_outputs(),
                   *[output for input_id in HA_INPUTS.values() for output in
                     (Output(input_id, "invalid"), Output(input_id + "-feedback", "children"))],
                   Output(HA_INPUTS["password"], "value"), Output(HA_REMOVE_STORED, "value"),
                   Output(HA_REMOVE_ROW, "className"), Output(HA_STORED_HINT, "children"),
                   Output(HA_STATUS, "children", allow_duplicate=True),
                   Output(HA_WARNING, "children", allow_duplicate=True),
                   Input(HA_SAVE, "n_clicks"), State(HA_ENABLED, "value"), State(HA_TLS, "value"),
                   State(HA_REMOVE_STORED, "value"), *[State(input_id, "value") for input_id in HA_INPUTS.values()],
                   prevent_initial_call=True)
def save_home_assistant(n_clicks, enabled, tls, remove_password, *values):
    """Nothing here may show or log the form, it holds the password; the field names of the errors are fine."""
    if not n_clicks:
        raise PreventUpdate
    previous = app().config.Home_Assistant_MQTT
    form = dict(zip(HA_INPUTS, values), enabled=enabled, tls=tls, remove_password=remove_password)
    keep_form = [no_update] * 6
    try:
        errors = app().save_home_assistant(form)
    except (OSError, ValueError):
        logger.exception("Can't save the Home Assistant settings")
        return (*ui.toast_message("Can't save the setting, see the log", ok=False), *_ha_form_result({}), *keep_form)
    if errors:
        logger.info("Home Assistant settings refused, fields: %s", sorted(errors))
        return (*ui.toast_message("Not saved, check the marked fields", ok=False), *_ha_form_result(errors),
                *keep_form)
    config = app().config.Home_Assistant_MQTT
    if config == previous and not app().ha_start_failed:
        message = ui.toast_message("No changes")
    elif app().ha_start_failed:
        message = ui.toast_message("Saved, but it can't start, see the log", ok=False)
    elif not config.is_enable():
        message = ui.toast_message("Saved, MQTT for Home Assistant is off")
    elif getattr(app(), "home_assistant", None) is None:
        message = ui.toast_message("Saved, it starts after the login")
    else:
        message = ui.toast_message("Saved, connecting to the broker")
    return (*message, *_ha_form_result({}), "", False, "mt-1" if config.password else "d-none",
            _ha_password_hint(config.password), _ha_status(), _ha_warning(config.enabled, config.host, config.tls))


# only once it is saved: a choice that failed would look applied until the next page
dash_app.clientside_callback("function (value) { applyLanguage(value) }", Input(LANGUAGE_SAVED, "data"),
                             prevent_initial_call=True)
