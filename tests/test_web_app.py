import unittest
from unittest.mock import patch

from psa_car_controller.web import app as web_app

# no routes or callbacks of its own, so the test does not depend on the views
VIEW = "psa_car_controller.web.units"


class TestConfigFlask(unittest.TestCase):
    def setUp(self):
        for name in ("app", "dash_app"):
            patcher = patch.object(web_app, name, None)
            patcher.start()
            self.addCleanup(patcher.stop)

    def config_flask(self):
        return web_app.config_flask("test", "/", False, "127.0.0.1", 0, view=VIEW)

    def test_first_call_creates_the_app(self):
        config = self.config_flask()
        self.assertIsNotNone(web_app.app)
        self.assertIs(web_app.app, web_app.dash_app.server)
        self.assertEqual("127.0.0.1", config["hostname"])

    def test_second_call_keeps_the_running_app(self):
        self.config_flask()
        app, dash_app = web_app.app, web_app.dash_app
        with self.assertRaisesRegex(RuntimeError, "once per process"):
            self.config_flask()
        self.assertIs(app, web_app.app)
        self.assertIs(dash_app, web_app.dash_app)

    def test_prefix_of_a_request_doesnt_stay(self):
        # a test client runs the request in its own thread: an ingress prefix stayed for whatever ran there next
        self.config_flask()
        config = web_app.dash_app.config
        before = config.requests_pathname_prefix
        response = web_app.app.test_client().get("/_dash-layout", headers={"X-Ingress-Path": "/api/hassio_ingress/x"})
        response.close()
        self.assertEqual(before, config.requests_pathname_prefix)


if __name__ == '__main__':
    unittest.main()
