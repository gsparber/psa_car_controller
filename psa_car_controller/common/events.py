"""Short entries of what happened with the cars, for the log page: status requests to PSA, MQTT, remote commands,
charge control and what users asked for.

In memory only, the last MAX_EVENTS: nothing more is written to the SD card of a Raspberry Pi. They go to the log
file too, but at debug level, as the log has its own lines for most of them.
"""
import logging
import threading
from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Iterable, List, Optional

from psa_car_controller.common.sensitive_filter import mask_value, sensitive_filter

logger = logging.getLogger(__name__)

API, MQTT, COMMAND, CHARGE_CONTROL, USER = "api", "mqtt", "command", "charge_control", "user"
CATEGORIES = {API: "PSA API", MQTT: "MQTT", COMMAND: "Commands", CHARGE_CONTROL: "Charge control", USER: "User"}
MAX_EVENTS = 500


@dataclass(frozen=True)
class Event:
    at: datetime
    category: str
    # INFO, WARNING or ERROR
    level: str
    message: str
    # masked
    vin: Optional[str] = None
    # how many times the same happened in a row, from first_at to at
    count: int = 1
    first_at: Optional[datetime] = None
    # what makes it the same as the event before, see EventLog.record
    same_as: Any = None


class EventLog:
    def __init__(self, size=MAX_EVENTS):
        self._events = deque(maxlen=size)
        self._lock = threading.Lock()

    # pylint: disable-next=too-many-arguments
    def record(self, category: str, message: str, *args, vin: Optional[str] = None, level=logging.INFO,
               same_as=None) -> Event:
        """message is formatted with args as a log message; secrets and VINs in it are masked.

        same_as: when the last event of the category and car has the same, nothing new happened: that event is
        counted again instead of adding one (a parked car answers the same every refresh)."""
        try:
            text = message % args if args else message
        except (TypeError, ValueError):
            text = " ".join([message, *map(str, args)])
        now = datetime.now(timezone.utc)
        event = Event(now, category, logging.getLevelName(level), sensitive_filter.mask(text),
                      mask_value(vin) if vin else None, same_as=same_as)
        with self._lock:
            index = self._last_index(category, event.vin) if same_as is not None else None
            if index is not None and self._events[index].same_as == same_as:
                last = self._events[index]
                del self._events[index]
                event = replace(event, count=last.count + 1, first_at=last.first_at or last.at)
            self._events.append(event)
        logger.debug("[%s] %s%s", category, f"{event.vin}: " if event.vin else "", event.message)
        return event

    def _last_index(self, category, vin) -> Optional[int]:
        for index in range(len(self._events) - 1, -1, -1):
            if self._events[index].category == category and self._events[index].vin == vin:
                return index
        return None

    def entries(self, categories: Optional[Iterable[str]] = None) -> List[Event]:
        """Newest first."""
        with self._lock:
            events = list(self._events)
        if categories is not None:
            categories = set(categories)
            events = [event for event in events if event.category in categories]
        return events[::-1]

    def clear(self):
        with self._lock:
            self._events.clear()


EVENTS = EventLog()


def record(category: str, message: str, *args, vin: Optional[str] = None, level=logging.INFO, same_as=None) -> Event:
    return EVENTS.record(category, message, *args, vin=vin, level=level, same_as=same_as)


def as_text(events: List[Event]) -> str:
    """The events as lines, oldest first, for the download."""
    lines = []
    for event in reversed(events):
        when = f"{event.at.astimezone():%Y-%m-%d %H:%M:%S}"
        if event.count > 1:
            when = f"{event.first_at.astimezone():%Y-%m-%d %H:%M:%S} – {event.at.astimezone():%H:%M:%S}"
        repeated = f" ({event.count}x the same)" if event.count > 1 else ""
        lines.append(f"{when} {event.level} [{CATEGORIES.get(event.category, event.category)}] "
                     f"{event.vin + ': ' if event.vin else ''}{event.message}{repeated}")
    return "\n".join(lines) + "\n"
