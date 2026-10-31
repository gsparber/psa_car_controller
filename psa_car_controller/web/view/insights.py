"""Insights page: driving and charging totals for a period, sized for phones. The full analysis stays on /v1/stats."""
import logging

from dash import MATCH, dcc, html
from dash.dependencies import Input, Output, State
from dash.exceptions import PreventUpdate

from psa_car_controller.psacc.application.charging import Charging
from psa_car_controller.web import units
from psa_car_controller.web.app import dash_app
from psa_car_controller.web.view import insights_data, ui
from psa_car_controller.web.view.car_access import app, read

logger = logging.getLogger(__name__)

PERIOD = "insights-period"
CONTENT = "insights-content"
CHART = "insights-chart"
CHART_DATA = "insights-chart-data"


# views owns the trips cache (rebuilt when the database changes) and registers how to read it
_trips_provider = list


def set_trips_provider(provider):
    global _trips_provider  # pylint: disable=global-statement
    _trips_provider = provider


def _trips(unit=units.KM):
    # distances in the display unit, so totals and per 100 km/mi consumptions follow without more conversion
    return units.convert_rows(insights_data.trip_rows(_trips_provider()), unit)


def _consumption_series(trips, period):
    x, energy = insights_data.series(trips, "consumption", period)
    _, distance = insights_data.series(trips, "distance", period)
    return x, [e / d * 100 if d else None for e, d in zip(energy, distance)]


def _chart(name, x, y, unit, local, **kwargs):
    """Bars by day or month of the server until clientside.js has redone them with the ones of the browser."""
    return [dcc.Store(id={"type": CHART_DATA, "index": name}, data=local),
            ui.bar_chart({"type": CHART, "index": name}, x, y, unit, **kwargs)]


def _trip_row(trip, unit):
    return html.Div([
        html.I(className="bi bi-geo me-3"),
        html.Div(ui.local_datetime(trip["start_at"]), className="flex-grow-1 text-nowrap"),
        html.Div([html.Div(ui.with_unit(trip['distance'], units.distance_label(unit), 1), className="fw-semibold"),
                  html.Small(ui.with_unit(trip['consumption_km'], units.consumption_label(unit), 1),
                             className="text-body-secondary")],
                 className="text-end"),
    ], className="list-group-item d-flex align-items-center")


def _content(period):
    unit = units.current()
    trips = insights_data.in_period(_trips(unit), period)
    charges = insights_data.in_period(insights_data.charge_rows(Charging.get_chargings()), period)
    trip_totals = insights_data.trip_totals(trips)
    charge_totals = insights_data.charge_totals(charges)
    currency = read(lambda: app().config.General.currency, "€")
    if not trips and not charges:
        return html.P("No trip or charge recorded in this period.", className="text-body-secondary")
    content = [ui.stat_grid([
        ui.stat_tile("Distance", ui.fmt(trip_totals["distance"]), units.distance_label(unit), "bi-signpost-split"),
        ui.stat_tile("Trips", ui.fmt(trip_totals["count"]), "", "bi-car-front"),
        ui.stat_tile("Consumption", ui.fmt(trip_totals["consumption"], 1), units.consumption_label(unit),
                     "bi-speedometer"),
        ui.stat_tile("Energy used", ui.fmt(trip_totals["energy"]), "kWh", "bi-lightning"),
        ui.stat_tile("Charged", ui.fmt(charge_totals["energy"]), "kWh", "bi-ev-station"),
        ui.stat_tile("Charging cost", ui.fmt(charge_totals["cost"], 2), currency, "bi-cash-coin"),
    ])]
    if trips:
        x, y = insights_data.series(trips, "distance", period)
        content.append(ui.section("Distance", _chart("distance", x, y, units.distance_label(unit),
                                                     insights_data.local_series(trips, "distance", period))))
        x, y = _consumption_series(trips, period)
        local = insights_data.local_series(trips, "consumption", period, divisor_key="distance")
        content.append(ui.section("Consumption", _chart("consumption", x, y, units.consumption_label(unit), local,
                                                        color="#fd7e14")))
        recent = sorted(trips, key=lambda t: t["start_at"], reverse=True)[:8]
        content.append(ui.section("Recent trips", [html.Div([
            _trip_row(trip, unit) for trip in recent], className="list-group")]))
    if charges:
        x, y = insights_data.series(charges, "kw", period)
        content.append(ui.section("Charged energy", _chart("charged", x, y, "kWh",
                                                           insights_data.local_series(charges, "kw", period),
                                                           color="#198754")))
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


dash_app.clientside_callback("function(local, figure) { return localBars(local, figure) }",
                             Output({"type": CHART, "index": MATCH}, "figure"),
                             Input({"type": CHART_DATA, "index": MATCH}, "data"),
                             State({"type": CHART, "index": MATCH}, "figure"))
