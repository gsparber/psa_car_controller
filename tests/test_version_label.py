"""A dev build has no release on GitHub: the UI names its commit, or itself, and doesn't link to a tag that doesn't exist."""
import os
import unittest
from unittest.mock import patch

from dash import html

from psa_car_controller.common import build_version
from tests.test_remote_command_ui import text
from tests.utils import get_web_view

RELEASES = "https://github.com/flobz/psa_car_controller/releases/tag/"
REPO = "https://github.com/gsparber/psa_car_controller"
DEV_VERSIONS = ("0.0.0", "unknown")
UNSAFE = ("<script>alert(1)</script>", 'v1"onclick="x', "javascript:alert(1)", "v3.7.7 -1-gabc1234", "v3.7.7\n", "-v1",
          "../../evil", "a" * 101)


def anchors(component):
    found = [component] if isinstance(component, html.A) else []
    children = getattr(component, "children", None)
    if not isinstance(children, (list, tuple)):
        children = [children]
    for child in children:
        if child is not None and not isinstance(child, str):
            found += anchors(child)
    return found


def build(version, git_version="", repo=""):
    """A build with this package version and these arguments of docker build."""
    env = {"PSACC_GIT_VERSION": git_version, "PSACC_GIT_REPO_URL": repo}
    return patch.object(build_version, "__version__", version), patch.dict(os.environ, env)


class TestVersionLabel(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.views = get_web_view("views")
        cls.settings = get_web_view("settings")

    def header(self, version, git_version="", repo=""):
        package, env = build(version, git_version, repo)
        with package, env:
            return self.views.add_header(html.Div())[0]

    def more(self, version, git_version="", repo=""):
        package, env = build(version, git_version, repo)
        with package, env:
            return self.settings._more()

    def tag_links(self, component):
        return [a.href for a in anchors(component) if str(getattr(a, "href", "")).startswith("https://")]

    def test_dev_builds_have_no_link(self):
        for version in DEV_VERSIONS:
            with self.subTest(version=version):
                self.assertEqual([], self.tag_links(self.header(version)))
                self.assertEqual([], self.tag_links(self.more(version)))

    def test_dev_builds_are_named(self):
        for version in DEV_VERSIONS:
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

    def test_a_release_ignores_the_git_state(self):
        build = ("3.7.9", "v3.7.7-71-g49f88b8", REPO)
        for component in (self.header(*build), self.more(*build)):
            self.assertEqual([RELEASES + "v3.7.9"], self.tag_links(component))
            self.assertIn("v3.7.9", text(component))
            self.assertNotIn("49f88b8", text(component))

    def test_dev_build_shows_and_links_its_commit(self):
        for git_version, commit in (("v3.7.7-71-g49f88b8", "49f88b8"), ("49f88b8", "49f88b8"),
                                    ("v3.7.2-rc.1-3-g0123456789ab", "0123456789ab")):
            for version in DEV_VERSIONS:
                with self.subTest(git_version=git_version, version=version):
                    for component in (self.header(version, git_version, REPO), self.more(version, git_version, REPO)):
                        self.assertEqual([f"{REPO}/commit/{commit}"], self.tag_links(component))
                        self.assertIn(git_version, text(component))
                        self.assertNotIn("dev build", text(component))

    def test_commit_without_repository_has_no_link(self):
        for repo in ("", "http://github.com/a/b", "javascript:alert(1)", "git@github.com:a/b.git",
                     'https://github.com/a"><script>', "https://github.com/a/b\n", "https://github.com/a/b/"):
            with self.subTest(repo=repo):
                for component in (self.header("0.0.0", "v3.7.7-71-g49f88b8", repo),
                                  self.more("0.0.0", "v3.7.7-71-g49f88b8", repo)):
                    self.assertEqual([], self.tag_links(component))
                    self.assertIn("v3.7.7-71-g49f88b8", text(component))

    def test_dirty_state_is_marked_and_not_linked(self):
        for git_version in ("v3.7.7-71-g49f88b8-dirty", "49f88b8-dirty", "v3.7.7-dirty"):
            with self.subTest(git_version=git_version):
                for component in (self.header("0.0.0", git_version, REPO), self.more("0.0.0", git_version, REPO)):
                    self.assertEqual([], self.tag_links(component))
                    self.assertIn(git_version, text(component))

    def test_exactly_on_a_tag_has_no_commit_to_link(self):
        self.assertEqual([], self.tag_links(self.header("0.0.0", "v3.7.7", REPO)))
        self.assertIn("v3.7.7", text(self.header("0.0.0", "v3.7.7", REPO)))

    def test_unsafe_git_state_is_ignored(self):
        for git_version in UNSAFE:
            with self.subTest(git_version=git_version):
                for component in (self.header("0.0.0", git_version, REPO), self.more("0.0.0", git_version, REPO)):
                    self.assertEqual([], self.tag_links(component))
                    self.assertIn("dev build", text(component))
                    self.assertNotIn(git_version, text(component))


class TestAppVersion(unittest.TestCase):
    """The name in the log and in --version."""

    def app_version(self, version, git_version=""):
        package, env = build(version, git_version)
        with package, env:
            return build_version.app_version()

    def test_release(self):
        self.assertEqual("3.7.9", self.app_version("3.7.9"))
        self.assertEqual("3.7.9", self.app_version("3.7.9", "v3.7.7-71-g49f88b8"))

    def test_dev_build_uses_the_git_state(self):
        self.assertEqual("v3.7.7-71-g49f88b8-dirty", self.app_version("0.0.0", "v3.7.7-71-g49f88b8-dirty"))

    def test_dev_build_without_usable_git_state_stays(self):
        self.assertEqual("0.0.0", self.app_version("0.0.0"))
        self.assertEqual("unknown", self.app_version("unknown"))
        for git_version in UNSAFE:
            with self.subTest(git_version=git_version):
                self.assertEqual("0.0.0", self.app_version("0.0.0", git_version))

    def test_missing_variables_are_a_dev_build(self):
        with patch.object(build_version, "__version__", "0.0.0"), patch.dict(os.environ):
            os.environ.pop("PSACC_GIT_VERSION", None)
            os.environ.pop("PSACC_GIT_REPO_URL", None)
            self.assertEqual(("dev build", None), build_version.version_info())


if __name__ == "__main__":
    unittest.main()
