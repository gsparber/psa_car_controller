"""Log page: the events (status requests, MQTT, commands, charge control, user actions) or the log file, newest
first, grouped by day, filtered by category or level, tracebacks folded. Readable on a phone."""
import logging
import os
import re
from datetime import date, datetime

import dash_bootstrap_components as dbc
from dash import dcc, html
from dash.dependencies import Input, Output, State
from dash.exceptions import PreventUpdate

from psa_car_controller.common.events import CATEGORIES, EVENTS, USER, Event, as_text, record
from psa_car_controller.common.mylogger import LOG_FILE, is_debug, set_debug
from psa_car_controller.web.app import dash_app
from psa_car_controller.web.view import ui

logger = logging.getLogger(__name__)

VIEW = "log-view"
EVENTS_VIEW, FILE_VIEW = "events", "file"
VIEWS = {EVENTS_VIEW: "Events", FILE_VIEW: "Log file"}
CATEGORY = "log-category"
CATEGORY_ROW = "log-category-row"
LEVEL_ROW = "log-level-row"
EVENTS_NOTE = "log-events-note"
DEBUG_SWITCH = "log-debug"
ALL = "all"
LEVEL = "log-level"
CONTENT = "log-content"
POLL = "log-poll"
DOWNLOAD = "log-download"
DOWNLOAD_BUTTON = "log-download-button"

# Only the end of the file: the log rotates at 1 MB and a phone doesn't need all of it
TAIL_BYTES = 256 * 1024
MAX_ENTRIES = 200
LEVELS = {"info": "Info", "warning": "Warnings", "error": "Errors", "debug": "All"}
LEVEL_RANK = {"DEBUG": 0, "INFO": 1, "WARNING": 2, "ERROR": 3, "CRITICAL": 4}
MIN_RANK = {"debug": 0, "info": 1, "warning": 2, "error": 3}
# the format of mylogger: "2026-10-29 17:08:23,614 :: INFO :: message"
ENTRY = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ :: ([A-Z]+) :: (.*)$")


def read_tail(file_name=LOG_FILE, size=TAIL_BYTES):
    with open(file_name, "rb") as f:
        f.seek(0, os.SEEK_END)
        end = f.tell()
        f.seek(max(0, end - size))
        data = f.read()
    text = data.decode("utf-8", errors="replace")
    # drop the first, probably cut, line when reading from the middle of the file
    return text.split("\n", 1)[-1] if end > size else text


def parse(text):
    """Log entries oldest first: {"at": datetime, "level": "INFO", "message": str, "details": str}.
    Lines that don't start an entry (tracebacks, multi line messages) belong to the entry before."""
    entries = []
    for line in text.splitlines():
        match = ENTRY.match(line)
        if match:
            at = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
            entries.append({"at": at, "level": match.group(2), "message": match.group(3), "details": []})
        elif entries and line.strip():
            entries[-1]["details"].append(line)
    for entry in entries:
        entry["details"] = "\n".join(entry["details"])
    return entries


def filter_level(entries, level):
    rank = MIN_RANK.get(level, 1)
    return [e for e in entries if LEVEL_RANK.get(e["level"], 1) >= rank]


def _day_title(day: date, today: date):
    if day == today:
        return "Today"
    if (today - day).days == 1:
        return "Yesterday"
    return f"{day:%a %d %b}"


def _entry(entry):
    level = entry["level"]
    message = html.Div(entry["message"], className="log-message")
    if entry["details"]:
        message = html.Details([html.Summary(entry["message"], className="log-message"),
                                html.Pre(entry["details"], className="log-details")])
    return html.Div([
        html.Div([html.Span(f"{entry['at']:%H:%M:%S}", className="log-time"),
                  html.Span(level.title(), className=f"log-level log-{level.lower()}")],
                 className="d-flex align-items-center gap-2 mb-1"),
        message,
    ], className=f"list-group-item log-entry log-entry-{level.lower()}")


def render(level, today=None):
    try:
        entries = parse(read_tail())
    except OSError as e:
        return dbc.Alert(f"Can't read the log file: {e}", color="warning")
    entries = filter_level(entries, level)[-MAX_ENTRIES:][::-1]
    if not entries:
        return html.P("Nothing logged at this level.", className="text-body-secondary")
    today = today or date.today()
    by_day = {}
    for entry in entries:
        by_day.setdefault(entry["at"].date(), []).append(_entry(entry))
    return [ui.section(_day_title(day, today), [html.Div(rows, className="list-group")])
            for day, rows in by_day.items()]


def _event(event: Event):
    level = event.level
    when = f"{event.at.astimezone():%H:%M:%S}"
    if event.count > 1:
        # a parked car answers the same every refresh: one row for all of them
        when = f"{event.first_at.astimezone():%H:%M}–{event.at.astimezone():%H:%M} · {event.count} times the same"
    return html.Div([
        html.Div([html.Span(when, className="log-time"),
                  html.Span(CATEGORIES.get(event.category, event.category), className="badge text-bg-secondary"),
                  html.Span(event.vin, className="log-time") if event.vin else None,
                  html.Span(level.title(), className=f"log-level log-{level.lower()}") if level != "INFO" else None],
                 className="d-flex align-items-center gap-2 mb-1 flex-wrap"),
        html.Div(event.message, className="log-message"),
    ], className=f"list-group-item log-entry log-entry-{level.lower()}")


def render_events(category, today=None):
    entries = EVENTS.entries(None if category == ALL else [category])
    if not entries:
        return html.P("Nothing happened in this category since the start.", className="text-body-secondary")
    today = today or date.today()
    by_day = {}
    for event in entries:
        by_day.setdefault(event.at.astimezone().date(), []).append(_event(event))
    return [ui.section(_day_title(day, today), [html.Div(rows, className="list-group")])
            for day, rows in by_day.items()]


def get_log_layout():
    return html.Div([
        # the times stay the ones of the log file, and so of the download; the days group them the same way
        html.Div([ui.page_title("Log", f"Times in the server's time zone ({datetime.now().astimezone():%Z})"),
                  dbc.Button([html.I(className="bi bi-download me-2"), "Download"], id=DOWNLOAD_BUTTON,
                             color="secondary", outline=True, size="sm", className="ms-auto")],
                 className="d-flex align-items-center"),
        ui.period_chips(VIEW, VIEWS, EVENTS_VIEW),
        html.Div(ui.period_chips(CATEGORY, {ALL: "All", **CATEGORIES}, ALL), id=CATEGORY_ROW, className="mt-2"),
        html.Div(ui.period_chips(LEVEL, LEVELS, "info"), id=LEVEL_ROW, className="mt-2"),
        html.Div([dbc.Switch(id=DEBUG_SWITCH, value=is_debug(), label=_debug_label(), className="mb-0"),
                  html.Small("Everything into the log file until the next restart, also the raw messages of PSA "
                             "and MQTT with the positions of the cars. Secrets and VINs stay masked.",
                             className="text-body-secondary d-block")], className="mt-3"),
        html.Small("Events: the last 500 since the start, kept in memory only.", id=EVENTS_NOTE,
                   className="text-body-secondary d-block mt-2"),
        dcc.Interval(id=POLL, interval=15 * 1000),
        dcc.Download(id=DOWNLOAD),
        html.Div(id=CONTENT, className="mt-2"),
    ], className="page")


@dash_app.callback(Output(CONTENT, "children"), Output(CATEGORY_ROW, "hidden"), Output(LEVEL_ROW, "hidden"),
                   Output(EVENTS_NOTE, "hidden"),
                   Input(VIEW, "value"), Input(CATEGORY, "value"), Input(LEVEL, "value"), Input(POLL, "n_intervals"))
def show_log(view, category, level, _):
    if view == EVENTS_VIEW:
        if category != ALL and category not in CATEGORIES:
            raise PreventUpdate
        return render_events(category), False, True, False
    if level not in LEVELS:
        raise PreventUpdate
    return render(level), True, False, True


def _debug_label():
    return "Debug logging (on until the next restart)" if is_debug() else "Debug logging"


@dash_app.callback(Output(DEBUG_SWITCH, "label"), Input(DEBUG_SWITCH, "value"), prevent_initial_call=True)
def switch_debug(enabled):
    if enabled is None:
        raise PreventUpdate
    if bool(enabled) != is_debug():
        set_debug(bool(enabled))
        record(USER, "Debug logging %s from the log page", "on" if enabled else "off")
    return _debug_label()


@dash_app.callback(Output(DOWNLOAD, "data"), Input(DOWNLOAD_BUTTON, "n_clicks"), State(VIEW, "value"),
                   State(CATEGORY, "value"), prevent_initial_call=True)
def download_log(n_clicks, view, category):
    """What the page shows: the events of the chosen category, or the log file."""
    if not n_clicks:
        raise PreventUpdate
    if view == EVENTS_VIEW:
        entries = EVENTS.entries(None if category in (None, ALL) else [category])
        return dcc.send_string(as_text(entries), filename=f"psacc-events-{datetime.now():%Y-%m-%d-%H%M}.txt")
    return dcc.send_file(LOG_FILE, filename="psacc-activity.log")
