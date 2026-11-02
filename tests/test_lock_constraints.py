import importlib.util
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
SPEC = importlib.util.spec_from_file_location("lock_constraints", ROOT / "docker_files" / "lock_constraints.py")
lock_constraints = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(lock_constraints)


def package(name, version, groups=("main",)):
    return {"name": name, "version": version, "groups": list(groups)}


class TestLockConstraints(unittest.TestCase):
    def test_pins_the_packages_of_the_app(self):
        lock = {"package": [package("Dash", "4.5.0"), package("ruamel.yaml_clib", "1.0")]}
        self.assertEqual(["dash==4.5.0", "ruamel-yaml-clib==1.0"], lock_constraints.constraints(lock, set()))

    def test_leaves_what_the_system_installed(self):
        lock = {"package": [package("numpy", "2.5.4"), package("dash", "4.5.0")]}
        self.assertEqual(["dash==4.5.0"], lock_constraints.constraints(lock, {"numpy"}))

    def test_leaves_the_tools_of_development(self):
        lock = {"package": [package("pylint", "4.1.3", ["dev"]), package("astroid", "4.0", ["dev", "main"])]}
        self.assertEqual(["astroid==4.0"], lock_constraints.constraints(lock, set()))

    def test_leaves_a_package_with_two_versions(self):
        lock = {"package": [package("numpy", "2.4.6"), package("numpy", "2.5.4")]}
        self.assertEqual([], lock_constraints.constraints(lock, set()))

    def test_the_lock_file_of_the_project(self):
        lock = tomllib.loads((ROOT / "poetry.lock").read_text(encoding="utf-8"))
        pins = lock_constraints.constraints(lock, set())
        self.assertTrue(any(pin.startswith("dash==") for pin in pins))
        self.assertFalse(any(pin.startswith("pylint==") for pin in pins))
