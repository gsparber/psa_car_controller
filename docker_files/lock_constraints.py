"""Prints constraints for pip: what poetry.lock has, without what the system already installed.

The wheel only has ranges, so an image would install the newest versions, which CI never tested. The packages of Debian
stay: pip can't replace most of them, and numpy, pandas and scipy have no wheels for armv7.
"""
import re
import sys
import tomllib
from importlib.metadata import distributions


def normalize(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def constraints(lock, installed):
    versions = {}
    for package in lock["package"]:
        if "main" in package.get("groups", ["main"]):
            versions.setdefault(normalize(package["name"]), set()).add(package["version"])
    # two versions depend on markers, which a constraint can't choose between
    return [f"{name}=={next(iter(found))}" for name, found in sorted(versions.items())
            if len(found) == 1 and name not in installed]


def main(path):
    with open(path, "rb") as file:
        lock = tomllib.load(file)
    installed = {normalize(dist.metadata["Name"]) for dist in distributions()}
    print("\n".join(constraints(lock, installed)))


if __name__ == "__main__":
    main(sys.argv[1])
