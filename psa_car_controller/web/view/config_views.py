import logging
import re
import threading
import time
import traceback
from collections import namedtuple
from urllib import parse

from dash import html, dcc
from dash.exceptions import PreventUpdate

from psa_car_controller.psa.constants import BRAND
from psa_car_controller.psa.otp.otp import ActivationException, FAQ_ACTIVATION_URL, new_otp_session
from psa_car_controller.psa.RemoteClient import SmsRequestException
from psa_car_controller.psa.setup.apk_parser import UnknownCountryError
from psa_car_controller.psa.setup.countries import BRAND_COUNTRIES, countries_of_brand, normalize_country_code
from psa_car_controller.psa.setup.headless_oauth import HeadlessOAuthError, get_oauth_code_headless
from psa_car_controller.psa.setup.worker_oauth import (WorkerLoginError, WorkerLoginRejected,
                                                       get_oauth_code_from_worker, is_insecure_worker_url, worker_host)
from psa_car_controller.psacc.application.car_controller import PSACarController
from psa_car_controller.psa.setup.app_decoder import InitialSetup, MissingCustomerIdError
from psa_car_controller.common.mylogger import LOG_FILE
from psa_car_controller.web.app import dash_app
from psa_car_controller.web.tools import jobs
from psa_car_controller.web.tools.jobs import DONE, JobBusy, JobFailure, describe_error, scrub
from psa_car_controller.web.tools.utils import create_error_alert
from psa_car_controller.web.view.job_view import job_active, job_parts, refused_view, register_job_view, \
    start_outputs, started_view
import dash_bootstrap_components as dbc
from dash.dependencies import Output, Input, State

logger = logging.getLogger(__name__)

app = PSACarController()
INITIAL_SETUP: InitialSetup = None

BRANDS = [{"label": "Peugeot", "value": "com.psa.mym.mypeugeot"},
          {"label": "Opel", "value": "com.psa.mym.myopel"},
          {"label": "Citroën", "value": "com.psa.mym.mycitroen"},
          {"label": "DS", "value": "com.psa.mym.myds"},
          {"label": "Vauxhall", "value": "com.psa.mym.myvauxhall"}]
LOGIN_FIELDS = ("psa-app", "psa-countrycode", "psa-email", "psa-password")
_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")

# also the kind of the manual login: both use INITIAL_SETUP, only one of them can run
LOGIN_JOB = "login"
OTP_JOB = "otp"
LOGIN_PREFIX = "login"
OTP_PREFIX = "otp"
# worst case: APK download and parsing, worker (300 s), then the local browser (180 s), then reading the account
LOGIN_TIMEOUT_S = 12 * 60
OTP_TIMEOUT_S = 5 * 60
STEP_APP_DATA = "Loading the app data of your brand… The first time this can take a few minutes."
STEP_WORKER = "Logging in through the login service… This can take up to 5 minutes."
STEP_STELLANTIS = "Logging in at Stellantis… This can take up to 3 minutes."
STEP_SAVING = "Saving the login and reading your cars…"
STEP_OTP = "Activating remote control… This can take a minute."
STEP_OTP_CONNECT = "Connecting to the remote control server…"
LOGIN_TIMEOUT_MESSAGE = ("The login took too long and was given up on. It may still finish in the background: reload "
                         "this page in a minute to check before trying again.")
OTP_TIMEOUT_MESSAGE = ("The activation took too long and was given up on. Reload this page in a minute to check "
                       "before asking for a new SMS code.")
LOGIN_LOST_MESSAGE = ("The state of this login is gone, the app has probably restarted. Reload this page to see if it "
                      "worked, then try again if needed.")
OTP_LOST_MESSAGE = ("The state of this activation is gone, the app has probably restarted. Reload this page to see if "
                    "it worked, then ask for a new SMS code if needed.")

# what the page needs to explain a login; made in the thread of the job, shown later with the prefix of the request
LoginDetails = namedtuple("LoginDetails", "kind auth_url worker_error debug", defaults=(None, None, None))
LOGIN_OK, LOGIN_REJECTED, LOGIN_HEADLESS, LOGIN_MANUAL = "ok", "rejected", "headless", "manual"


def _login_worker_url():
    return (app.config.Login_Worker.url or "").strip()


def _login_worker_notice():
    """Tells before the submit that the credentials leave this app."""
    worker_url = _login_worker_url()
    if not worker_url:
        return None
    notice = [dbc.Alert(f"Your email and password will be sent to the login service {worker_host(worker_url)} "
                        "(Login Worker in config.ini). Only use a service you host yourself.",
                        color="info", className="small")]
    if is_insecure_worker_url(worker_url):
        notice.append(dbc.Alert("This login service is not local and uses http://: your password would cross the "
                                "network unencrypted. Use https://.", color="danger", className="small"))
    return html.Div(notice)


def _code_from_worker(auth_url, email, password):
    """Returns (code, None), or (None, what went wrong) when the next login method has to take over."""
    worker_url = _login_worker_url()
    if not worker_url:
        return None, None
    try:
        return get_oauth_code_from_worker(worker_url, auth_url, email, password), None
    except WorkerLoginRejected:
        raise
    except WorkerLoginError as e:
        logger.warning("%s; trying the local browser", e)
        return None, str(e)


def _worker_error_note(worker_error):
    return html.P(worker_error) if worker_error else None


def _country_choice(brand, country=None):
    """(options, value, disabled) of the country field: there is nothing to choose before the brand is known."""
    countries = countries_of_brand(brand)
    codes = [code for code, _ in countries]
    code = normalize_country_code(country)
    if code not in codes:
        code = codes[0] if len(codes) == 1 else None
    return [{"label": f"{name} ({code_})", "value": code_} for code_, name in countries], code, not codes


def check_login_form(app_name, email, password, countrycode):
    """Returns the problem of each field, empty when the form can be sent."""
    errors = {}
    if not isinstance(app_name, str) or app_name not in BRAND:
        errors["psa-app"] = "Choose the brand of your car."
    if not countries_of_brand(app_name):
        errors["psa-countrycode"] = "Choose the brand first."
    elif not normalize_country_code(countrycode):
        errors["psa-countrycode"] = "Choose the country of your account."
    elif normalize_country_code(countrycode) not in BRAND_COUNTRIES[app_name]:
        errors["psa-countrycode"] = "This brand is not available in this country."
    if not isinstance(email, str) or not email.strip():
        errors["psa-email"] = "Enter the email of your account."
    elif not _EMAIL_RE.fullmatch(email.strip()):
        errors["psa-email"] = "This is not a valid email address."
    # a password can start or end with a space: it's sent as it was typed
    if not isinstance(password, str) or not password:
        errors["psa-password"] = "Enter the password of your account."
    return errors


def login_form(brand=None, email=None, country=None):
    """PSA account login; the callbacks below handle it on the settings page and on /v1/config."""
    country_options, country_value, country_disabled = _country_choice(brand, country)
    return dbc.Form([
        _login_worker_notice(),
        html.Div(className="mb-3", children=[
            dbc.Label("Car Brand", html_for="psa-app"),
            dbc.Select(id="psa-app", options=BRANDS, value=brand, placeholder="Choose your brand"),
            dbc.FormFeedback(id="psa-app-feedback", type="invalid")]),
        html.Div(className="mb-3", children=[
            dbc.Label("Country", html_for="psa-countrycode"),
            dbc.Select(id="psa-countrycode", options=country_options, value=country_value,
                       disabled=country_disabled, placeholder="Choose your country"),
            dbc.FormFeedback(id="psa-countrycode-feedback", type="invalid"),
            dbc.FormText("The country of your account. The list depends on the brand.", color="secondary")]),
        html.Div(className="mb-3", children=[
            dbc.Label("Email", html_for="psa-email"),
            dbc.Input(type="email", id="psa-email", placeholder="Enter email", value=email, autoComplete="username"),
            dbc.FormFeedback(id="psa-email-feedback", type="invalid"),
            dbc.FormText("PSA account email", color="secondary")]),
        html.Div(className="mb-3", children=[
            dbc.Label("Password", html_for="psa-password"),
            dbc.Input(type="password", id="psa-password", placeholder="Enter password",
                      autoComplete="current-password"),
            dbc.FormFeedback(id="psa-password-feedback", type="invalid"),
            dbc.FormText("PSA account password", color="secondary")]),
        dbc.Button("Submit", color="primary", id="submit-form", className="w-100", disabled=job_active(LOGIN_JOB)),
        *job_parts(LOGIN_PREFIX, LOGIN_JOB),
        html.Div(id="form_result", className="mt-2", **{"aria-live": "polite"}),
    ])


def otp_form():
    """SMS code + PIN to activate remote control (OTP)."""
    return dbc.Form([
        dbc.Label("Click to receive a code by SMS", html_for="ask-sms"),
        dbc.Button("Send SMS", color="primary", outline=True, id="ask-sms", className="d-block"),
        html.Div(id="sms-demand-result", className="mt-2"),
        dbc.Label("Write the code you just received by SMS", html_for="psa-code", className="mt-2"),
        dbc.Input(type="text", id="psa-code", placeholder="Enter code", inputMode="numeric",
                  autoComplete="one-time-code"),
        dbc.Label("Enter your PIN code", html_for="psa-pin", className="mt-2"),
        dbc.Input(type="password", id="psa-pin", placeholder="Enter codepin", inputMode="numeric"),
        dbc.FormText("It's a digit password", color="secondary"),
        html.Div([
            dbc.Button("Submit", color="primary", id="finish-otp", className="w-100 mt-3",
                       disabled=job_active(OTP_JOB)),
            *job_parts(OTP_PREFIX, OTP_JOB),
            html.Div(id="opt-result", className="mt-2", **{"aria-live": "polite"})]),
        dcc.Location(id="otp-redirect-url", refresh=True),
    ])


def setup_config_layout():
    return dbc.Row(dbc.Col(md=12, lg=4, className="my-3", children=[html.H2('Config'), login_form()]))


def config_otp_layout():
    return dbc.Row(dbc.Col(md=12, lg=4, className="my-3", children=[html.H2('Config OTP'), otp_form()]))


def log_layout():
    try:
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            log_text = f.read()
    except Exception as e:
        log_text = f"Can't read log file: {e}"
    return html.H3(className="m-2", children=["Log:", dbc.Container(
        fluid=True,
        style={"height": "80vh",
               "overflow": "auto",
               "display": "flex",
               "flex-direction": "column-reverse",
               "white-space": "pre-line"},
        children=log_text,
        className="m-3 bg-body-tertiary h5"),
        html.Div(id="empty-div")])


def config_layout(activeTabs="log"):
    return dbc.Tabs(active_tab=activeTabs, children=[
        dbc.Tab([log_layout()], label="Log", tab_id="log"),
        dbc.Tab([setup_config_layout()], label="User config", tab_id="login"),
        dbc.Tab([config_otp_layout()], label="OTP config", tab_id="otp")])


@dash_app.callback(
    Output("psa-countrycode", "options"),
    Output("psa-countrycode", "value"),
    Output("psa-countrycode", "disabled"),
    Input("psa-app", "value"),
    State("psa-countrycode", "value"),
    prevent_initial_call=True)
def updateCountries(app_name, countrycode):
    return _country_choice(app_name, countrycode)


@dash_app.callback(
    [Output(field, "invalid") for field in LOGIN_FIELDS]
    + [Output(f"{field}-feedback", "children") for field in LOGIN_FIELDS],
    Input("submit-form", "n_clicks"),
    State("psa-app", "value"),
    State("psa-email", "value"),
    State("psa-password", "value"),
    State("psa-countrycode", "value"),
    prevent_initial_call=True)
def markLoginForm(n_clicks, app_name, email, password, countrycode):  # pylint: disable=unused-argument
    """On its own callback: the marks of a valid form have to disappear before the login, which takes minutes."""
    errors = check_login_form(app_name, email, password, countrycode)
    return [field in errors for field in LOGIN_FIELDS] + [errors.get(field) for field in LOGIN_FIELDS]


@dash_app.callback(
    Output("form_result", "children", allow_duplicate=True),
    *start_outputs(LOGIN_PREFIX, "submit-form"),
    Input("submit-form", "n_clicks"),
    State("psa-app", "value"),
    State("psa-email", "value"),
    State("psa-password", "value"),
    State("psa-countrycode", "value"),
    State(f"{LOGIN_PREFIX}-job", "data"),
    prevent_initial_call=True,
    running=[(Output("submit-form", "disabled"), True, False)])
# pylint: disable-next=too-many-arguments,too-many-positional-arguments
def connectPSA(n_clicks, app_name, email, password, countrycode, stored=None):
    """Only starts the login, which takes minutes: the job and its polling (register_job_view below) do the rest."""
    if not n_clicks:
        raise PreventUpdate()
    if check_login_form(app_name, email, password, countrycode):
        # nothing leaves this app before the marked fields are fixed
        return refused_view(dbc.Alert("Please fix the marked fields.", color="danger"))
    email = email.strip()
    countrycode = normalize_country_code(countrycode)

    def work(report_step):
        return _login(app_name, email, password, countrycode, report_step)

    try:
        # a second click, another tab or a reverse proxy which gave up on the first request: they all follow this login
        started = jobs.JOBS.start(LOGIN_JOB, work, step=STEP_APP_DATA, timeout_s=LOGIN_TIMEOUT_S,
                                  timeout_message=LOGIN_TIMEOUT_MESSAGE, hidden=(email, password))
    except JobBusy:
        return refused_view(dbc.Alert("The previous login hasn't stopped yet, please try again in a minute.",
                                      color="warning"))
    return started_view(LOGIN_JOB, started, stored)


def _manual_link(auth_url):
    return html.A("Go to login",
                  href=f"{dash_app.config.requests_pathname_prefix}config_connect?url={parse.quote(auth_url)}")


def _headless_debug(debug):
    style = {"height": "150px", "font-family": "monospace", "font-size": "12px", "margin-bottom": "10px"}
    children = [
        dbc.Label("Last URL:"),
        dbc.Input(value=debug["url"], readonly=True, style={"margin-bottom": "10px"}),
        dbc.Label("Console Logs:"),
        dbc.Textarea(value="\n".join(debug["logs"]), style=style, readonly=True),
        dbc.Label("HTML Content:"),
        dbc.Textarea(value=debug["html"], style=style, readonly=True),
    ]
    if debug["screenshot"]:
        children[:0] = [
            dbc.Label("Screenshot of the last page:"),
            html.Img(src="data:image/png;base64," + debug["screenshot"],
                     style={"width": "100%", "margin-bottom": "10px", "border": "1px solid #ccc"}),
        ]
    return html.Details([
        html.Summary("Debug information (please include in a GitHub issue)"),
        html.Div(children, className="mt-2"),
    ])


def render_login(state):
    """The result of a finished login for the tab which started it."""
    details = state.result
    if details is None:
        return dbc.Alert(state.message, color="success" if state.status == DONE else "danger")
    if details.kind == LOGIN_OK:
        return dbc.Alert(["Login successful! ",
                          html.A("Continue with remote control setup", href=dash_app.get_relative_path("/config")),
                          _worker_error_note(details.worker_error)], color="success")
    if details.kind == LOGIN_REJECTED:
        return dbc.Alert(
            [
                html.P(state.message, className="fw-bold"),
                html.P("The login service tried your email and password and failed. They are not sent to "
                       "Stellantis a second time, since repeated failures could lock your account."),
                html.P(["Check them and try again, or finish the login by hand: ", _manual_link(details.auth_url)]),
            ],
            color="danger")
    if details.kind == LOGIN_HEADLESS:
        return dbc.Alert(
            [
                _worker_error_note(details.worker_error),
                html.P(state.message, className="fw-bold"),
                html.P(["You can still finish the login by hand: ", _manual_link(details.auth_url)]),
                _headless_debug(details.debug),
            ],
            color="warning")
    return dbc.Alert([_worker_error_note(details.worker_error), state.message + " ", _manual_link(details.auth_url)],
                     color="warning")


def _login(app_name, email, password, countrycode, report_step):
    """Runs in the thread of the job. Returns (message, details) or raises JobFailure; shown by render_login."""
    hidden = (email, password)
    logger.info("Initial setup...")
    try:
        global INITIAL_SETUP
        INITIAL_SETUP = InitialSetup(app_name, email, password, countrycode)
        auth_url = INITIAL_SETUP.psacc.manager.generate_redirect_url()
        scheme = INITIAL_SETUP.psacc.manager.redirect_uri.split("://")[0]
    except (ConnectionError, UnknownCountryError, ValueError) as e:
        # the details are already logged or there are none
        logger.error("Initial setup failed: %s", scrub(str(e), hidden))
        raise JobFailure(str(e)) from e
    except Exception as e:
        logger.error("Initial setup failed: %s", scrub(traceback.format_exc(), hidden))
        raise JobFailure(describe_error(e)) from e

    # Attempt automatic login: the login worker if there is one, then headless OAuth
    worker_error = None
    try:
        if _login_worker_url():
            report_step(STEP_WORKER)
        code, worker_error = _code_from_worker(auth_url, email, password)
        if code is None:
            report_step(STEP_STELLANTIS)
            code = get_oauth_code_headless(auth_url, email, password, scheme)
        report_step(STEP_SAVING)
        INITIAL_SETUP.connect(code)
        return "Login successful!", LoginDetails(LOGIN_OK, worker_error=worker_error)
    except MissingCustomerIdError as e:
        # the login worked, logging in again by hand would fail the same way
        raise JobFailure(str(e)) from e
    except WorkerLoginRejected as e:
        logger.warning("%s; not trying the local browser", scrub(str(e), hidden))
        raise JobFailure(str(e), LoginDetails(LOGIN_REJECTED, auth_url)) from e
    except HeadlessOAuthError as e:
        debug = {"url": scrub(e.url, hidden), "logs": [scrub(line, hidden) for line in e.logs],
                 "html": scrub(e.html, hidden), "screenshot": e.screenshot}
        raise JobFailure(str(e), LoginDetails(LOGIN_HEADLESS, auth_url, worker_error, debug)) from e
    except Exception as e:  # pylint: disable=broad-except
        logger.warning("Headless OAuth failed (%s), falling back to manual flow", scrub(str(e), hidden))

    # Manual fallback
    raise JobFailure("Automatic login failed. Please complete manually:",
                     LoginDetails(LOGIN_MANUAL, auth_url, worker_error))


poll_login = register_job_view(LOGIN_PREFIX, LOGIN_JOB, "submit-form", "form_result", render_login, LOGIN_LOST_MESSAGE)


class Cooldown:
    """One caller per period. For the whole app, not per session: there is one account and the SMS all go to it."""

    def __init__(self, seconds):
        self.seconds = seconds
        self._lock = threading.Lock()
        self._claimed_at = None

    def claim(self):
        """Returns 0 if the caller may go on, else the seconds left."""
        now = time.monotonic()
        with self._lock:
            if self._claimed_at is not None and now - self._claimed_at < self.seconds:
                return int(self.seconds - (now - self._claimed_at)) + 1
            self._claimed_at = now
            return 0

    def release(self):
        with self._lock:
            self._claimed_at = None


# longer than SMS_TIMEOUT_IN_S: it also covers a request which is still running
SMS_COOLDOWN = Cooldown(30)


@dash_app.callback(
    Output("sms-demand-result", "children"),
    Input("ask-sms", "n_clicks"),
    prevent_initial_call=True,
    running=[(Output("ask-sms", "disabled"), True, False)])
def askCode(n_clicks):
    # every SMS counts for the OTP limit of PSA: only a click sends one
    if not n_clicks:
        raise PreventUpdate()
    seconds_left = SMS_COOLDOWN.claim()
    if seconds_left:
        return dbc.Alert("An SMS was requested a moment ago, it can take a little while to arrive. "
                         f"You can ask for another one in {seconds_left} s.", color="info")
    try:
        app.myp.remote_client.get_sms_otp_code()
        return dbc.Alert("SMS sent", color="success")
    except SmsRequestException as e:
        SMS_COOLDOWN.release()  # the page says to try again
        return create_error_alert("The SMS could not be requested", [e.hint], e.details)
    except Exception as e:
        SMS_COOLDOWN.release()
        res = str(e)
        return dbc.Alert(res, color="danger")


def _activate_otp(sms_code, code_pin, report_step):
    """Runs in the thread of the job. Returns (message, None) or raises JobFailure; shown by render_otp."""
    try:
        otp_session = new_otp_session(sms_code, code_pin, app.myp.remote_client.otp)
        app.myp.remote_client.otp = otp_session
        app.myp.save_config()
        report_step(STEP_OTP_CONNECT)
        app.start_remote_control()
        return "OTP config finished!", None
    except ActivationException as e:
        logger.error("finishOtp: %s", scrub(str(e), (sms_code, code_pin)))
        hints = [e.hint]
        # a failed activation leaves the previous one in use
        if app.myp.remote_client.is_mqtt_connected():
            hints.append("Remote control is working right now with the previous activation, so there is no "
                         "need to activate it again.")
        raise JobFailure(e.hint, (hints, scrub(str(e), (sms_code, code_pin)))) from e
    except Exception as e:
        logger.error("finishOtp: %s", scrub(traceback.format_exc(), (sms_code, code_pin)))
        raise JobFailure(describe_error(e)) from e


def render_otp(state):
    """The result of a finished activation for the tab which started it."""
    if state.status == DONE:
        home = dash_app.get_relative_path("/")
        return [dbc.Alert(["OTP config finished! Redirecting to home in 5 seconds… ",
                           html.A("Go to home now", href=home)], color="success"),
                # counts once, then otp_redirect_home takes over
                dcc.Interval(id="otp-redirect-interval", interval=5000, n_intervals=0, max_intervals=1)]
    if state.result is None:
        return dbc.Alert(state.message, color="danger")
    hints, details = state.result
    return create_error_alert("The remote control could not be activated", hints, details,
                              ("More in the FAQ", FAQ_ACTIVATION_URL))


@dash_app.callback(
    Output("opt-result", "children", allow_duplicate=True),
    *start_outputs(OTP_PREFIX, "finish-otp"),
    Input("finish-otp", "n_clicks"),
    State("psa-pin", "value"),
    State("psa-code", "value"),
    State(f"{OTP_PREFIX}-job", "data"),
    prevent_initial_call=True,
    running=[(Output("finish-otp", "disabled"), True, False)])
def finishOtp(n_clicks, code_pin, sms_code, stored=None):
    """Only starts the activation: an SMS code works once, an answer lost on the way must not cost a new one."""
    if not n_clicks:
        raise PreventUpdate()

    def work(report_step):
        return _activate_otp(sms_code, code_pin, report_step)

    try:
        started = jobs.JOBS.start(OTP_JOB, work, step=STEP_OTP, timeout_s=OTP_TIMEOUT_S,
                                  timeout_message=OTP_TIMEOUT_MESSAGE, hidden=(sms_code, code_pin))
    except JobBusy:
        return refused_view(dbc.Alert("The previous activation hasn't stopped yet, please try again in a minute.",
                                      color="warning"))
    return started_view(OTP_JOB, started, stored)


poll_otp = register_job_view(OTP_PREFIX, OTP_JOB, "finish-otp", "opt-result", render_otp, OTP_LOST_MESSAGE)


@dash_app.callback(
    Output("otp-redirect-url", "href"),
    Input("otp-redirect-interval", "n_intervals"))
def otp_redirect_home(n_intervals):
    if n_intervals and n_intervals > 0:
        return dash_app.get_relative_path("/")
    raise PreventUpdate()
