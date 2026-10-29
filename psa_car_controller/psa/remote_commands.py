import json
import threading
import time
from collections import OrderedDict
from typing import Dict, List, Optional

from psa_car_controller.psa.mqtt_request import MQTTRequest

# answers to a command arrive within minutes (wakeup -> 901 -> result or 300 timeout),
# keep them a while longer so the status can still be looked up
COMMAND_TTL = 3600
MAX_COMMANDS = 200

# return codes of the mqtt answers (psa/RemoteServices/to/cid/...)
RC_SUCCESS = "0"
RC_TOKEN_EXPIRED = "400"
RC_FAILED = "113"
RC_SERVER_ERROR = "500"
# the server forwarded a wakeup to the car (sms/cellular), the result follows later
RC_WAKEUP_FORWARDED = "901"
# the car didn't answer in time, e.g. its connectivity was disabled after a week without driving
RC_TIMEOUT = "300"
# a 400 with this reason isn't an expired token: the car doesn't offer the service
REASON_NOT_COMPATIBLE = "[authorization.denied.cvs.response.no.matching.service.key]"

# command states: sent -> (woken_up) -> success | failed | timeout | not_compatible | retried
SENT = "sent"
WOKEN_UP = "woken_up"
SUCCESS = "success"
FAILED = "failed"
TIMEOUT = "timeout"
NOT_COMPATIBLE = "not_compatible"
# refused with an expired token and sent again under retried_as
RETRIED = "retried"
FINAL_STATES = frozenset((SUCCESS, FAILED, TIMEOUT, NOT_COMPATIBLE, RETRIED))
STATE_BY_RETURN_CODE = {RC_SUCCESS: SUCCESS, RC_FAILED: FAILED, RC_SERVER_ERROR: FAILED,
                        RC_WAKEUP_FORWARDED: WOKEN_UP, RC_TIMEOUT: TIMEOUT}
# a command without final answer after this long is no longer waited for: the car runs one command
# at a time, but an answer lost (mqtt reconnect, restart) mustn't block the next command for long
PENDING_TIMEOUT = 120


def describe_error(reason, resp_data) -> Optional[str]:
    """Readable cause of a failed answer: its reason, else what resp_data tells about the error."""
    if reason:
        return str(reason)
    if isinstance(resp_data, dict):
        for key in ("error", "error_message", "message", "reason", "status", "code"):
            if resp_data.get(key) not in (None, ""):
                return str(resp_data[key])
        return json.dumps(resp_data, sort_keys=True) if resp_data else None
    if resp_data not in (None, "", []):
        return str(resp_data)
    return None


class RemoteCommand:
    """A command sent to a car and the answers received for it, identified by its correlation_id."""

    def __init__(self, request: MQTTRequest, sent_at: float):
        self.request = request
        self.correlation_id = request.correlation_id
        self.vin = request.vin
        self.name = request.name
        self.retried = request.retried
        self.sent_at = sent_at
        self.status = SENT
        self.retried_as: Optional[str] = None
        self.error: Optional[str] = None
        self.updates: List[dict] = []

    @property
    def final(self) -> bool:
        return self.status in FINAL_STATES

    def is_pending(self, now) -> bool:
        """Still waiting for the car, which runs one command at a time."""
        return not self.final and now - self.sent_at < PENDING_TIMEOUT

    def to_dict(self, now=None) -> dict:
        now = time.time() if now is None else now
        return {"correlation_id": self.correlation_id, "vin": self.vin, "name": self.name,
                "retried": self.retried, "retried_as": self.retried_as, "sent_at": self.sent_at,
                "status": self.status, "final": self.final, "pending": self.is_pending(now),
                "error": self.error, "updates": [dict(update) for update in self.updates]}


class RemoteCommands:
    """Commands sent recently, by correlation_id. Old ones are pruned after COMMAND_TTL seconds."""

    def __init__(self, ttl=COMMAND_TTL, max_commands=MAX_COMMANDS, clock=time.time):
        self.ttl = ttl
        self.max_commands = max_commands
        self.clock = clock
        self._lock = threading.Lock()
        self._commands: Dict[str, RemoteCommand] = OrderedDict()

    def add(self, request: MQTTRequest) -> RemoteCommand:
        with self._lock:
            now = self.clock()
            self._prune(now, room=1)
            command = RemoteCommand(request, now)
            self._commands[command.correlation_id] = command
            return command

    def get(self, correlation_id) -> Optional[RemoteCommand]:
        with self._lock:
            self._prune(self.clock())
            return self._commands.get(correlation_id)

    def answer(self, correlation_id, return_code, reason=None, resp_data=None) -> Optional[RemoteCommand]:
        """Record an answer; returns the command, None when it is unknown (expired, sent by another client).

        Unknown return codes only add an update: the command stays pending until PENDING_TIMEOUT.
        """
        with self._lock:
            command = self._commands.get(correlation_id)
            if command is None:
                return None
            update = {"at": self.clock(), "return_code": return_code, "reason": reason}
            if resp_data is not None:
                update["resp_data"] = resp_data
            command.updates.append(update)
            status = STATE_BY_RETURN_CODE.get(return_code)
            if return_code == RC_TOKEN_EXPIRED and reason == REASON_NOT_COMPATIBLE:
                status = NOT_COMPATIBLE
            if status == WOKEN_UP and command.final:
                status = None  # a late 901 doesn't undo the result
            if status is not None:
                command.status = status
            if status in (FAILED, TIMEOUT, NOT_COMPATIBLE):
                command.error = describe_error(reason, resp_data)
            return command

    def set_status(self, command: RemoteCommand, status, retried_as=None, error=None):
        with self._lock:
            command.status = status
            if retried_as is not None:
                command.retried_as = retried_as
            if error is not None:
                command.error = error

    def to_list(self, vin=None) -> List[dict]:
        """Snapshot of the known commands, oldest first."""
        with self._lock:
            now = self.clock()
            self._prune(now)
            return [command.to_dict(now) for command in self._commands.values()
                    if vin is None or command.vin == vin]

    def get_dict(self, correlation_id) -> Optional[dict]:
        """Snapshot of one command, None when it is unknown or expired."""
        with self._lock:
            now = self.clock()
            self._prune(now)
            command = self._commands.get(correlation_id)
            return None if command is None else command.to_dict(now)

    def pending(self, vin) -> Optional[dict]:
        """Snapshot of the newest command the car hasn't answered yet, None when it is free."""
        with self._lock:
            now = self.clock()
            for command in reversed(self._commands.values()):
                if command.vin == vin and command.is_pending(now):
                    return command.to_dict(now)
            return None

    def _prune(self, now, room=0):
        while self._commands:
            oldest = next(iter(self._commands.values()))
            if now - oldest.sent_at < self.ttl and len(self._commands) + room <= self.max_commands:
                break
            self._commands.pop(oldest.correlation_id)
