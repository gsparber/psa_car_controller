import logging
import re
from typing import Callable, Iterable, List, Optional

MASK = "***"
# known secrets shorter than this are not masked: too likely to match unrelated text
MIN_SECRET_LENGTH = 6
# keep the last characters of long values so that log lines stay distinguishable (e.g. two cars)
VISIBLE_SUFFIX = 4

# "access_token": "x" / 'refresh_token': 'x' / client_secret=x / password: x / vehicleId=x
# the quotes can be escaped: a dict turned to str and logged inside a list or a json document
_KEY_VALUE_RE = re.compile(
    r"""(?P<key>\\?["']?[\w-]*(?:token|secret|password|vehicle[_-]?id|api[_-]?key|appid)\\?["']?\s*[:=]\s*\\?["']?)"""
    r"""(?!None\b|null\b)(?P<value>(?:(?!\\["'])[^"'\s,;&})\]])+)""",
    re.IGNORECASE)
# an exception of requests holds the url with its query: position (tlm, lat, lon), login (jsonRequest), OTP (sid, code).
# The values are url-encoded, the pattern above can't look inside
_QUERY_PARAM_RE = re.compile(r"""(?P<key>[?&](?:tlm|lat|lon|locations|jsonRequest|sid|code|macid)=)[^&\s'"()]+""",
                             re.IGNORECASE)
_VIN_KEY_RE = re.compile(r"""\\?["']vin\\?["']\s*:""", re.IGNORECASE)
_ID_KEY_RE = re.compile(r"""(?P<key>\\?["']id\\?["']\s*:\s*\\?["'])(?P<value>(?:(?!\\["'])[^"'])+)""", re.IGNORECASE)
_BEARER_RE = re.compile(r"(?P<key>Bearer\s+)(?P<value>[\w\-.~+/]+=*)", re.IGNORECASE)
# 17 chars without I, O, Q, at least one letter and one digit
_VIN_RE = re.compile(r"\b(?=[A-HJ-NPR-Z0-9]*[A-HJ-NPR-Z])(?=[A-HJ-NPR-Z0-9]*\d)[A-HJ-NPR-Z0-9]{17}\b")
# the rights of a car list its services under the serial number of each telematic unit: "0D1B...": [
_UNIT_KEY_RE = re.compile(r"""(?P<quote>\\?["'])(?P<value>[0-9A-Fa-f]{16,})(?P<end>\\?["']\s*:\s*\[)""")


def mask_value(value: str) -> str:
    if len(value) >= 3 * VISIBLE_SUFFIX:
        return MASK + value[-VISIBLE_SUFFIX:]
    return MASK


def _object_start(text: str, position: int) -> Optional[int]:
    depth = 0
    for index in range(position - 1, -1, -1):
        if text[index] == "}":
            depth += 1
        elif text[index] == "{":
            if depth == 0:
                return index
            depth -= 1
    return None


def _object_end(text: str, position: int) -> int:
    depth = 0
    for index in range(position, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            if depth == 0:
                return index + 1
            depth -= 1
    return len(text)


def _mask_object_id(obj: str) -> str:
    def mask_top_level(match: "re.Match") -> str:
        before = obj[:match.start()]
        if before.count("{") - before.count("}") == 1:
            return match.group("key") + MASK
        return match.group(0)

    return _ID_KEY_RE.sub(mask_top_level, obj)


def _mask_vehicle_ids(text: str) -> str:
    """Masks the "id" next to a "vin" (the vehicles of the API): any other "id" says nothing about the owner."""
    # from the end, because masking moves everything behind it
    for vin_key in reversed(list(_VIN_KEY_RE.finditer(text))):
        start = _object_start(text, vin_key.start())
        if start is not None:
            end = _object_end(text, vin_key.end())
            text = text[:start] + _mask_object_id(text[start:end]) + text[end:]
    return text


class SensitiveDataFilter(logging.Filter):
    """Masks tokens, secrets, VINs, vehicle ids and account ids in log records.

    Must be attached to handlers: filters on a logger don't see records propagated from child loggers.
    Sources are callables returning the current secrets, so refreshed tokens are masked without re-registering.
    """

    def __init__(self):
        super().__init__()
        self._sources: List[Callable[[], Iterable[Optional[str]]]] = []

    def add_source(self, source: Callable[[], Iterable[Optional[str]]]):
        self._sources.append(source)

    def clear_sources(self):
        self._sources = []

    def _secrets(self) -> List[str]:
        secrets = set()
        for source in self._sources:
            try:
                values = source()
            except Exception:  # pylint: disable=broad-except
                continue
            for value in values:
                if isinstance(value, str) and len(value) >= MIN_SECRET_LENGTH:
                    secrets.add(value)
        # longest first so that a secret containing another one is masked as a whole
        return sorted(secrets, key=len, reverse=True)

    def mask(self, text: str) -> str:
        for secret in self._secrets():
            if secret in text:
                text = text.replace(secret, mask_value(secret))
        text = _KEY_VALUE_RE.sub(lambda m: m.group("key") + MASK, text)
        text = _QUERY_PARAM_RE.sub(lambda m: m.group("key") + MASK, text)
        text = _mask_vehicle_ids(text)
        text = _BEARER_RE.sub(lambda m: m.group("key") + MASK, text)
        text = _VIN_RE.sub(lambda m: mask_value(m.group(0)), text)
        text = _UNIT_KEY_RE.sub(lambda m: m.group("quote") + mask_value(m.group("value")) + m.group("end"), text)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pylint: disable=broad-except
            return True
        record.msg = self.mask(message)
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = self.mask(record.exc_text)
        return True


sensitive_filter = SensitiveDataFilter()
