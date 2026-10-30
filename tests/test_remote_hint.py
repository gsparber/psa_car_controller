"""The home page says why the remote tiles are locked, in line with the settings page.

Before, a start with --remote-disable or --offline showed "not set up" and sent the user to the settings page, where
there is nothing to set up.
"""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from dash import dcc

from tests.test_remote_command_ui import text
from tests.test_status_age import get_car
from tests.utils import get_web_view


def has_link(component):
    if isinstance(component, dcc.Link):
        return True
    children = getattr(component, "children", None)
    if not isinstance(children, (list, tuple)):
        children = [children]
    return any(has_link(child) for child in children if child is not None and not isinstance(child, str))


class TestRemoteHint(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.home = get_web_view("home")
        cls.settings = get_web_view("settings")

    def tiles(self, remote_control, remote_ready):
        with patch.object(self.home, "app", return_value=SimpleNamespace(remote_control=remote_control)), \
                patch.object(self.home, "command_supported", return_value=True):
            return self.home._tiles(get_car(), remote_ready, None)

    def test_disabled_at_start_is_not_called_unset(self):
        tiles = self.tiles(False, False)
        self.assertIn("Remote control is off.", text(tiles))
        self.assertIn("--remote-disable", text(tiles))
        self.assertNotIn("not set up", text(tiles))

    def test_disabled_at_start_has_nothing_to_open_in_settings(self):
        self.assertFalse(has_link(self.tiles(False, False)))

    def test_same_reason_as_the_settings_page(self):
        self.assertIn(text(self.settings._remote(self.settings.REMOTE_OFF)[1]), text(self.tiles(False, False)))

    def test_missing_setup_still_links_to_settings(self):
        tiles = self.tiles(True, False)
        self.assertIn("Remote control is not set up.", text(tiles))
        self.assertTrue(has_link(tiles))

    def test_no_hint_when_remote_works(self):
        tiles = self.tiles(True, True)
        self.assertNotIn("Remote control", text(tiles))
        self.assertFalse(has_link(tiles))


if __name__ == "__main__":
    unittest.main()
