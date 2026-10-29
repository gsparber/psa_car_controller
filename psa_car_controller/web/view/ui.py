"""Small building blocks shared by the mobile pages."""
import dash_bootstrap_components as dbc
import plotly.graph_objects as go
from dash import dcc, html
from dash.dependencies import Output

TOAST = "app-toast"
TOAST_OUTPUTS = ("children", "header", "icon", "is_open")


def toast():
    """One toast in the app shell; pages send messages to it with toast_outputs() + toast_message()."""
    return dbc.Toast(id=TOAST, is_open=False, duration=4000, dismissable=True, className="app-toast")


def toast_outputs():
    return [Output(TOAST, prop, allow_duplicate=True) for prop in TOAST_OUTPUTS]


def toast_message(message, ok=True):
    return message, "Sent" if ok else "Error", "success" if ok else "danger", True


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
                          className="period-chips", inputClassName="btn-check",
                          labelClassName="btn btn-sm btn-outline-secondary", labelCheckedClassName="active")


def bar_chart(x, y, unit, color="#0d6efd", height=200):
    """Small bar chart sized for phones: no toolbar, no zoom, values on hover."""
    fig = go.Figure(go.Bar(x=x, y=y, marker_color=color, hovertemplate=f"%{{x}}<br>%{{y:.1f}} {unit}<extra></extra>"))
    fig.update_layout(height=height, margin={"l": 8, "r": 8, "t": 8, "b": 8}, showlegend=False,
                      paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)", bargap=0.25,
                      xaxis={"fixedrange": True, "showgrid": False},
                      yaxis={"fixedrange": True, "ticksuffix": f" {unit}", "gridcolor": "rgba(128,128,128,0.2)"},
                      font={"size": 11})
    return dcc.Graph(figure=fig, config={"displayModeBar": False, "responsive": True}, className="mini-chart")
