"""Small building blocks shared by the mobile pages."""
import math

import dash_bootstrap_components as dbc
import plotly.graph_objects as go
from dash import dcc, html
from dash.dependencies import Output

from psa_car_controller.web import units
from psa_car_controller.web.view import status_text

TOAST = "app-toast"
TOAST_OUTPUTS = ("children", "header", "icon", "is_open")


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


def section(title, children, extra=None, class_name=None):
    return html.Section([html.Div([html.H2(title, className="section-title"), extra],
                                  className="d-flex align-items-center justify-content-between"),
                         *children], className=f"page-section {class_name}" if class_name else "page-section")


def columns(left, right, class_name=None):
    """Two columns on a big screen, one after the other below (10_mobile.css): the reading and tab order stay the same
    on all widths, left comes first."""
    return html.Div([html.Div(left, className="page-col"), html.Div(right, className="page-col")],
                    className=f"page-columns {class_name}" if class_name else "page-columns")


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


def _known(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def fmt(value, digits=0):
    """A number in the language of the browser, clientside.js replaces text; "–" when unknown."""
    if not _known(value):
        return "–"
    return html.Data(f"{value:,.{digits}f}".replace(",", " "), value=str(value), className="local-number",
                     **{"data-digits": digits})


def percent(value, digits=0):
    """A percentage as the language of the browser writes it ("80%", "80 %", "%80"), clientside.js replaces text;
    "–" when unknown."""
    if not _known(value):
        return "–"
    return html.Data(f"{value:.{digits}f} %", value=str(value), className="local-number",
                     **{"data-digits": digits, "data-style": "percent"})


def with_unit(value, unit, digits=0):
    """fmt with a unit, "–" alone when unknown. Children of a component, not text."""
    number = fmt(value, digits)
    return [number, f" {unit}"] if number != "–" else number


def number_label(template, *values):
    """The props of an aria-label with whole numbers, a {} each: it can't hold fmt, so clientside.js writes it again
    from data-label, where {12345} is a number. "–" when unknown."""
    return {"aria-label": template.format(*[status_text.format_int(value) for value in values]),
            "data-label": template.format(*[f"{{{round(value)}}}" if _known(value) else "–" for value in values])}


def distance(km, unit):
    """A distance stored in km as a whole number in the display unit."""
    return with_unit(units.distance(km, unit), units.distance_label(unit))


def next_service(maintenance, unit=units.KM):
    """Distance and days left until the next service, e.g. "12 000 km · 150 days"; None when unknown.
    Both can be negative once the service is overdue. Children of a component, not text."""
    if not maintenance:
        return None
    mileage, days = maintenance.get("mileage"), maintenance.get("days")
    parts = []
    if _known(mileage):
        parts.append((distance(abs(mileage), unit), mileage))
    if _known(days):
        parts.append((with_unit(abs(days), "day" if abs(days) == 1 else "days"), days))
    children = []
    for number, value in parts:
        if children:
            children.append(" · ")
        children.extend(number)
        if value < 0:
            children.append(" overdue")
    return children or None


def power(kw):
    return ["≈", fmt(kw, 1), " kW"]


def flag(name):
    """A command line flag, on one line: browsers break after the hyphens of "--offline"."""
    return html.Code(name, className="text-nowrap")


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
