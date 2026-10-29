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
# a 400 with this reason isn't an expired token: the car doesn't offer the service
REASON_NOT_COMPATIBLE = "[authorization.denied.cvs.response.no.matching.service.key]"

# command states; intermediate answers (e.g. 901 wakeup forwarded) only add an update
SENT = "sent"
SUCCESS = "success"
FAILED = "failed"
NOT_COMPATIBLE = "not_compatible"
RETRIED = "retried"


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
        self.updates: List[dict] = []

    def to_dict(self) -> dict:
        return {"correlation_id": self.correlation_id, "vin": self.vin, "name": self.name,
                "retried": self.retried, "retried_as": self.retried_as, "sent_at": self.sent_at,
                "status": self.status, "updates": [dict(update) for update in self.updates]}


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

    def answer(self, correlation_id, return_code, reason=None) -> Optional[RemoteCommand]:
        """Record an answer; returns the command, None when it is unknown (expired, sent by another client)."""
        with self._lock:
            command = self._commands.get(correlation_id)
            if command is None:
                return None
            command.updates.append({"at": self.clock(), "return_code": return_code, "reason": reason})
            if return_code == RC_SUCCESS:
                command.status = SUCCESS
            elif return_code == RC_FAILED:
                command.status = FAILED
            elif return_code == RC_TOKEN_EXPIRED and reason == REASON_NOT_COMPATIBLE:
                command.status = NOT_COMPATIBLE
            return command

    def set_status(self, command: RemoteCommand, status, retried_as=None):
        with self._lock:
            command.status = status
            if retried_as is not None:
                command.retried_as = retried_as

    def to_list(self, vin=None) -> List[dict]:
        """Snapshot of the known commands, oldest first."""
        with self._lock:
            self._prune(self.clock())
            return [command.to_dict() for command in self._commands.values() if vin is None or command.vin == vin]

    def _prune(self, now, room=0):
        while self._commands:
            oldest = next(iter(self._commands.values()))
            if now - oldest.sent_at < self.ttl and len(self._commands) + room <= self.max_commands:
                break
            self._commands.pop(oldest.correlation_id)
