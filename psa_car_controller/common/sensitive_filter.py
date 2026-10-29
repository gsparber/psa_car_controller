import logging
import re
from typing import Callable, Iterable, List, Optional

MASK = "***"
# known secrets shorter than this are not masked: too likely to match unrelated text
MIN_SECRET_LENGTH = 6
# keep the last characters of long values so that log lines stay distinguishable (e.g. two cars)
VISIBLE_SUFFIX = 4

# "access_token": "x" / 'refresh_token': 'x' / client_secret=x / password: x
_KEY_VALUE_RE = re.compile(
    r"""(?P<key>["']?[\w-]*(?:token|secret|password)["']?\s*[:=]\s*["']?)"""
    r"""(?!None\b|null\b)(?P<value>[^"'\s,;&})\]]+)""",
    re.IGNORECASE)
_BEARER_RE = re.compile(r"(?P<key>Bearer\s+)(?P<value>[\w\-.~+/]+=*)", re.IGNORECASE)
# 17 chars without I, O, Q, at least one letter and one digit
_VIN_RE = re.compile(r"\b(?=[A-HJ-NPR-Z0-9]*[A-HJ-NPR-Z])(?=[A-HJ-NPR-Z0-9]*\d)[A-HJ-NPR-Z0-9]{17}\b")


def mask_value(value: str) -> str:
    if len(value) >= 3 * VISIBLE_SUFFIX:
        return MASK + value[-VISIBLE_SUFFIX:]
    return MASK


class SensitiveDataFilter(logging.Filter):
    """Masks tokens, secrets, VINs and account ids in log records.

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
        text = _BEARER_RE.sub(lambda m: m.group("key") + MASK, text)
        text = _VIN_RE.sub(lambda m: mask_value(m.group(0)), text)
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
