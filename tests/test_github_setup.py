import contextlib
import json
import logging
import os
import tempfile
import unittest
from unittest.mock import patch

import requests

from psa_car_controller.psa.setup.github import GithubError, get_github_sha_from_file, urlretrieve_from_github
from tests.github_fixture import FakeGithub, RAW_URL, TREE_URL, build_apk_archive, rate_limit_response, response

APK = "mypeugeot.apk"
ARCHIVE = APK + ".bz2"
USER = "flobz"
REPO = "psa_apk"


class WorkingDirTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cwd = contextlib.chdir(tmp.name)
        cwd.__enter__()
        self.addCleanup(cwd.__exit__, None, None, None)

    @staticmethod
    def write(name, content):
        with open(name, "wb") as f:
            f.write(content)

    @staticmethod
    def read(name):
        with open(name, "rb") as f:
            return f.read()


class TestGithubSha(unittest.TestCase):
    def get_sha_error(self, tree_answer) -> str:
        with patch("requests.get", side_effect=FakeGithub(tree_answer=tree_answer).get), \
                self.assertRaises(GithubError) as error:
            get_github_sha_from_file(USER, REPO, "", ARCHIVE)
        return str(error.exception)

    def test_rate_limit_is_named(self):
        message = self.get_sha_error(rate_limit_response())
        self.assertIn("rate limit", message)
        self.assertNotIn("203.0.113.7", message)

    def test_secondary_rate_limit_is_named(self):
        message = self.get_sha_error(response(429, b'{"message": "You have exceeded a secondary rate limit"}'))
        self.assertIn("rate limit", message)

    def test_other_refusal_gives_the_status(self):
        self.assertIn("404", self.get_sha_error(response(404, b'{"message": "Not Found"}')))
        self.assertIn("403", self.get_sha_error(response(403, b'{"message": "Forbidden"}')))

    def test_network_failure(self):
        self.assertIn("can't be reached", self.get_sha_error(requests.ConnectionError("dns")))
        self.assertIn("can't be reached", self.get_sha_error(requests.Timeout()))

    def test_unexpected_answers(self):
        for content in (b"<html>", b"[]", b"{}", b'{"tree": null}', b'{"tree": [{"path": "other", "sha": "1"}]}',
                        b'{"tree": [{"sha": "1"}]}', b'{"tree": ["other"]}'):
            with self.subTest(content):
                self.assertIn(ARCHIVE, self.get_sha_error(response(content=content)))

    def test_is_a_connection_error_for_the_web_setup(self):
        # config_views shows the message of a ConnectionError without a stack trace
        self.assertTrue(issubclass(GithubError, ConnectionError))


class TestUrlRetrieve(WorkingDirTestCase):
    def retrieve(self, github):
        with patch("requests.get", side_effect=github.get):
            urlretrieve_from_github(USER, REPO, "", APK)

    def test_first_download_doesnt_need_the_api(self):
        github = FakeGithub({ARCHIVE: build_apk_archive()})
        self.retrieve(github)
        self.assertEqual([RAW_URL + ARCHIVE], github.urls)
        self.assertTrue(os.path.getsize(APK))

    def test_latest_version_isnt_downloaded_again(self):
        archive = build_apk_archive()
        github = FakeGithub({ARCHIVE: archive})
        self.write(ARCHIVE, archive)
        self.write(APK, b"local")
        self.retrieve(github)
        self.assertEqual([TREE_URL], github.urls)
        self.assertEqual(b"local", self.read(APK))

    def test_new_version_is_downloaded(self):
        github = FakeGithub({ARCHIVE: build_apk_archive()})
        self.write(ARCHIVE, b"old")
        self.write(APK, b"old")
        self.retrieve(github)
        self.assertEqual([TREE_URL, RAW_URL + ARCHIVE], github.urls)
        self.assertNotEqual(b"old", self.read(APK))

    def test_rate_limit_uses_the_local_apk(self):
        self.write(ARCHIVE, b"local archive")
        self.write(APK, b"local")
        github = FakeGithub(tree_answer=rate_limit_response())
        with self.assertLogs("psa_car_controller.psa.setup.github", logging.WARNING) as logs:
            self.retrieve(github)
        self.assertEqual([TREE_URL], github.urls)
        self.assertEqual(b"local", self.read(APK))
        self.assertEqual(b"local archive", self.read(ARCHIVE))
        self.assertIn("rate limit", logs.output[0])
        self.assertNotIn("203.0.113.7", logs.output[0])

    def test_offline_uses_the_local_apk(self):
        self.write(ARCHIVE, b"local archive")
        self.write(APK, b"local")
        self.retrieve(FakeGithub(tree_answer=requests.ConnectionError("dns")))
        self.assertEqual(b"local", self.read(APK))

    def test_rate_limit_without_local_apk_downloads_it(self):
        # the archive stayed but the apk is gone: downloads aren't counted by the API rate limit
        self.write(ARCHIVE, b"local archive")
        github = FakeGithub({ARCHIVE: build_apk_archive()}, tree_answer=rate_limit_response())
        self.retrieve(github)
        self.assertEqual([TREE_URL, RAW_URL + ARCHIVE], github.urls)
        self.assertTrue(os.path.getsize(APK))

    def test_failed_download_has_a_clear_message(self):
        for failure, name in ((response(404), "refused"), (requests.ConnectionError("dns"), "offline")):
            with self.subTest(name), patch("requests.get", side_effect=[failure]), \
                    self.assertRaises(GithubError) as error:
                urlretrieve_from_github(USER, REPO, "", APK)
            self.assertIn(ARCHIVE, str(error.exception))
            self.assertIn("internet connection", str(error.exception))
            self.assertFalse(os.path.exists(APK))

    def test_refused_download_keeps_the_archive(self):
        self.write(ARCHIVE, b"local archive")
        self.write(APK, b"local")
        new_version = response(content=json.dumps({"tree": [{"path": ARCHIVE, "sha": "new"}]}).encode())
        with patch("requests.get", side_effect=[new_version, response(404)]), self.assertRaises(GithubError):
            urlretrieve_from_github(USER, REPO, "", APK)
        self.assertEqual(b"local archive", self.read(ARCHIVE))
        self.assertEqual(b"local", self.read(APK))


class TestWebSetupMessage(WorkingDirTestCase):
    def test_setup_without_network_explains_the_failure(self):
        # regression: the page displayed only "'tree'"
        from psa_car_controller.psa.setup.app_decoder import InitialSetup
        self.write(ARCHIVE, b"local archive")
        with patch("requests.get", side_effect=[rate_limit_response(), response(503)]), \
                self.assertRaises(ConnectionError) as error:
            InitialSetup("com.psa.mym.mypeugeot", "user@example.com", "password", "FR")
        self.assertIn("internet connection", str(error.exception))
        self.assertNotIn("password", str(error.exception))
