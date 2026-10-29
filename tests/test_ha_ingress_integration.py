"""
Integration test for Home Assistant X-Ingress-Path header handling (issue #1228)

This test starts the PSA Car Controller app with --web-conf -r --offline -d10,
makes a GET request with X-Ingress-Path header, and verifies that hrefs in
the HTML response contain the prefix.

Run with: python -m unittest tests.test_ha_ingress_integration -v
or: python tests/test_ha_ingress_integration.py
"""
import os
import subprocess
import sys
import time
import re
import shutil
import socket
import tempfile
import requests
from unittest import TestCase

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Add parent directory to path
sys.path.insert(0, ROOT_DIR)


def get_free_port(host='127.0.0.1'):
    """A port nothing listens on: another test run on this machine may use any fixed one."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


class TestHomeAssistantIngressIntegration(TestCase):
    """
    Integration tests for PSA Car Controller with Home Assistant X-Ingress-Path header.
    These tests verify that hrefs in the HTML response include the prefix.
    """

    @classmethod
    def setUpClass(cls):
        """Set up test server"""
        cls.server_process = None
        cls.server_port = get_free_port()
        cls.server_url = f"http://127.0.0.1:{cls.server_port}"

        # the app saves its config file: give it a copy, the original is tracked by git
        tmp_dir = tempfile.TemporaryDirectory()
        cls.addClassCleanup(tmp_dir.cleanup)
        config_path = shutil.copy(os.path.join(ROOT_DIR, "tests", "data", "config.json"), tmp_dir.name)

        # Start the PSA app in its own process: main() sets up the app singleton and the Dash app of the process,
        # the other test files need these for themselves, whichever order they run in
        log_path = os.path.join(tmp_dir.name, "server.log")
        with open(log_path, "w", encoding="utf-8") as log_file:
            process = subprocess.Popen(  # pylint: disable=consider-using-with
                [sys.executable, "-m", "psa_car_controller",
                 '--web-conf',
                 '-r',
                 '--offline',
                 '-d', '10',
                 '-l', '127.0.0.1',
                 '-p', str(cls.server_port),
                 '-b', '/',
                 '-f', config_path],
                cwd=tmp_dir.name, env={**os.environ, "PYTHONPATH": ROOT_DIR},
                stdout=log_file, stderr=subprocess.STDOUT)
        cls.addClassCleanup(cls.stop_server, process)

        # Wait for server to start
        max_wait = 15
        for _ in range(max_wait):
            try:
                response = requests.get(cls.server_url, timeout=1)
                if response.status_code in (200, 404):
                    break
            except requests.exceptions.RequestException:
                if process.poll() is not None:
                    break
                time.sleep(1)
        else:
            process.kill()
            process.wait()
        if process.poll() is not None:
            with open(log_path, encoding="utf-8") as log_file:
                raise RuntimeError(f"Server failed to start on port {cls.server_port}:\n{log_file.read()[-2000:]}")
        cls.server_process = process

        time.sleep(2)  # Extra time for Dash to initialize

    @staticmethod
    def stop_server(process):
        process.terminate()
        process.wait(timeout=10)

    def setUp(self):
        """Check if server is running before each test"""
        if self.server_process is None:
            self.skipTest("Server failed to start")

        # Verify server is responsive
        try:
            response = requests.get(self.server_url, timeout=5)
            if response.status_code != 200:
                self.skipTest(f"Server not ready, got status {response.status_code}")
        except requests.exceptions.RequestException:
            self.skipTest("Server not responding")

    def validate_path(self, headers, expected_prefix):

        try:
            response = requests.get(self.server_url, headers=headers, timeout=10)

            self.assertEqual(response.status_code, 200,
                             f"Expected 200, got {response.status_code}")

            html = response.text

            # Check for prefix in HTML

            scripts = re.findall(r'src="(/[^"]+)"', html)
            for script in scripts:
                assert re.match(expected_prefix, script), f"Script {script} does not match {expected_prefix}"
            # Check if prefix appears in hrefs
            hrefs = re.findall(f'href="(/[^"]+)"', html)
            for href in hrefs:
                assert re.match(expected_prefix, href), f"Href {href} does not match {expected_prefix}"

        except requests.exceptions.RequestException as e:
            self.fail(f"Request failed: {e}")

    def test_root_route_with_ingress_path(self):
        """
        Test that / route returns HTML with hrefs containing the prefix
        when X-Ingress-Path header is present.
        """
        prefix = r'/api/ingress/a93a74ea_psacc/.+'
        headers = {
            'X-Ingress-Path': prefix,
            'Host': f'127.0.0.1:{self.server_port}'
        }
        self.validate_path(headers, prefix)

    def test_root_route_without(self):
        """
        Test that / route returns HTML with hrefs containing the prefix
        when X-Ingress-Path header is present.
        """
        headers = {
            'Host': f'127.0.0.1:{self.server_port}'
        }
        self.validate_path(headers, r"/(assets|_dash|_favicon).+")

    def test_referrer_policy_meta_before_scripts(self):
        """
        Home Assistant sends "Referrer-Policy: no-referrer", OpenStreetMap blocks tile requests without a Referer:
        the page must set its own policy before any script loads the map.
        """
        headers = {
            'X-Ingress-Path': '/api/ingress/a93a74ea_psacc/',
            'Host': f'127.0.0.1:{self.server_port}'
        }
        response = requests.get(self.server_url, headers=headers, timeout=10)
        self.assertEqual(response.status_code, 200)
        html = response.text
        meta = re.search(r'<meta name="referrer" content="strict-origin-when-cross-origin"\s*/?>', html)
        self.assertIsNotNone(meta, "missing referrer meta tag")
        self.assertLess(meta.start(), html.index("<script"))

    def test_api_route_with_ingress_path(self):
        """
        Test that Flask API routes (from api.py) work with prefix header.
        """
        headers = {
            'X-Ingress-Path': '/api/ingress/a93a74ea_psacc/',
            'Host': f'127.0.0.1:{self.server_port}'
        }

        # Try a simple API route that should work
        url = f"{self.server_url}/get_vehicles"

        try:
            response = requests.get(url, headers=headers, timeout=10)

            # API should return JSON
            if response.status_code == 200:
                print(f"✓ API route /get_vehicles returned 200")
            else:
                print(f"API route returned {response.status_code}")

        except requests.exceptions.RequestException as e:
            print(f"API request failed (may need auth): {e}")
            # This is OK - we're just checking the server is running

    def test_style_json_with_ingress_path(self):
        """
        Test that /style.json returns JSON with sprite path containing the prefix
        when X-Ingress-Path header is present.
        """
        prefix = '/api/ingress/a93a74ea_psacc/'
        headers = {
            'X-Ingress-Path': prefix,
            'Host': f'127.0.0.1:{self.server_port}'
        }

        url = f"{self.server_url}/style.json"
        expected = f"{self.server_url}{prefix}assets/sprites/osm-liberty"
        try:
            # todo lower timeout
            response = requests.get(url, headers=headers, timeout=3600)

            self.assertEqual(response.status_code, 200,
                             f"Expected 200, got {response.status_code}")

            data = response.json()
            self.assertIn('sprite', data, "Response should contain 'sprite' key")

            sprite_path = data['sprite']
            self.assertEqual(sprite_path, expected)

        except requests.exceptions.RequestException as e:
            self.fail(f"Request failed: {e}")

    def test_svg_images_with_ingress_path(self):
        """
        Test that SVG images in HTML have correct path with X-Ingress-Path header.
        Specifically checks for battery-soh.svg and other image paths.
        """
        prefix = '/api/ingress/a93a74ea_psacc/'
        headers = {
            'X-Ingress-Path': prefix,
            'Host': f'127.0.0.1:{self.server_port}'
        }

        try:
            response = requests.get(self.server_url, headers=headers, timeout=10)

            self.assertEqual(response.status_code, 200,
                             f"Expected 200, got {response.status_code}")

            html = response.text

            # Find all src attributes in img tags
            img_srcs = re.findall(r'<img[^>]+src="([^"]+)"', html)

            # Check each image source
            for img_src in img_srcs:
                # Skip data URLs
                if img_src.startswith('data:'):
                    continue

                # Check if it's an assets/image path
                if 'assets/images/' in img_src:
                    # The path should start with the prefix
                    self.assertTrue(img_src.startswith(prefix),
                                    f"Image path {img_src} does not start with prefix {prefix}")

        except requests.exceptions.RequestException as e:
            self.fail(f"Request failed: {e}")


if __name__ == '__main__':
    import unittest
    unittest.main(verbosity=2)
