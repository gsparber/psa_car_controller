import logging
import traceback

from dash import html
from dash.exceptions import PreventUpdate

from psa_car_controller.web.app import dash_app
from psa_car_controller.web.tools import jobs
from psa_car_controller.web.tools.jobs import DONE, JobBusy, JobFailure, describe_error, scrub
from psa_car_controller.web.view.job_view import job_active, job_parts, refused_view, register_job_view, \
    start_outputs, started_view
import dash_bootstrap_components as dbc
from dash.dependencies import Output, Input, State

from psa_car_controller.web.view import config_views

logger = logging.getLogger(__name__)

OAUTH_PREFIX = "oauth"
STEP_CONNECT = "Checking the code and reading your cars… This can take a minute."


def oauth_link_missing():
    """The address without ?url=: the login form links here with the address of the login."""
    return dbc.Alert(["This page needs the link of a login. ",
                      html.A("Start the login from the config page", href=dash_app.get_relative_path("/config")), "."],
                     color="warning", className="my-3")


def get_oauth_config_layout(redirect_url):
    return dbc.Row(dbc.Col(md=12, lg=2, className="my-3", children=[
        dbc.Row(html.H2('Connection to PSA (manual fallback)')),
        dbc.Row(children=[
            html.Div(html.P([
                "Automatic login failed. Complete the OAuth flow manually:", html.Br(),
                html.A("1. Click here", href=redirect_url, target="_blank"), html.Br(),
                "2. Complete the login procedure there too until you see 'LOGIN SUCCESSFUL'", html.Br(),
                "3. Open your browser's DevTools (F12) and then the click on 'Network' tab", html.Br(),
                "4. Hit the final 'OK' button, under 'LOGIN SUCCESSFUL'", html.Br(),
                "5. Find in the network tab: xxxx://oauth2redirect....?code=<copy this part>&scope=openid... ",
                html.Br(),
                html.A("You can find more info here",
                       href="https://github.com/flobz/psa_car_controller/discussions/779"), html.Br()]
            )),
            dbc.Form([
                html.Div(className="mb-3", children=[
                    dbc.Label("Code", html_for="psa-oauth-code"),
                    dbc.Input(type="text", id="psa-oauth-code", placeholder="Enter login code"),
                    dbc.FormText(
                        "PSA code from step above, or the whole xxxx://oauth2redirect... URL",
                        color="secondary",
                    )]),
                dbc.Row(dbc.Button("Submit", color="primary", id="finish-oauth",
                                   disabled=job_active(config_views.LOGIN_JOB))),
                *job_parts(OAUTH_PREFIX, config_views.LOGIN_JOB),
                html.Div(id="oauth-result", className="mt-2", **{"aria-live": "polite"}),
            ])
        ])]))


def _connect_with_code(setup, code):
    """Runs in the thread of the job: the code works once, an answer lost on the way must not cost a new login."""
    try:
        setup.connect(code)
        return "PSA login finish !", None
    except Exception as e:
        logger.error("finish_oauth: %s", scrub(traceback.format_exc(), (code,)))
        raise JobFailure(describe_error(e)) from e


def render_oauth(state):
    if state.status != DONE:
        return dbc.Alert(state.message, color="danger")
    return dbc.Alert(["PSA login finish !", html.A(" Continue with remote control setup",
                                                   href=dash_app.get_relative_path("/config"))], color="success")


@dash_app.callback(
    Output("oauth-result", "children", allow_duplicate=True),
    *start_outputs(OAUTH_PREFIX, "finish-oauth"),
    Input("finish-oauth", "n_clicks"),
    State("psa-oauth-code", "value"),
    State(f"{OAUTH_PREFIX}-job", "data"),
    prevent_initial_call=True,
    running=[(Output("finish-oauth", "disabled"), True, False)])
def finish_oauth(n_clicks, code, stored=None):
    if not n_clicks:
        raise PreventUpdate()
    setup = config_views.INITIAL_SETUP
    if setup is None:
        return refused_view(dbc.Alert("This login is gone, the app has probably restarted. Please start again from "
                                      "the login form.", color="danger"))

    def work(_):
        return _connect_with_code(setup, code)

    try:
        started = jobs.JOBS.start(config_views.LOGIN_JOB, work, step=STEP_CONNECT,
                                  timeout_s=config_views.OTP_TIMEOUT_S,
                                  timeout_message=config_views.LOGIN_TIMEOUT_MESSAGE, hidden=(code,))
    except JobBusy:
        return refused_view(dbc.Alert("The previous login hasn't stopped yet, please try again in a minute.",
                                      color="warning"))
    return started_view(config_views.LOGIN_JOB, started, stored)


poll_oauth = register_job_view(OAUTH_PREFIX, config_views.LOGIN_JOB, "finish-oauth", "oauth-result", render_oauth,
                               config_views.LOGIN_LOST_MESSAGE)
