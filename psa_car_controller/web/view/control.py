import logging
from collections import OrderedDict

import dash_bootstrap_components as dbc
from dash import html

from psa_car_controller.psacc.repository.db import Database
from psa_car_controller.web import units
from psa_car_controller.web.app import dash_app
from psa_car_controller.web.tools.Button import Button
from psa_car_controller.web.tools.Switch import Switch
from psa_car_controller.web.tools.utils import card_value_div, create_card
from psa_car_controller.web.view import ui

logger = logging.getLogger(__name__)

REFRESH_SWITCH = "refresh-switch"
ABRP_SWITCH = 'abrp-switch'
CHARGE_SWITCH = "charge-switch"
PRECONDITIONING_SWITCH = "preconditioning-switch"


def _at_click(config, method):
    """A button keeps the callback of its first build and a login replaces the client: look it up at the click."""
    def call(*args):
        return method(config.myp)(*args)
    return call


def get_control_tabs(config):
    tabs = []
    for car in config.myp.vehicles_list:
        if car.label is None:
            label = car.vin
        else:
            label = car.label
        el = []
        buttons_row = []
        if car.status is not None:
            unit = units.current()
            cards = OrderedDict({"Battery SOC": {"text": [card_value_div("battery_value", "", value=ui.percent(
                                                 car.status.get_energy('Electric').level))],
                                                 "src": dash_app.get_asset_url("images/battery-charge.svg")},
                                 "Mileage": {"text": [card_value_div("mileage_value", units.distance_label(unit),
                                                                     value=ui.fmt(units.distance(
                                                                         car.status.timed_odometer.mileage, unit)))],
                                             "src": dash_app.get_asset_url("images/mileage.svg")}
                                 })
            soh = Database.get_last_soh_by_vin(car.vin)
            if soh:
                cards["Battery SOH"] = {"text": [card_value_div("battery_soh_value", "", value=ui.percent(soh))],
                                        "src": dash_app.get_asset_url("images/battery-soh.svg")}
                cards.move_to_end("Mileage")
            el.append(dbc.Container(dbc.Row(children=create_card(cards)), fluid=True))
            if config.remote_control:
                try:
                    preconditionning_state = car.status.preconditionning.air_conditioning.status != "Disabled"
                    charging_state = car.status.get_energy('Electric').charging.status == "InProgress"

                    updated_at = car.status.get_energy('Electric').updated_at
                    refresh_date = ui.local_time(updated_at, updated_at.astimezone().strftime("%X %x"), "locale")
                    buttons_row.extend([Button(REFRESH_SWITCH, car.vin,
                                               html.Div([
                                                   html.Img(src=dash_app.get_asset_url("images/sync.svg"),
                                                            width="50px"),
                                                   refresh_date
                                               ]),
                                               _at_click(config, lambda myp: myp.remote_client.wakeup)).get_html(),
                                        Switch(CHARGE_SWITCH, car.vin, "Charge",
                                               _at_click(config, lambda myp: myp.remote_client.charge_now),
                                               charging_state).get_html(),
                                        Switch(PRECONDITIONING_SWITCH, car.vin, "Preconditioning",
                                               _at_click(config, lambda myp: myp.remote_client.preconditioning),
                                               preconditionning_state).get_html()])
                except (AttributeError, TypeError):
                    logger.exception("get_control_tabs:")
        if not config.offline:
            buttons_row.append(Switch(ABRP_SWITCH, car.vin, "Send data to ABRP",
                                      _at_click(config, lambda myp: myp.abrp.enable_abrp),
                                      car.vin in config.myp.abrp.abrp_enable_vin).get_html())
        tabs.append(
            dbc.Tab(
                label=label,
                id="tab-" + car.vin,
                tab_id="tab-" + car.vin,
                children=[dbc.Row(buttons_row), *el],
            )
        )

    return dbc.Tabs(
        id="control-tabs",
        active_tab="tab-" + config.myp.vehicles_list[0].vin,
        children=tabs,
    )
