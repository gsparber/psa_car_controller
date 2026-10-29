"""Log page: newest entries first, grouped by day, filtered by level, tracebacks folded. Readable on a phone."""
import logging
import os
import re
from datetime import date, datetime

import dash_bootstrap_components as dbc
from dash import dcc, html
from dash.dependencies import Input, Output
from dash.exceptions import PreventUpdate

from psa_car_controller.common.mylogger import LOG_FILE
from psa_car_controller.web.app import dash_app
from psa_car_controller.web.view import ui

logger = logging.getLogger(__name__)

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


def get_log_layout():
    return html.Div([
        html.Div([ui.page_title("Log"),
                  dbc.Button([html.I(className="bi bi-download me-2"), "Download"], id=DOWNLOAD_BUTTON,
                             color="secondary", outline=True, size="sm", className="ms-auto")],
                 className="d-flex align-items-center"),
        ui.period_chips(LEVEL, LEVELS, "info"),
        dcc.Interval(id=POLL, interval=15 * 1000),
        dcc.Download(id=DOWNLOAD),
        html.Div(id=CONTENT),
    ], className="page")


@dash_app.callback(Output(CONTENT, "children"), Input(LEVEL, "value"), Input(POLL, "n_intervals"))
def show_log(level, _):
    if level not in LEVELS:
        raise PreventUpdate
    return render(level)


@dash_app.callback(Output(DOWNLOAD, "data"), Input(DOWNLOAD_BUTTON, "n_clicks"), prevent_initial_call=True)
def download_log(n_clicks):
    if not n_clicks:
        raise PreventUpdate
    return dcc.send_file(LOG_FILE, filename="psacc-activity.log")
