import logging
import threading
import time
from typing import Callable, Dict

from psa_car_controller.psa.remote_commands import RemoteCommand, SUCCESS

logger = logging.getLogger(__name__)

# the car reports its new state to the api a few seconds after it answered the command
COMMAND_REFRESH_DELAY = 10
# a burst of commands to one car fetches its status once: PSA blocks accounts which poll too often
COMMAND_REFRESH_MIN_INTERVAL = 60


class CommandStatusRefresh:
    """Fetch a car's status COMMAND_REFRESH_DELAY seconds after a command to it succeeded.

    Without it the new state (doors locked, charge started, ...) shows up at the next regular
    refresh only, up to info_refresh_rate later. Skipped while PSA rate limits the account.
    """

    def __init__(self, refresh: Callable[[str], object], rate_limited: Callable[[], bool],
                 delay=COMMAND_REFRESH_DELAY, min_interval=COMMAND_REFRESH_MIN_INTERVAL, clock=time.monotonic):
        # pylint: disable=too-many-arguments,too-many-positional-arguments
        self.refresh = refresh
        self.rate_limited = rate_limited
        self.delay = delay
        self.min_interval = min_interval
        self.clock = clock
        self._lock = threading.Lock()
        self._timers: Dict[str, threading.Timer] = {}
        self._last_refresh: Dict[str, float] = {}
        self._stopped = False

    def on_command(self, command: RemoteCommand):
        if command.status == SUCCESS:
            self.schedule(command.vin)

    def schedule(self, vin):
        with self._lock:
            if self._stopped or vin in self._timers:
                return
            if self.rate_limited():
                logger.debug("no status refresh of %s after a command: rate limited", vin)
                return
            delay = self.delay
            last = self._last_refresh.get(vin)
            if last is not None:
                delay = max(delay, last + self.min_interval - self.clock())
            timer = threading.Timer(delay, self._run, args=[vin])
            timer.daemon = True
            self._timers[vin] = timer
            timer.start()

    def _run(self, vin):
        with self._lock:
            self._timers.pop(vin, None)
            if self._stopped or self.rate_limited():
                return
            self._last_refresh[vin] = self.clock()
        logger.debug("refresh the status of %s after a command", vin)
        try:
            self.refresh(vin)
        except Exception:  # pylint: disable=broad-except
            logger.warning("status refresh of %s after a command failed", vin, exc_info=True)

    def stop(self):
        with self._lock:
            self._stopped = True
            timers = list(self._timers.values())
            self._timers.clear()
        for timer in timers:
            timer.cancel()
