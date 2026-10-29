"""Remote command endpoints give the id of the command they sent, its status can be asked for (T34)."""
import unittest
from unittest.mock import MagicMock, patch

from dash import html

from psa_car_controller.psa.remote_commands import WOKEN_UP
from psa_car_controller.web import app as web_app
from tests.test_remote_command_answers import answer, published, OTHER_VIN
from tests.test_remote_commands import get_ready_remote_client, VIN


def get_api_view():
    with patch("sys.argv", ["psa-car-controller"]):  # the app singleton parses the command line
        if web_app.app is None:  # run alone: the integration tests set the app up otherwise
            web_app.config_flask("test", "/", False, "127.0.0.1", 0, view="psa_car_controller.web.view.api")
            web_app.dash_app.layout = html.Div()  # dash refuses requests without one
        from psa_car_controller.web.view import api as api_view  # pylint: disable=import-outside-toplevel
    return api_view


class TestCommandApi(unittest.TestCase):

    def setUp(self):
        api_view = get_api_view()
        self.rc = get_ready_remote_client()
        self.patch = patch.object(api_view, "APP", MagicMock(myp=MagicMock(remote_client=self.rc)))
        self.patch.start()
        self.client = api_view.app.test_client()

    def tearDown(self):
        self.patch.stop()

    def test_command_answer_is_unchanged_and_has_its_id(self):
        response = self.client.get(f"/horn/{VIN}/2")

        self.assertEqual(200, response.status_code)
        self.assertIs(True, response.get_json())  # same body as before
        [(_, horn)] = published(self.rc)
        self.assertEqual(horn["correlation_id"], response.headers["X-Correlation-Id"])
        self.assertIn("X-Correlation-Id", response.headers["Access-Control-Expose-Headers"])

    def test_command_status(self):
        correlation_id = self.client.get(f"/lock_door/{VIN}/1").headers["X-Correlation-Id"]
        self.rc._on_mqtt_message(None, None, answer(correlation_id, "901"))

        state = self.client.get(f"/command/{correlation_id}").get_json()
        self.assertEqual(("/Doors", WOKEN_UP, True), (state["name"], state["status"], state["pending"]))
        self.assertEqual([correlation_id], [command["correlation_id"]
                                            for command in self.client.get(f"/commands/{VIN}").get_json()])
        self.assertEqual([], self.client.get(f"/commands/{OTHER_VIN}").get_json())

    def test_unknown_command(self):
        response = self.client.get("/command/unknown")
        self.assertEqual(404, response.status_code)
        self.assertEqual("unknown_command", response.get_json()["error"])

    def test_no_header_when_nothing_was_sent(self):
        self.rc.remoteCredentials.access_token = None
        self.rc._refresh_remote_token = MagicMock(return_value=False)
        response = self.client.get(f"/horn/{VIN}/2")
        self.assertIs(False, response.get_json())
        self.assertNotIn("X-Correlation-Id", response.headers)


if __name__ == '__main__':
    unittest.main()
