"""/config_connect is reached with the address of the login in ?url=, by hand it can come without it."""
import unittest

from psa_car_controller.web import app as web_app
from tests.utils import get_web_view


def page_of(search):
    return web_app.app.test_client().post("/_dash-update-component", json={
        "output": "page-content.children", "outputs": {"id": "page-content", "property": "children"},
        "inputs": [{"id": "url", "property": "pathname", "value": "/config_connect"},
                   {"id": "url", "property": "search", "value": search}],
        "changedPropIds": ["url.pathname"]})


class TestOauthPageWithoutLink(unittest.TestCase):

    def setUp(self):
        get_web_view("views")

    def test_hint_without_url(self):
        for search in ("", "?url=", "?other=1"):
            with self.subTest(search=search):
                response = page_of(search)
                self.assertEqual(200, response.status_code)
                body = response.get_data(as_text=True)
                self.assertIn("needs the link of a login", body)
                self.assertNotIn("psa-oauth-code", body)

    def test_form_with_url(self):
        response = page_of("?url=https%3A%2F%2Flogin.example.com%2Fstart")
        self.assertEqual(200, response.status_code)
        body = response.get_data(as_text=True)
        self.assertIn("login.example.com", body)
        self.assertIn("psa-oauth-code", body)
