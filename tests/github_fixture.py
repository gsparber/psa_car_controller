"""GitHub and a small application archive without network, to test the initial setup."""
import bz2
import io
import json
import zipfile
from datetime import datetime, timedelta, timezone
from hashlib import sha1
from unittest.mock import MagicMock

import requests
from androguard.core.apk import FileNotPresent
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import BestAvailableEncryption, pkcs12

TREE_URL = "https://api.github.com/repos/flobz/psa_apk/git/trees/main:"
RAW_URL = "https://github.com/flobz/psa_apk/raw/main/"


def response(status_code=200, content=b"", headers=None) -> requests.Response:
    res = requests.Response()
    res.status_code = status_code
    res._content = content
    res._content_consumed = True
    res.headers.update(headers or {})
    return res


def rate_limit_response() -> requests.Response:
    body = {"message": "API rate limit exceeded for 203.0.113.7. (But here's the good news: ...)"}
    return response(403, json.dumps(body).encode(), {"X-RateLimit-Remaining": "0"})


def git_blob_sha(data: bytes) -> str:
    return sha1(f"blob {len(data)}\0".encode() + data).hexdigest()


class FakeGithub:
    """Replacement for requests.get. An unknown URL fails the test, so nothing reaches the network."""

    def __init__(self, files=None, tree_answer=None):
        self.files = files or {}
        self.tree_answer = tree_answer  # response or exception of the API, it answers normally by default
        self.urls = []

    def get(self, url, **_):
        self.urls.append(url)
        if url == TREE_URL:
            if isinstance(self.tree_answer, Exception):
                raise self.tree_answer
            if self.tree_answer is not None:
                return self.tree_answer
            tree = [{"path": name, "sha": git_blob_sha(data)} for name, data in self.files.items()]
            return response(content=json.dumps({"tree": tree}).encode())
        if url.startswith(RAW_URL):
            name = url[len(RAW_URL):]
            return response(content=self.files[name]) if name in self.files else response(404)
        raise AssertionError(f"unexpected request: {url}")


def _pkcs12_certificate(password: bytes) -> bytes:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "test")])
    now = datetime.now(timezone.utc)
    certificate = x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()) \
        .serial_number(1).not_valid_before(now).not_valid_after(now + timedelta(days=1)).sign(key, hashes.SHA256())
    return pkcs12.serialize_key_and_certificates(b"test", key, certificate, None, BestAvailableEncryption(password))


def build_apk_archive() -> bytes:
    """.apk.bz2 with the files ApkParser reads. Its manifest and resources need FakeApk, see test_parse_real_apk."""
    apk = io.BytesIO()
    with zipfile.ZipFile(apk, "w") as z:
        # like the real applications, IL has no parameters.json
        z.writestr("res/raw/cultures.json", json.dumps({"FR": {"languages": ["fr_FR", "en_FR"]},
                                                        "IL": {"languages": ["he_IL"]}}))
        z.writestr("res/raw-fr-rFR/parameters.json", json.dumps({"cvsClientId": "client-id",
                                                                 "cvsSecret": "client-secret"}))
        z.writestr("assets/MWPMYMA1.pfx", _pkcs12_certificate(b"y5Y2my5B"))
    return bz2.compress(apk.getvalue())


class FakeApk:
    """Replacement for androguard's APK: the binary manifest and resources.arsc aren't worth encoding by hand."""
    androidversion = {"Name": "1.2.3"}

    def __init__(self, filename):
        self.filename = filename

    def get_package(self):
        return "com.psa.mym.mypeugeot"

    def get_android_resources(self):
        resources = MagicMock()
        resources.get_string.return_value = ("HOST_BRANDID_PROD", "https://brandid.example")
        return resources

    def get_file(self, name):
        with zipfile.ZipFile(self.filename) as z:
            try:
                return z.read(name)
            except KeyError as e:
                raise FileNotPresent(name) from e
