"""Small building blocks shared by the mobile pages."""
import dash_bootstrap_components as dbc
import plotly.graph_objects as go
from dash import dcc, html
from dash.dependencies import Output

from psa_car_controller import __version__

TOAST = "app-toast"
TOAST_OUTPUTS = ("children", "header", "icon", "is_open")
RELEASES_URL = "https://github.com/flobz/psa_car_controller/releases/tag/"
# pyproject.toml holds 0.0.0, only release.yml sets the real one; unknown means not installed as a package
DEV_VERSIONS = ("0.0.0", "unknown")


def version_info():
    """Text and release page of the running version; a dev build has no tag to link to."""
    if __version__ in DEV_VERSIONS:
        return "dev build", None
    version = "v" + __version__
    return version, RELEASES_URL + version


def toast():
    """One toast in the app shell; pages send messages to it with toast_outputs() + toast_message()."""
    return dbc.Toast(id=TOAST, is_open=False, duration=4000, dismissable=True, className="app-toast")


def toast_outputs():
    return [Output(TOAST, prop, allow_duplicate=True) for prop in TOAST_OUTPUTS]


def toast_message(message, ok=True):
    return message, "Sent" if ok else "Error", "success" if ok else "danger", True


def local_time(date, text, time_format="datetime"):
    """A date shown in the time zone of the browser: clientside.js replaces text, written in the one of the server."""
    return html.Time(text, dateTime=date.isoformat(), className="local-time", **{"data-format": time_format})


def local_datetime(date):
    return local_time(date, f"{date.astimezone():%a %d %b, %H:%M}")


def page_title(title, subtitle=None):
    return html.Div([html.H1(title, className="h4 mb-0"),
                     html.Small(subtitle, className="text-body-secondary") if subtitle else None],
                    className="page-title")


def section(title, children, extra=None):
    return html.Section([html.Div([html.H2(title, className="section-title"), extra],
                                  className="d-flex align-items-center justify-content-between"),
                         *children], className="page-section")


def stat_tile(label, value, unit="", icon=None):
    return html.Div([
        html.Div([html.I(className=f"bi {icon} me-1") if icon else None, label], className="stat-label"),
        html.Div([html.Span(value, className="stat-value"), html.Span(unit, className="stat-unit")]),
    ], className="stat-tile")


def stat_grid(tiles):
    return html.Div(tiles, className="stat-grid")


def info_row(icon, label, value, href=None):
    content = [html.I(className=f"bi {icon} me-3"),
               html.Span(label, className="flex-grow-1"),
               html.Span(value, className="text-body-secondary")]
    if href:
        return html.A(content, href=href, target="_blank", rel="noopener",
                      className="list-group-item list-group-item-action d-flex align-items-center text-body")
    return html.Div(content, className="list-group-item d-flex align-items-center")


def fmt(value, digits=0):
    """Number for a tile: "–" when unknown, thin space grouping."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "–"
    return f"{value:,.{digits}f}".replace(",", " ")


def period_chips(chip_id, periods, value):
    """Segmented control to pick a time range; periods is {value: label}."""
    return dbc.RadioItems(id=chip_id, value=value, inline=True, persistence=True, persistence_type="local",
                          options=[{"label": label, "value": key} for key, label in periods.items()],
                          className="segmented", inputClassName="btn-check",
                          labelClassName="segment", labelCheckedClassName="active")


def link_row(icon, label, href):
    """List row that opens another page, with the chevron phones use for "goes somewhere"."""
    return dcc.Link([html.I(className=f"bi {icon} me-3"),
                     html.Span(label, className="flex-grow-1"),
                     html.I(className="bi bi-chevron-right text-body-secondary")],
                    href=href, className="list-group-item list-group-item-action d-flex align-items-center text-body")


def bar_chart(chart_id, x, y, unit, color="#0d6efd", height=200):
    # pylint: disable=too-many-arguments,too-many-positional-arguments
    """Small bar chart sized for phones: no toolbar, no zoom, values on hover.
    Text colors come from the CSS (.mini-chart) so the chart follows the light/dark theme."""
    fig = go.Figure(go.Bar(x=x, y=y, marker_color=color, hovertemplate=f"%{{x}}<br>%{{y:.1f}} {unit}<extra></extra>"))
    fig.update_layout(height=height, margin={"l": 8, "r": 8, "t": 8, "b": 8}, showlegend=False,
                      paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)", bargap=0.25,
                      xaxis={"fixedrange": True, "showgrid": False, "linecolor": "rgba(128,128,128,0.3)"},
                      yaxis={"fixedrange": True, "ticksuffix": f" {unit}", "gridcolor": "rgba(128,128,128,0.2)"},
                      font={"size": 11})
    return dcc.Graph(id=chart_id, figure=fig, config={"displayModeBar": False, "responsive": True},
                     className="mini-chart")
