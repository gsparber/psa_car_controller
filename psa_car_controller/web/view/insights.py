"""Insights page: driving and charging totals for a period, sized for phones. The full analysis stays on /v1/stats."""
import logging

from dash import dcc, html
from dash.dependencies import Input, Output
from dash.exceptions import PreventUpdate

from psa_car_controller.psacc.application.charging import Charging
from psa_car_controller.web.app import dash_app
from psa_car_controller.web.view import insights_data, ui
from psa_car_controller.web.view.car_access import app, read

logger = logging.getLogger(__name__)

PERIOD = "insights-period"
CONTENT = "insights-content"


# views owns the trips cache (rebuilt when the database changes) and registers how to read it
_trips_provider = list


def set_trips_provider(provider):
    global _trips_provider  # pylint: disable=global-statement
    _trips_provider = provider


def _trips():
    return insights_data.trip_rows(_trips_provider())


def _consumption_series(trips, period):
    x, energy = insights_data.series(trips, "consumption", period)
    _, distance = insights_data.series(trips, "distance", period)
    return x, [e / d * 100 if d else None for e, d in zip(energy, distance)]


def _trip_row(trip):
    return html.Div([
        html.I(className="bi bi-geo me-3"),
        html.Div(f"{trip['start_at'].astimezone():%a %d %b, %H:%M}", className="flex-grow-1 text-nowrap"),
        html.Div([html.Div(f"{ui.fmt(trip['distance'], 1)} km", className="fw-semibold"),
                  html.Small(f"{ui.fmt(trip['consumption_km'], 1)} kWh/100km", className="text-body-secondary")],
                 className="text-end"),
    ], className="list-group-item d-flex align-items-center")


def _content(period):
    trips = insights_data.in_period(_trips(), period)
    charges = insights_data.in_period(insights_data.charge_rows(Charging.get_chargings()), period)
    trip_totals = insights_data.trip_totals(trips)
    charge_totals = insights_data.charge_totals(charges)
    currency = read(lambda: app().config.General.currency, "€")
    if not trips and not charges:
        return html.P("No trip or charge recorded in this period.", className="text-body-secondary")
    content = [ui.stat_grid([
        ui.stat_tile("Distance", ui.fmt(trip_totals["distance"]), "km", "bi-signpost-split"),
        ui.stat_tile("Trips", ui.fmt(trip_totals["count"]), "", "bi-car-front"),
        ui.stat_tile("Consumption", ui.fmt(trip_totals["consumption"], 1), "kWh/100km", "bi-speedometer"),
        ui.stat_tile("Energy used", ui.fmt(trip_totals["energy"]), "kWh", "bi-lightning"),
        ui.stat_tile("Charged", ui.fmt(charge_totals["energy"]), "kWh", "bi-ev-station"),
        ui.stat_tile("Charging cost", ui.fmt(charge_totals["cost"], 2), currency, "bi-cash-coin"),
    ])]
    if trips:
        x, y = insights_data.series(trips, "distance", period)
        content.append(ui.section("Distance", [ui.bar_chart(x, y, "km")]))
        x, y = _consumption_series(trips, period)
        content.append(ui.section("Consumption", [ui.bar_chart(x, y, "kWh/100km", color="#fd7e14")]))
        recent = sorted(trips, key=lambda t: t["start_at"], reverse=True)[:8]
        content.append(ui.section("Recent trips", [html.Div([
            _trip_row(trip) for trip in recent], className="list-group")]))
    if charges:
        x, y = insights_data.series(charges, "kw", period)
        content.append(ui.section("Charged energy", [ui.bar_chart(x, y, "kWh", color="#198754")]))
    return content


def get_insights_layout():
    return html.Div([
        ui.page_title("Insights"),
        ui.period_chips(PERIOD, {key: value[0] for key, value in insights_data.PERIODS.items()}, "30d"),
        dcc.Loading(html.Div(id=CONTENT), type="dot"),
        html.Div(ui.link_row("bi-table", "Detailed statistics", dash_app.get_relative_path("/v1/stats")),
                 className="list-group mt-4"),
    ], className="page")


@dash_app.callback(Output(CONTENT, "children"), Input(PERIOD, "value"))
def show_content(period):
    if period not in insights_data.PERIODS:
        raise PreventUpdate
    return _content(period)
