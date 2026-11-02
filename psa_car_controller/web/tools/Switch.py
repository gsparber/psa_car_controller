import dash_bootstrap_components as dbc
from dash import html
from dash.dependencies import Input
from dash.exceptions import PreventUpdate

from psa_car_controller.common.utils import RateLimitException
from psa_car_controller.web.app import dash_app
from psa_car_controller.web.tools.Button import Button, RATE_LIMIT_MESSAGE


class Switch(Button):
    # What the page shows per switch, not the state of the car, which is out of date right after a command.
    # By id: the page builds its switches per request, the callback stays the one of the first.
    _shown = {}

    def __init__(self, role, element_id, label, fct, value, prevent_initial_call=True):
        # pylint: disable=too-many-arguments,too-many-positional-arguments
        self.value = value
        super().__init__(role, element_id, label, fct, prevent_initial_call)
        Switch._shown[self.get_button_id()] = value

    def _set_callback(self, prevent_initial_call):
        dash_app.callback(self._output, Input(self.get_button_id(), 'value'),
                          prevent_initial_call=prevent_initial_call)(self.call)

    def get_html(self):
        return dbc.Col([dbc.Switch(
            id=self.get_button_id(),
            value=self.value,
            label=self.label
        ), html.Div(id=self.get_response_id())])

    def call(self, value):
        if value is None or value == Switch._shown.get(self.get_button_id()):
            raise PreventUpdate()
        before = Switch._shown.get(self.get_button_id())
        Switch._shown[self.get_button_id()] = value
        try:
            self._fct(self._element_id, value)
        except RateLimitException:
            # nothing was sent: only switching to this value again has something to do
            Switch._shown[self.get_button_id()] = before
            return RATE_LIMIT_MESSAGE
        return " "
