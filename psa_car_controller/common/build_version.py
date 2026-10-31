"""The name of the running build: its package version, on a dev build the git state given to docker build.

Both values come from outside the code and end up in HTML and in a href, only strict matches are used.
"""
import os
import re

from psa_car_controller import __version__

RELEASES_URL = "https://github.com/flobz/psa_car_controller/releases/tag/"
# pyproject.toml holds 0.0.0, only release.yml sets the real one; unknown means not installed as a package
DEV_VERSIONS = ("0.0.0", "unknown")
# what git describe --tags --always --dirty prints, as long as the tag has no unusual character
GIT_VERSION = re.compile(r"[0-9A-Za-z][0-9A-Za-z._-]{0,99}")
# -dirty doesn't match: the code of that build isn't in the commit, a link would show something else
COMMIT = re.compile(r"(?:.*-\d+-g)?([0-9a-f]{7,40})")
REPO_URL = re.compile(r"https://[0-9A-Za-z.-]+(?:/[0-9A-Za-z._-]+)+")


def git_version():
    value = os.environ.get("PSACC_GIT_VERSION", "")
    return value if GIT_VERSION.fullmatch(value) else None


def _commit_url(version):
    # no default repository: a hash of a fork or a local commit doesn't exist upstream
    repo = os.environ.get("PSACC_GIT_REPO_URL", "")
    commit = COMMIT.fullmatch(version)
    if commit and REPO_URL.fullmatch(repo):
        return f"{repo}/commit/{commit.group(1)}"
    return None


def app_version():
    """The version for the log and --version: only a dev build uses the git state."""
    if __version__ in DEV_VERSIONS:
        return git_version() or __version__
    return __version__


def version_info():
    """Text and page of the running version; a dev build without git state has no page to link to."""
    if __version__ in DEV_VERSIONS:
        version = git_version()
        return (version, _commit_url(version)) if version else ("dev build", None)
    version = "v" + __version__
    return version, RELEASES_URL + version
