"""The /control page shows its remote buttons whatever part of the status is missing.

Before, a status without its preconditioning part (or without a date) left out all three buttons, refresh, charge
and preconditioning, and logged a traceback at each build of the page.
"""
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from dash import html

from psa_car_controller.psa.connected_car_api.models.energy import Energy
from psa_car_controller.psa.connected_car_api.models.energy_charging import EnergyCharging
from psa_car_controller.psacc.model.car import Car
from psa_car_controller.psacc.model.car_status import CarStatus
from tests.utils import get_web_view

VIN = "VR3UHZKXZLT000000"
DATE = datetime(2026, 11, 10, 7, 0, tzinfo=timezone.utc)


def build(status):
    with patch("sys.argv", ["psa-car-controller"]):  # the app singleton parses the command line when made
        control = get_web_view("control")
    car = Car(VIN, "vehicle-id", "Peugeot")
    car.status = status
    config = MagicMock(offline=True, remote_control=True)
    config.myp.vehicles_list = [car]
    with patch.object(control.Database, "get_last_soh_by_vin", return_value=None), \
            patch.object(control, "Button") as button, patch.object(control, "Switch") as switch, \
            patch.object(control.logger, "exception") as exception, patch("sys.argv", ["psa-car-controller"]):
        button.return_value.get_html.return_value = html.Div()
        switch.return_value.get_html.return_value = html.Div()
        control.get_control_tabs(config)
    switches = {call.args[0]: call.args[4] for call in switch.call_args_list}
    return button, switches, exception


def status(charging_status="InProgress", updated_at=DATE):
    return CarStatus(links={}, energy=[Energy(type="Electric", level=70, updated_at=updated_at,
                                              charging=EnergyCharging(status=charging_status))])


class TestControlButtons(unittest.TestCase):

    def test_without_preconditioning_in_the_status(self):
        button, switches, exception = build(status())
        button.assert_called_once()
        self.assertEqual({"charge-switch": True, "preconditioning-switch": False}, switches)
        exception.assert_not_called()

    def test_without_date(self):
        button, switches, _ = build(status(updated_at=None))
        button.assert_called_once()
        self.assertEqual(2, len(switches))

    def test_with_everything(self):
        full = status(charging_status="Stopped")
        full.preconditionning = MagicMock()
        full.preconditionning.air_conditioning.status = "Enabled"
        _, switches, _ = build(full)
        self.assertEqual({"charge-switch": False, "preconditioning-switch": True}, switches)


if __name__ == "__main__":
    unittest.main()
