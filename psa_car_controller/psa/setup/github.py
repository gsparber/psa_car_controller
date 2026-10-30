import bz2
import logging
import os
from hashlib import sha1
from os import path

import requests

logger = logging.getLogger(__name__)
TIMEOUT_IN_S = 20
TOKEN_ENV = "PSACC_GITHUB_TOKEN"


class GithubError(ConnectionError):
    """GitHub didn't answer as expected. The message is meant for the user, the web setup displays it."""


def _describe_refusal(res) -> str:
    # the body isn't reused in the message: GitHub writes the IP address of the user in its rate limit answer
    if res.status_code in (403, 429) and (res.headers.get("X-RateLimit-Remaining") == "0"
                                          or "rate limit" in res.text.lower()):
        return "the GitHub API rate limit is reached (60 requests per hour for an IP address)"
    return f"GitHub answered with HTTP {res.status_code}"


def _api_headers() -> dict:
    # optional, for the nightly CI run: runners share their IP address and so the limit of 60 requests per hour
    token = os.environ.get(TOKEN_ENV, "").strip()
    return {"Authorization": f"Bearer {token}"} if token else {}


def get_github_sha_from_file(user, repo, directory, filename):
    try:
        res = requests.get("https://api.github.com/repos/{}/{}/git/trees/main:{}".format(user, repo, directory),
                           headers=_api_headers(),
                           timeout=TIMEOUT_IN_S)
    except requests.exceptions.InvalidHeader:
        # requests writes the value of the header in this exception: it's neither logged nor chained
        raise GithubError(f"{TOKEN_ENV} isn't a valid token") from None
    except requests.RequestException as e:
        logger.debug("can't reach the GitHub API", exc_info=True)
        raise GithubError("GitHub can't be reached") from e
    if res.status_code != 200:
        raise GithubError(_describe_refusal(res))
    try:
        return next(file["sha"] for file in res.json()["tree"] if file["path"] == filename)
    except (ValueError, KeyError, TypeError, StopIteration) as e:
        raise GithubError(f"GitHub doesn't list {filename}") from e


def github_file_need_to_be_downloaded(user, repo, directory, filename):
    try:
        with open(filename, 'rb') as file_for_hash:
            data = file_for_hash.read()
            filesize = len(data)
        prefix = "blob " + str(filesize) + "\0"
        sha_of_downloaded_file = sha1(prefix.encode("utf-8") + data).hexdigest()
        sha_of_git_file = get_github_sha_from_file(user, repo, directory, filename)
        if sha_of_downloaded_file == sha_of_git_file:
            logger.debug("locale file is the latest version")
            return False
        logger.debug("download last version of file")
    except FileNotFoundError:
        logger.debug("File not found, download file")
    return True


def _download(url, archive_name):
    try:
        r = requests.get(url,
                         headers={
                             "Accept": "application/vnd.github.VERSION.raw"
                         },
                         stream=True,
                         timeout=TIMEOUT_IN_S
                         )
        r.raise_for_status()
        # opened only now, a refused download must not erase the previous archive
        with open(archive_name, 'wb') as f:
            for chunk in r.iter_content(1024):
                f.write(chunk)
    except requests.RequestException as e:
        logger.debug("can't download %s", url, exc_info=True)
        raise GithubError(f"Can't download {archive_name} from GitHub. "
                          "Please check the internet connection and try again later.") from e


def urlretrieve_from_github(user, repo, directory, filename, branch="main"):
    archive_name = filename + ".bz2"
    try:
        need_download = github_file_need_to_be_downloaded(user, repo, directory, archive_name)
    except GithubError as e:
        # This check is only there to get a newer version. Downloads aren't counted by the API rate limit.
        if path.isfile(filename):
            logger.warning("Can't check if %s is the latest version, using the local file: %s", filename, e)
            return
        logger.warning("Can't check the latest version of %s, downloading it: %s", filename, e)
        need_download = True
    if need_download or not path.isfile(filename):
        _download("https://github.com/{}/{}/raw/{}/{}{}".format(user, repo, branch, directory, archive_name),
                  archive_name)
        with bz2.BZ2File(archive_name, 'rb') as file, open(filename, 'wb') as out_file:
            out_file.write(file.read())
