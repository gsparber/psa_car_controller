"""A dev build has no release on GitHub: the UI names it and doesn't link to a tag that doesn't exist."""
import unittest
from unittest.mock import patch

from dash import html

from tests.test_remote_command_ui import text
from tests.utils import get_web_view

RELEASES = "https://github.com/flobz/psa_car_controller/releases/tag/"


def anchors(component):
    found = [component] if isinstance(component, html.A) else []
    children = getattr(component, "children", None)
    if not isinstance(children, (list, tuple)):
        children = [children]
    for child in children:
        if child is not None and not isinstance(child, str):
            found += anchors(child)
    return found


class TestVersionLabel(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.views = get_web_view("views")
        cls.settings = get_web_view("settings")
        cls.ui = get_web_view("ui")

    def header(self, version):
        with patch.object(self.ui, "__version__", version):
            return self.views.add_header(html.Div())[0]

    def more(self, version):
        with patch.object(self.ui, "__version__", version):
            return self.settings._more()

    def tag_links(self, component):
        return [a.href for a in anchors(component) if str(getattr(a, "href", "")).startswith("https://github.com")]

    def test_dev_builds_have_no_link(self):
        for version in ("0.0.0", "unknown"):
            with self.subTest(version=version):
                self.assertEqual([], self.tag_links(self.header(version)))
                self.assertEqual([], self.tag_links(self.more(version)))

    def test_dev_builds_are_named(self):
        for version in ("0.0.0", "unknown"):
            with self.subTest(version=version):
                for component in (self.header(version), self.more(version)):
                    self.assertIn("dev build", text(component))
                    self.assertNotIn("v" + version, text(component))

    def test_releases_still_link_to_their_tag(self):
        for version in ("3.7.9", "3.7.2b0", "3.0.0rc0"):
            with self.subTest(version=version):
                for component in (self.header(version), self.more(version)):
                    self.assertEqual([RELEASES + "v" + version], self.tag_links(component))
                    self.assertIn("v" + version, text(component))
                    self.assertNotIn("dev build", text(component))


if __name__ == "__main__":
    unittest.main()
