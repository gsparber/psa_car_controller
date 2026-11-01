from datetime import datetime
from threading import Lock, RLock, Thread
from typing import List
from urllib.parse import parse_qs, urlparse

import dash_bootstrap_components as dbc
from dash import dcc, html
from dash.dependencies import Output, Input, State
from dash.exceptions import PreventUpdate
import time

from psa_car_controller.common import utils
from psa_car_controller.common.build_version import version_info
from psa_car_controller.common.mylogger import CustomLogger
from psa_car_controller.psacc.application.car_controller import PSACarController
from psa_car_controller.psacc.model.car import Cars, Car
from psa_car_controller.psacc.model.charge import Charge

from psa_car_controller.psacc.repository.trips import Trips

from psa_car_controller.psacc.application.charging import Charging
from psa_car_controller.web import figures, units

from psa_car_controller.web.app import dash_app
from psa_car_controller.psacc.repository.db import Database
from psa_car_controller.web.tools.utils import diff_dashtable, unix_time_millis, get_marks_from_start_end, create_card
from psa_car_controller.web.view.config_oauth import get_oauth_config_layout, oauth_link_missing
from psa_car_controller.web.view.config_views import config_layout

# pylint: disable=invalid-name
from psa_car_controller.web.tools.figurefilter import FigureFilter, set_clientside_callback
from psa_car_controller.web.view.control import get_control_tabs
from psa_car_controller.web.view.home import get_home_layout
from psa_car_controller.web.view.charge import get_charge_layout
from psa_car_controller.web.view.climate import get_climate_layout
from psa_car_controller.web.view.insights import get_insights_layout, set_trips_provider
from psa_car_controller.web.view.settings import get_settings_layout
from psa_car_controller.web.view.log import get_log_layout
from psa_car_controller.web.view.car_access import command_status, pending_store
from psa_car_controller.web.view import ui

from psa_car_controller.web.view import api  # pylint: disable=unused-import

logger = CustomLogger.getLogger(__name__)

EMPTY_DIV = "empty-div"
# what the callback of /v1/stats fills, in the order FigureFilter gives them to it
STATS_TABLES = ["trips-table", "battery-table"]
STATS_GRAPHS = ["consumption_fig", "consumption_fig_by_speed", "consumption_graph_by_temp"]
STATS_MAPS = ["trips_map"]
STATS_TABS = "tabs"
STATS_MAP_TAB = "map"

trips: Trips = Trips()
chargings: List[dict]
min_date = max_date = min_millis = max_millis = step = marks = cached_layout = None
cached_unit = units.KM  # display unit the figures and cached_layout were built with
APP = PSACarController()
set_trips_provider(lambda: trips)


def get_default_car() -> Car:
    return APP.myp.vehicles_list[0]


NAV_ITEMS = [("/", "bi-car-front-fill", "Car"),
             ("/charge", "bi-ev-station-fill", "Charge"),
             ("/climate", "bi-snow", "Climate"),
             ("/insights", "bi-bar-chart-line-fill", "Insights"),
             ("/config", "bi-gear-fill", "Settings")]
# The full statistics need a big screen: a tab on desktop, reached from Insights on phones
DESKTOP_NAV_ITEMS = NAV_ITEMS[:4] + [("/v1/stats", "bi-table", "Stats")] + NAV_ITEMS[4:]
# Pages without their own tab highlight the closest one
NAV_PARENT = {"/v1/stats": "/insights", "/log": "/config", "/v1/config": "/config", "/control": "/"}


def add_header(el, pathname="/"):
    """Compact top bar plus a bottom tab bar on phones; on wider screens the tabs move into the top bar."""

    def nav_links(class_name, items, active):
        return [dcc.Link([html.I(className=f"bi {icon}"), html.Span(label)],
                         href=dash_app.get_relative_path(path),
                         className=class_name + (" active" if active == path else ""))
                for path, icon, label in items]

    desktop_active = pathname if pathname == "/v1/stats" else NAV_PARENT.get(pathname, pathname)
    phone_active = NAV_PARENT.get(pathname, pathname)
    version, github_url = version_info()
    version_class = "app-version d-none d-md-inline"
    top_bar = html.Header([
        dcc.Link("PSA Car Controller", href=dash_app.get_relative_path("/"), className="app-title"),
        html.Nav(nav_links("top-nav-link", DESKTOP_NAV_ITEMS, desktop_active),
                 className="top-nav d-none d-md-flex"),
        html.A(version, href=github_url, title=version, target="_blank", rel="noopener",
               className=version_class) if github_url
        else html.Span(version, title=version, className=version_class),
    ], className="app-bar")
    bottom_bar = html.Nav(nav_links("bottom-nav-link", NAV_ITEMS, phone_active),
                          className="bottom-nav d-md-none")
    return top_bar, html.Main(el, className="app-main"), bottom_bar


@dash_app.callback(Output('page-content', 'children'),
                   [Input('url', 'pathname'),
                    Input('url', 'search')])
def display_page(pathname, search):
    prefix = dash_app.config.requests_pathname_prefix or "/"
    pathname = pathname[len(prefix) - 1:]
    query_params = parse_qs(urlparse(search).query)
    no_header = query_params.get("header", None) == ["false"]
    # old addresses keep working for bookmarks: /stats, /config_login, /config_otp
    pathname = {"/stats": "/v1/stats", "/config_login": "/config", "/config_otp": "/config"}.get(pathname, pathname)
    if pathname == "/config_connect":
        page = get_oauth_config_layout(query_params["url"][0]) if "url" in query_params else oauth_link_missing()
    elif not APP.is_good and pathname not in ("/v1/config", "/log"):
        pathname = "/config"
        page = get_settings_layout()
    elif pathname == "/config":
        page = get_settings_layout()
    elif pathname == "/v1/config":
        page = config_layout()
    elif pathname == "/log":
        page = get_log_layout()
    elif pathname == "/control":
        page = get_control_tabs(APP)
    elif pathname == "/v1/stats":
        page = serve_layout()
    elif pathname == "/charge":
        page = get_charge_layout()
    elif pathname == "/climate":
        page = get_climate_layout()
    elif pathname == "/insights":
        page = get_insights_layout()
    else:
        pathname = "/"
        page = get_home_layout()
    if no_header:
        return page
    return add_header(page, pathname)


def create_callback():  # noqa: MC0001
    set_clientside_callback(dash_app, STATS_TABLES, STATS_GRAPHS + STATS_MAPS, STATS_MAPS, STATS_TABS, STATS_MAP_TAB)

    @dash_app.callback(Output(EMPTY_DIV, "children"),
                       [Input("battery-table", "data_timestamp")],
                       [State("battery-table", "data"),
                        State("battery-table", "data_previous")])
    def capture_diffs_in_battery_table(timestamp, data, data_previous):
        if timestamp is None:
            raise PreventUpdate
        diff_data = diff_dashtable(data, data_previous, "start_at")
        for changed_line in diff_data:
            if changed_line['column_name'] == 'price':
                conn = Database.get_db()
                charge = Charge(datetime.utcfromtimestamp(changed_line['start_at'] / 1000))
                charge.price = changed_line['current_value']
                if APP.myp.vehicles_list:
                    charge.vin = get_default_car().vin
                if not Database.set_chargings_price(conn, charge):
                    logger.error("Can't find line to update in the database")
                else:
                    logger.debug("update price %s of %s", changed_line['current_value'], changed_line['start_at'])
                conn.close()
        return ""  # don't need to update dashboard

    @dash_app.callback([Output("tab_battery_popup_graph", "children"), Output("tab_battery_popup", "is_open")],
                       [Input("battery-table", "active_cell"),
                        Input("tab_battery_popup-close", "n_clicks")],
                       [State('battery-table', 'data'),
                        State("tab_battery_popup", "is_open")])
    def get_battery_curve(active_cell, close, data, is_open):  # pylint: disable=unused-argument
        if is_open is None:
            is_open = False
        if active_cell is not None and active_cell["column_id"] in ["start_level", "end_level"] and not is_open:
            row = data[active_cell["row"]]
            if APP.myp.vehicles_list:
                return figures.get_battery_curve_fig(row, APP.myp.vehicles_list[0]), True
        return "", False

    @dash_app.callback([Output("tab_trips_popup_graph", "children"), Output("tab_trips_popup", "is_open"), ],
                       [Input("trips-table", "active_cell"),
                        Input("tab_trips_popup-close", "n_clicks")],
                       State("tab_trips_popup", "is_open"))
    def get_altitude_graph(active_cell, close, is_open):  # pylint: disable=unused-argument
        if is_open is None:
            is_open = False
        if active_cell is not None and active_cell["column_id"] in ["altitude_diff"] and not is_open:
            return figures.get_altitude_fig(trips[active_cell["row_id"] - 1]), True
        return "", False

    # Only after a click: on every visit of the page they kept two server threads for nothing, and leaving it within
    # these 3 s was a "Callback failed: the server did not respond" in WebKit.
    @dash_app.callback(Output("loading-output-trips", "children"), Input("export-trips-table", "n_clicks"),
                       prevent_initial_call=True)
    def export_trips_loading_animation(n_clicks):  # pylint: disable=unused-argument
        time.sleep(3)

    @dash_app.callback(Output("loading-output-battery", "children"), Input("export-battery-table", "n_clicks"),
                       prevent_initial_call=True)
    def export_batt_loading_animation(n_clicks):  # pylint: disable=unused-argument
        time.sleep(3)
    # Emulate click on original Export datatables button, since original button is hard to modify
    dash_app.clientside_callback(
        """
        function(n_clicks) {
            if (n_clicks > 0)
                document.querySelector("#trips-table button.export").click()
            return ""
        }
        """,
        Output("trips-table", "data-dummy"),
        [Input("export-trips-table", "n_clicks")]
    )
    dash_app.clientside_callback(
        """
        function(n_clicks) {
            if (n_clicks > 0)
                document.querySelector("#battery-table button.export").click()
            return ""
        }
        """,
        Output("battery-table", "data-dummy"),
        [Input("export-battery-table", "n_clicks")]
    )
    dash_app.clientside_callback("function(_, marks) { return localSliderMarks(marks) }",
                                 Output("date-slider", "marks"),
                                 Input("clientside-data-store", "data"),
                                 State("date-slider", "marks"))


no_concurrent_update_trip = Lock()
# The trips, the figures and the layout built from them belong together: requests and update_trips change them.
# Never wait for DATA_READY or the network while holding it, update_trips needs it to get there.
figures_lock = RLock()
DATA_READY = False
# How long a page waits for the trips. Missing altitudes come from the network 100 points per second, it can be long.
TRIPS_WAIT_STEP = 0.2
TRIPS_WAIT_STEPS = 100


def update_trips():
    global DATA_READY
    # we allow on thread waiting for no_concurrent_update_trip lock
    with utils.nonblocking(no_concurrent_update_trip) as locked:
        if not locked:
            logger.debug("update_trips already in progress")
            return
        try:
            logger.info("update_trips")
            conn = Database.get_db()
            Database.add_altitude_to_db(conn)
            with figures_lock:
                _load_trips()
        finally:
            # without a login there is nothing to load yet: the first page after it has to do that
            DATA_READY = APP.is_good
        logger.debug("trips updated !")
        return


def _load_trips():
    global trips, chargings, cached_layout, cached_unit, min_date, max_date, min_millis, max_millis, step, marks
    min_date = None
    max_date = None
    cached_unit = figures.UNIT = units.current()
    if APP.is_good and APP.myp.vehicles_list:
        # the tables below show them: when the login happens after the start, nothing has set them yet
        figures.CURRENCY = APP.config.General.currency
        figures.EXPORT_FORMAT = APP.config.General.export_format
        car = get_default_car()  # todo handle multiple car
        try:
            trips_by_vin = Trips.get_trips(Cars([car]))
            # not the ones of the last run when the positions are gone
            trips = trips_by_vin.get(car.vin, Trips())
            if len(trips) > 0:
                min_date = trips[0].start_at
                max_date = trips[-1].start_at
                figures.get_figures(trips[0].car)
        except IndexError:
            logger.info("No trips yet")
        chargings = Charging.get_chargings()
        if len(chargings) == 0:
            logger.info("No chargings yet")
            if min_date is None:
                return
        else:
            if min_date:
                min_date = min(min_date, chargings[0]["start_at"])
                max_date = max(max_date, chargings[-1]["start_at"])
            else:
                min_date = chargings[0]["start_at"]
                max_date = chargings[-1]["start_at"]
        # update for slider
        try:
            logger.debug("min_date:%s - max_date:%s", min_date, max_date)
            min_millis = unix_time_millis(min_date)
            max_millis = unix_time_millis(max_date)
            step = (max_millis - min_millis) / 100
            marks = get_marks_from_start_end(min_date, max_date)
            cached_layout = None  # force regenerate layout
            figures.get_figures(car)
        except (ValueError, IndexError):
            logger.error("update_trips (slider):", exc_info=True)
        except AttributeError:
            logger.debug("position table is probably empty :", exc_info=True)


def _follow_unit_setting():
    """The unit can be changed on the settings page: rebuild the figures (column and axis labels) for it."""
    global cached_layout, cached_unit
    unit = units.current()
    if unit == cached_unit:
        return
    cached_layout = None
    cached_unit = figures.UNIT = unit
    try:
        figures.get_figures(get_default_car())
    except (IndexError, TypeError, AttributeError):
        logger.debug("Can't rebuild the figures for %s", unit, exc_info=True)


def _trips_are_ready():
    """The start loads the trips only with a working login. After a web login no one does until the database changes."""
    if not DATA_READY:
        # not in this thread: the altitudes can take longer than a page should wait
        Thread(target=update_trips, name="update_trips", daemon=True).start()
    for _ in range(TRIPS_WAIT_STEPS):
        if DATA_READY:
            return True
        logger.debug("wait for trips data...")
        time.sleep(TRIPS_WAIT_STEP)
    return DATA_READY


def serve_layout():
    if not _trips_are_ready():
        return dbc.Alert("The trips are still loading, reload this page in a moment.", color="info")
    with figures_lock:
        return _cached_or_new_layout()


def _cached_or_new_layout():
    global cached_layout
    _follow_unit_setting()
    if cached_layout is None:
        logger.debug("Create new layout")
        fig_filter = FigureFilter()
        try:
            range_slider = dcc.RangeSlider(
                id='date-slider',
                min=min_millis,
                max=max_millis,
                step=step,
                marks=marks,
                value=[min_millis, max_millis],
                allow_direct_input=False,
            )
            figures.CURRENCY = APP.config.General.currency
            figures.EXPORT_FORMAT = APP.config.General.export_format
            summary_tab = [
                dbc.Container(dbc.Row(id="summary-cards",
                                      children=create_card(figures.get_summary_cards())), fluid=True),
                fig_filter.add_graph(dcc.Graph(id="consumption_fig"), "start_at_local", ["consumption_km"],
                                     figures.consumption_fig),
                fig_filter.add_graph(dcc.Graph(id="consumption_fig_by_speed"), "speed_average",
                                     ["consumption_km"] * 2, figures.consumption_fig_by_speed),
                fig_filter.add_graph(dcc.Graph(id="consumption_graph_by_temp"), "consumption_by_temp",
                                     ["consumption_km"] * 2, figures.consumption_fig_by_temp)]
            maps = fig_filter.add_map(dcc.Graph(id="trips_map", style={"height": '90vh'}), "lat",
                                      ["long", "start_at_str"], figures.trips_map)
            fig_filter.add_table("trips", figures.table_fig)
            fig_filter.add_table("chargings", figures.battery_table)
            # the database stays metric, only what the page gets is converted
            # minimum trip length is in km: the trips keep "distance_km" for that filter (clientside.js)
            fig_filter.src = {"trips": units.convert_rows(trips.get_trips_as_dict(), cached_unit,
                                                          keep_km=("distance",)),
                              "chargings": units.convert_chargings(chargings, cached_unit)}
            if min_millis is None:
                raise IndexError("no trips and no charges yet")
        except (IndexError, TypeError, NameError, AssertionError, NameError, AttributeError):
            logger.warning("Failed to generate figure, there is probably not enough data yet", exc_info_debug=True)
            # nothing else: the callbacks of the tables can't run without them
            cached_layout = figures.ERROR_DIV
            return cached_layout

        data_div = html.Div([
            *fig_filter.get_store({"minimumLength": APP.config.General.minimum_trip_length}),
            html.Div([
                dbc.Row(
                    children=range_slider,
                    style={"paddingLeft": "40px", "paddingRight": "40px", "height": "50px"},
                ),
                dbc.Tabs([
                    dbc.Tab(label="Summary", tab_id="summary", children=summary_tab),
                    dbc.Tab(label="Trips", tab_id="trips", id="tab_trips",
                            children=[dbc.Row(
                                dbc.Col([
                                    dcc.Loading(
                                        id="loading-div-trips",
                                        children=[html.Div([html.Div(id="loading-output-trips")])],
                                        type="circle",
                                        parent_className="export-load-anim"
                                    ),
                                    dbc.Button("Export trips data",
                                               id="export-trips-table",
                                               n_clicks=0,
                                               size="sm",
                                               color="light",
                                               className="m-1 w-200"
                                               )],
                                    className="d-grid gap-2 d-md-flex justify-content-md-end"
                                )
                            ),
                                html.Div(id="tab_trips_fig", children=figures.table_fig),
                                dbc.Modal(
                                [
                                    dbc.ModalHeader("Altitude"),
                                    dbc.ModalBody(html.Div(
                                                  id="tab_trips_popup_graph")),
                                    dbc.ModalFooter(
                                        dbc.Button("Close",
                                                   id="tab_trips_popup-close",
                                                   className="ms-auto")
                                    ),
                                ],
                                id="tab_trips_popup",
                                size="xl",
                            )
                            ]),
                    dbc.Tab(label="Charge", tab_id="charge", id="tab_charge",
                            children=[dbc.Row(
                                dbc.Col([
                                    dcc.Loading(
                                        id="loading-div-battery",
                                        children=[html.Div([html.Div(id="loading-output-battery")])],
                                        type="circle",
                                        parent_className="export-load-anim"
                                    ),
                                    dbc.Button("Export charging data",
                                               id="export-battery-table",
                                               n_clicks=0,
                                               size="sm",
                                               color="light",
                                               className="m-1 w-200"
                                               )],
                                    className="d-grid gap-2 d-md-flex justify-content-md-end"
                                )
                            ),
                                figures.battery_table,
                                dbc.Modal(
                                [
                                    dbc.ModalHeader(
                                        "Charging speed"),
                                    dbc.ModalBody(html.Div(
                                                  id="tab_battery_popup_graph")),
                                    dbc.ModalFooter(
                                        dbc.Button("Close",
                                                   id="tab_battery_popup-close",
                                                   className="ms-auto")
                                    ),
                                ],
                                id="tab_battery_popup",
                                size="xl",
                            )
                            ]),
                    dbc.Tab(label="Map", tab_id=STATS_MAP_TAB, children=[
                        # fetchTile of clientside.js fills it
                        html.Div(id="map-tiles-hint", className="alert alert-warning mb-1", role="status",
                                 hidden=True),
                        maps])
                ],
                    id=STATS_TABS,
                    active_tab="summary",
                    persistence=True),
                html.Div(id=EMPTY_DIV)
            ])])
        cached_layout = data_div
    return cached_layout


create_callback()
try:
    if APP.is_good:
        Charging.set_default_price(APP.myp.vehicles_list)
        figures.CURRENCY = APP.config.General.currency
        figures.EXPORT_FORMAT = APP.config.General.export_format
        update_trips()
    Database.set_db_callback(update_trips)
except (IndexError, TypeError, AttributeError):
    logger.error("Failed to get trips, there is probably not enough data yet:", exc_info=True)

dash_app.layout = dbc.Container(fluid=True, children=[dcc.Location(id='url', refresh=False),
                                                      html.Div(id='page-content'),
                                                      pending_store(),
                                                      command_status(),
                                                      ui.toast()],
                                className="app-root")
