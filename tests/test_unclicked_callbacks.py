"""Dash also runs a callback when its input comes into the page: without a click nothing may leave the app."""
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, call, patch

from dash.exceptions import PreventUpdate

from psa_car_controller.common.utils import RateLimitException
from psa_car_controller.psa.connected_car_api import ApiClient
from psa_car_controller.psacc.model.car import Car
from psa_car_controller.web import app as web_app
from tests.data.car_status import ELECTRIC_CAR_STATUS
from tests.utils import get_web_view, isolate_jobs


def dispatch(output, input_id, prop, value):
    """A request as the browser sends it, with the input listed as changed: the worst case of a callback at load."""
    return web_app.app.test_client().post("/_dash-update-component", json={
        "output": output, "outputs": dict(zip(("id", "property"), output.rsplit(".", 1))),
        "inputs": [{"id": input_id, "property": prop, "value": value}],
        "changedPropIds": [f"{input_id}.{prop}"]})


def ids_in(component):
    if isinstance(component, (list, tuple)):
        return [found for child in component for found in ids_in(child)]
    own = [component.id] if getattr(component, "id", None) else []
    return own + ids_in(getattr(component, "children", None) or [])


def callback_of(output):
    dependencies = web_app.app.test_client().get("/_dash-dependencies").get_json()
    return next(callback for callback in dependencies if callback["output"] == output)


class TestAskCode(unittest.TestCase):
    OUTPUT = "sms-demand-result.children"

    def setUp(self):
        self.views = get_web_view("config_views")
        patcher = patch.object(self.views, "app", MagicMock())
        self.controller = patcher.start()
        self.addCleanup(patcher.stop)
        self.get_sms = self.controller.myp.remote_client.get_sms_otp_code
        cooldown = patch.object(self.views, "SMS_COOLDOWN", self.views.Cooldown(30))
        cooldown.start()
        self.addCleanup(cooldown.stop)

    def test_not_run_when_the_button_comes_into_the_page(self):
        self.assertTrue(callback_of(self.OUTPUT)["prevent_initial_call"])

    def test_no_sms_without_a_click(self):
        for clicks in (None, 0):
            with self.subTest(clicks=clicks):
                response = dispatch(self.OUTPUT, "ask-sms", "n_clicks", clicks)
                self.assertEqual(204, response.status_code)
        self.get_sms.assert_not_called()

    def test_a_click_sends_one_sms(self):
        response = dispatch(self.OUTPUT, "ask-sms", "n_clicks", 1)
        self.assertEqual(200, response.status_code)
        self.assertIn("SMS sent", response.get_data(as_text=True))
        self.get_sms.assert_called_once_with()


class TestJobsNeedAClick(unittest.TestCase):
    """The answer of a login or an activation can only be given once, and repeated failures can lock the account."""

    def setUp(self):
        self.registry = isolate_jobs(self)
        self.views = get_web_view("config_views")
        self.oauth = get_web_view("config_oauth")
        patcher = patch.object(self.registry, "start")
        self.start = patcher.start()
        self.addCleanup(patcher.stop)

    def test_otp(self):
        for clicks in (None, 0):
            with self.subTest(clicks=clicks), self.assertRaises(PreventUpdate):
                self.views.finishOtp(clicks, "1234", "123456")
        self.start.assert_not_called()

    def test_login(self):
        for clicks in (None, 0):
            with self.subTest(clicks=clicks), self.assertRaises(PreventUpdate):
                self.views.connectPSA(clicks, "com.psa.mym.myopel", "someone@example.com", "password", "DE")
        self.start.assert_not_called()

    def test_oauth_code(self):
        with patch.object(self.views, "INITIAL_SETUP", MagicMock()):
            for clicks in (None, 0):
                with self.subTest(clicks=clicks), self.assertRaises(PreventUpdate):
                    self.oauth.finish_oauth(clicks, "code")
        self.start.assert_not_called()


class TestControlButtons(unittest.TestCase):
    """/control: a wakeup costs battery of the car, charge and climate change what the car does."""

    def setUp(self):
        control = get_web_view("control")  # the Dash app has to exist before these two are imported
        self.button, self.switch = control.Button, control.Switch
        self.fct = MagicMock()

    def test_button_needs_a_click(self):
        button = self.button("t71-button", "needs-click", "Label", self.fct)
        for clicks in (None, 0):
            with self.subTest(clicks=clicks):
                response = dispatch(f"{button.get_response_id()}.children", button.get_button_id(), "n_clicks", clicks)
                self.assertEqual(204, response.status_code)
        self.fct.assert_not_called()

    def test_button_sends_on_a_click(self):
        button = self.button("t71-button", "click", "Label", self.fct)
        response = dispatch(f"{button.get_response_id()}.children", button.get_button_id(), "n_clicks", 1)
        self.assertEqual(200, response.status_code)
        self.fct.assert_called_once_with("click")

    def test_switch_ignores_what_the_page_already_shows(self):
        switch = self.switch("t71-switch", "shown", "Label", self.fct, True)
        for value in (None, True):
            with self.subTest(value=value), self.assertRaises(PreventUpdate):
                switch.call(value)
        self.fct.assert_not_called()

    def test_switch_sends_a_change(self):
        switch = self.switch("t71-switch", "change", "Label", self.fct, False)
        response = dispatch(f"{switch.get_response_id()}.children", switch.get_button_id(), "value", True)
        self.assertEqual(200, response.status_code)
        self.fct.assert_called_once_with("change", True)

    def test_switch_back_before_the_car_reports(self):
        """Compared with the page and not the car: the status still says charging, but this click has to count."""
        switch = self.switch("t71-switch", "back", "Label", self.fct, True)
        switch.call(False)
        switch.call(True)
        self.assertEqual([call("back", False), call("back", True)], self.fct.call_args_list)

    def test_switch_follows_the_last_render(self):
        """The callback stays the one of the first switch, the page shows the state of the last one."""
        first = self.switch("t71-switch", "render", "Label", self.fct, False)
        self.switch("t71-switch", "render", "Label", MagicMock(), True)
        with self.assertRaises(PreventUpdate):
            first.call(True)
        first.call(False)
        self.fct.assert_called_once_with("render", False)

    def test_switch_over_the_rate_limit_answers_like_the_button(self):
        self.fct.side_effect = RateLimitException("wakeup")
        switch = self.switch("t73-switch", "limit", "Label", self.fct, False)
        response = dispatch(f"{switch.get_response_id()}.children", switch.get_button_id(), "value", True)
        self.assertEqual(200, response.status_code)
        self.assertIn("Too many requests", response.get_data(as_text=True))

    def test_switch_can_retry_after_the_rate_limit(self):
        """Nothing was sent: the same value again has to go out, switching back has nothing to undo."""
        switch = self.switch("t73-switch", "retry", "Label", self.fct, False)
        self.fct.side_effect = [RateLimitException("wakeup"), None]
        switch.call(True)
        with self.assertRaises(PreventUpdate):
            switch.call(False)
        self.assertEqual(" ", switch.call(True))
        self.assertEqual([call("retry", True), call("retry", True)], self.fct.call_args_list)


class TestControlAfterLogin(unittest.TestCase):
    """The callbacks of /control stay registered, a login makes a new client."""

    def setUp(self):
        self.control = get_web_view("control")
        self.vin = "VR3-" + self._testMethodName  # a new button per test: the first build keeps the callback
        self.old_client, self.new_client = MagicMock(name="old"), MagicMock(name="new")
        self.old_client.abrp.abrp_enable_vin = []
        car = Car(self.vin, "vehicle-id", "Peugeot")
        car.status = MagicMock()
        car.status.preconditionning.air_conditioning.status = "Disabled"
        car.status.get_energy.return_value.level = 50
        car.status.get_energy.return_value.charging.status = "Stopped"
        car.status.get_energy.return_value.updated_at = datetime(2026, 10, 30, tzinfo=timezone.utc)
        car.status.timed_odometer.mileage = 1000
        self.old_client.vehicles_list = [car]
        self.config = MagicMock(myp=self.old_client, remote_control=True, offline=False)
        with patch.object(self.control.Database, "get_last_soh_by_vin", return_value=None):
            self.control.get_control_tabs(self.config)
        self.config.myp = self.new_client

    def click(self, role, prop, value):
        button = f"{role}-{self.vin}"
        response = dispatch(f"{button}-response.children", button, prop, value)
        self.assertEqual(200, response.status_code)

    def test_refresh(self):
        self.click(self.control.REFRESH_SWITCH, "n_clicks", 1)
        self.new_client.remote_client.wakeup.assert_called_once_with(self.vin)
        self.old_client.remote_client.wakeup.assert_not_called()

    def test_charge(self):
        self.click(self.control.CHARGE_SWITCH, "value", True)
        self.new_client.remote_client.charge_now.assert_called_once_with(self.vin, True)
        self.old_client.remote_client.charge_now.assert_not_called()

    def test_preconditioning(self):
        self.click(self.control.PRECONDITIONING_SWITCH, "value", True)
        self.new_client.remote_client.preconditioning.assert_called_once_with(self.vin, True)
        self.old_client.remote_client.preconditioning.assert_not_called()

    def test_abrp(self):
        self.click(self.control.ABRP_SWITCH, "value", True)
        self.new_client.abrp.enable_abrp.assert_called_once_with(self.vin, True)
        self.old_client.abrp.enable_abrp.assert_not_called()


class TestHomePoll(unittest.TestCase):

    def test_abrp_switch_and_its_output_come_in_together(self):
        """prevent_initial_call only holds a callback back when its output comes into the page with its input."""
        home = get_web_view("home")
        with patch.object(home, "app") as app, patch.object(home, "remote_ready", return_value=True), \
                patch.object(home, "command_supported", return_value=True), \
                patch.object(home, "maintenance", return_value=None), \
                patch.object(home, "_picture", return_value=None), \
                patch.object(home.Database, "get_last_soh_by_vin", return_value=None):
            app.return_value.offline = False
            app.return_value.chc.get.return_value = None
            app.return_value.myp.abrp.abrp_enable_vin = []
            car = Car("VR3-home-poll", "vehicle-id", "Peugeot")
            car.status = ApiClient()._ApiClient__deserialize(ELECTRIC_CAR_STATUS, "Status")
            found = ids_in(home.render_status(car))
        self.assertIn(home.ABRP_SWITCH, found)
        self.assertIn(home.ABRP_OUT, found)
