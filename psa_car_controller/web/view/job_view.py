"""Shows a background job on a page: a spinner with the current step, then the result.

The polling callback must never get the form as State: the password would travel with every request.
The id of the job is kept in the session storage of the tab, so it survives a reload and never shows up in a URL.
"""
import json
from collections import namedtuple
from time import time

import dash_bootstrap_components as dbc
from dash import dcc, html, no_update
from dash.dependencies import Input, Output, State

from psa_car_controller.web.app import dash_app
from psa_car_controller.web.tools import jobs
from psa_car_controller.web.tools.jobs import DONE

POLL_INTERVAL_MS = 2000
# without an answer for that long the button comes back: the server is gone or the network, polling can't tell
SILENCE_TICKS = 3 * 60 * 1000 // POLL_INTERVAL_MS
NO_ANSWER = "The server doesn't answer. Reload this page once it's back to see the result."

JobIds = namedtuple("JobIds", "store poll status")


def job_ids(prefix):
    return JobIds(f"{prefix}-job", f"{prefix}-job-poll", f"{prefix}-job-status")


def running_view(step):
    return [dbc.Spinner(size="sm", spinner_class_name="flex-shrink-0"), html.Span(step)]


def job_active(kind):
    """True when the button of a form has to start locked: rendering the page must not offer a second start."""
    return jobs.JOBS.active(kind) is not None


def job_parts(prefix, kind):
    """The state of the job as of now: the page shows a running job at once, without waiting for the first poll."""
    ids = job_ids(prefix)
    active = jobs.JOBS.active(kind)
    return [dcc.Store(id=ids.store, storage_type="session"),
            dcc.Interval(id=ids.poll, interval=POLL_INTERVAL_MS, n_intervals=0, disabled=active is None,
                         max_intervals=SILENCE_TICKS),
            # in the page before it gets a text: screen readers only announce changes of a live region they know
            html.Div(running_view(active.step) if active else None, id=ids.status, className="job-status",
                     role="status", **{"aria-live": "polite"})]


def new_job_store(job_id):
    """Also holds the time, or a second start without id would not change the store and not start the polling."""
    return {"id": job_id, "at": time()}


def start_outputs(prefix, button):
    """The outputs of a callback which starts a job, in the order of started_view and refused_view."""
    ids = job_ids(prefix)
    return [Output(ids.store, "data", allow_duplicate=True),
            Output(ids.poll, "disabled", allow_duplicate=True),
            Output(button, "disabled", allow_duplicate=True),
            Output(ids.status, "children", allow_duplicate=True)]


def started_view(kind, started, stored):
    """After JOBS.start: the store gets a new id, or the one of this tab if it follows the running job.
    The polling starts from here and not from its first answer, which can get lost like any other."""
    job_id, created = started
    if not created:
        own_id = stored.get("id") if isinstance(stored, dict) else None
        state = jobs.JOBS.status(own_id)
        job_id = own_id if state is not None and state.active else None
    active = jobs.JOBS.active(kind)
    return "", new_job_store(job_id), False, True, running_view(active.step) if active else None


def refused_view(alert):
    return alert, no_update, no_update, no_update, no_update


def poll_job(kind, stored, n_intervals, render, lost_message):
    """Returns (store, poll disabled, poll max_intervals, button disabled, status, result)."""
    polling = bool(n_intervals)
    job_id = stored.get("id") if isinstance(stored, dict) else None
    state = jobs.JOBS.status(job_id)
    deadline = (n_intervals or 0) + SILENCE_TICKS
    if state is None or state.active:
        active = state or jobs.JOBS.active(kind)
        if active is not None:
            return no_update, False, deadline, True, running_view(active.step), no_update
    cleared = new_job_store(None)
    if state is not None:
        # delivered once: a later reload shows the state of the app, and an OTP success doesn't redirect again
        return cleared, True, deadline, False, None, render(state)
    if job_id is not None:
        # the page was waiting for it. When it's only an old id found in the tab there is nothing to tell.
        result = dbc.Alert(lost_message, color="warning") if polling else no_update
        return cleared, True, deadline, False, None, result
    latest = jobs.JOBS.latest(kind) if polling else None
    if latest is not None:
        # followed without the id: another tab or device started it, so no details of the result
        color = "success" if latest.status == DONE else "warning"
        return no_update, True, deadline, False, None, dbc.Alert(latest.message, color=color)
    return no_update, True, deadline, False, None, no_update


# pylint: disable-next=too-many-arguments,too-many-positional-arguments
def register_job_view(prefix, kind, button, result, render, lost_message):
    """render(state) makes the result of a finished job: here and not in the job, since a link needs the prefix of the
    request (Home Assistant ingress) and the thread of the job has none."""
    ids = job_ids(prefix)

    @dash_app.callback(
        Output(ids.store, "data"),
        Output(ids.poll, "disabled"),
        Output(ids.poll, "max_intervals"),
        Output(button, "disabled"),
        Output(ids.status, "children"),
        Output(result, "children"),
        Input(ids.store, "data"),
        Input(ids.poll, "n_intervals"))
    def poll(stored, n_intervals):
        return poll_job(kind, stored, n_intervals, render, lost_message)

    dash_app.clientside_callback(
        f"""
        function (n, max) {{
            if (!n || max < 0 || n < max) {{
                return [window.dash_clientside.no_update, window.dash_clientside.no_update]
            }}
            return [false, {json.dumps(NO_ANSWER)}]
        }}
        """,
        Output(button, "disabled", allow_duplicate=True),
        Output(ids.status, "children", allow_duplicate=True),
        Input(ids.poll, "n_intervals"),
        State(ids.poll, "max_intervals"),
        prevent_initial_call=True)
    return poll
