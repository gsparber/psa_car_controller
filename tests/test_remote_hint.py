"""The home page says why the remote tiles are locked, in line with the settings page.

Before, a start with --remote-disable or --offline showed "not set up" and sent the user to the settings page, where
there is nothing to set up.
"""
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from dash import dcc, html

from psa_car_controller.web import units
from tests.test_remote_command_ui import text
from tests.test_status_age import get_car
from tests.utils import get_web_view

OFF = "Remote control is off. Started with --remote-disable or --offline."
NOT_SET_UP = "Remote control is not set up."
NOT_INCLUDED = "Not included in this car's subscription"


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
        cls.car_access = get_web_view("car_access")

    def tiles(self, remote_control, remote_ready):
        with patch.object(self.car_access, "app", return_value=SimpleNamespace(remote_control=remote_control)), \
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


def find_button(component, label):
    """The button with this text, searched in the tree of a page."""
    if type(component).__name__ == "Button" and label in text(component):
        return component
    children = getattr(component, "children", None)
    for child in children if isinstance(children, (list, tuple)) else [children]:
        if child is not None and not isinstance(child, str):
            found = find_button(child, label)
            if found is not None:
                return found
    return None


class TestLockedButtons(unittest.TestCase):
    """The buttons of the charge and climate pages, for each reason they can be locked."""

    @classmethod
    def setUpClass(cls):
        cls.charge = get_web_view("charge")
        cls.climate = get_web_view("climate")
        cls.car_access = get_web_view("car_access")

    def setUp(self):
        patcher = patch.object(units, "current", return_value=units.KM)
        patcher.start()
        self.addCleanup(patcher.stop)

    def pages(self, remote_control, remote_ready, supported=True):
        """What each page shows around its button, by the label of the button."""
        state = SimpleNamespace(remote_control=remote_control)
        car = get_car()
        with patch.object(self.car_access, "app", return_value=state), \
                patch.object(self.charge, "remote_ready", return_value=remote_ready), \
                patch.object(self.climate, "remote_ready", return_value=remote_ready), \
                patch.object(self.charge, "command_supported", return_value=supported), \
                patch.object(self.climate, "command_supported", return_value=supported), \
                patch.object(self.climate, "current_programs", return_value={
                    key: {"on": False, "hour": 0, "minute": 0, "day": [0] * 7} for key in
                    self.climate.precond_programs.KEYS}):
            charge = self.charge._status(car, None)
            climate = self.climate._status(car, None)
            programs = html.Div(self.climate._programs(car))
        return {"Start charging": charge, "Turn climate on": climate, "Save schedules": programs}

    def test_remote_off(self):
        for label, page in self.pages(False, False).items():
            with self.subTest(label):
                self.assertTrue(find_button(page, label).disabled)
                self.assertIn(OFF, text(page))
                self.assertNotIn(NOT_SET_UP, text(page))
                self.assertFalse(has_link(page))

    def test_same_text_as_the_home_page(self):
        home = get_web_view("home")
        with patch.object(self.car_access, "app", return_value=SimpleNamespace(remote_control=False)), \
                patch.object(home, "command_supported", return_value=True):
            self.assertIn(OFF, text(home._tiles(get_car(), False, None)))

    def test_not_set_up_links_to_the_settings(self):
        for label, page in self.pages(True, False).items():
            with self.subTest(label):
                self.assertTrue(find_button(page, label).disabled)
                self.assertIn(NOT_SET_UP, text(page))
                self.assertTrue(has_link(page))

    def test_no_hint_when_remote_works(self):
        for label, page in self.pages(True, True).items():
            with self.subTest(label):
                self.assertFalse(find_button(page, label).disabled)
                self.assertNotIn("Remote control", text(page))

    def test_the_subscription_goes_first(self):
        pages = self.pages(False, False, supported=False)
        for label in ("Start charging", "Turn climate on"):
            with self.subTest(label):
                self.assertIsNone(find_button(pages[label], label))
                self.assertIn(NOT_INCLUDED, text(pages[label]))
                self.assertNotIn("Remote control", text(pages[label]))
        # the line about the subscription is above the schedules, on the same page
        self.assertTrue(find_button(pages["Save schedules"], "Save schedules").disabled)
        self.assertNotIn("Remote control", text(pages["Save schedules"]))

    def test_waiting_for_the_car_is_no_lock_to_explain(self):
        car = get_car()
        pending = {car.vin: {"charge": {"at": time.time(), "target": True}}}
        with patch.object(self.charge, "remote_ready", return_value=True), \
                patch.object(self.charge, "command_supported", return_value=True):
            page = self.charge._status(car, pending)
        self.assertNotIn("Remote control", text(page))


if __name__ == "__main__":
    unittest.main()
