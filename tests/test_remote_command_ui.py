"""The mobile UI follows a command: sent -> car woken up -> done/timeout/failed (T34)."""
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

from psa_car_controller.psa.RemoteClient import wakeup_limiter
from psa_car_controller.psa.mqtt_request import MQTTRequest
from psa_car_controller.psa.remote_commands import RemoteCommands, PENDING_TIMEOUT
from tests.test_remote_command_answers import CUSTOMER_ID, published
from tests.test_remote_commands import get_ready_remote_client, VIN
from tests.utils import get_web_view


def get_car_access():
    return get_web_view("car_access")


def text(component):
    if isinstance(component, (list, tuple)):
        return " ".join(text(child) for child in component)
    if isinstance(component, str):
        return component
    children = getattr(component, "children", None)
    return text(children) if children is not None else ""


class FakeClock:
    now = 1000.0

    def __call__(self):
        return self.now


class TestCommandStatusLine(unittest.TestCase):

    def setUp(self):
        self.car_access = get_car_access()
        self.clock = FakeClock()
        self.commands = RemoteCommands(clock=self.clock)
        self.command = self.commands.add(MQTTRequest("/Doors", VIN, {"action": "lock"}, CUSTOMER_ID))

    def render(self):
        command = self.car_access.current_command(self.commands, self.command.correlation_id)
        children, finished_at = self.car_access.render_command(command, "Lock")
        return text(children), finished_at

    def test_steps(self):
        line, finished_at = self.render()
        self.assertEqual((["Lock", "Sent"], None), (line.split(), finished_at))

        self.commands.answer(self.command.correlation_id, "901")
        line, _ = self.render()
        self.assertIn("Car woken up", line)

        self.clock.now += 30
        self.commands.answer(self.command.correlation_id, "0")
        line, finished_at = self.render()
        self.assertIn("Car woken up", line)
        self.assertIn("Done", line)
        self.assertEqual(self.clock.now, finished_at)

    def test_timeout_and_failure(self):
        self.commands.answer(self.command.correlation_id, "300")
        self.assertIn("Timeout", self.render()[0])
        self.commands.answer(self.command.correlation_id, "113", resp_data={"error": "door open"})
        self.assertIn("Failed: door open", self.render()[0])

    def test_no_answer(self):
        self.clock.now += PENDING_TIMEOUT
        line, finished_at = self.render()
        self.assertIn("No answer from the car", line)
        self.assertEqual(self.command.sent_at + PENDING_TIMEOUT, finished_at)

    def test_follows_the_command_sent_again(self):
        retry = self.commands.add(self.command.request.retry())
        self.commands.set_status(self.command, "retried", retried_as=retry.correlation_id)
        self.commands.answer(retry.correlation_id, "0")
        self.assertIn("Done", self.render()[0])


class TestOneCommandAtATime(unittest.TestCase):

    def setUp(self):
        self.car_access = get_car_access()
        self.rc = get_ready_remote_client()
        self.rc.remote_token_last_update = datetime(2000, 1, 1)
        wakeup_limiter.reset()
        self.car = self.rc.vehicles_list[0]
        self.patch = patch.object(self.car_access, "app", return_value=MagicMock(myp=MagicMock(remote_client=self.rc)))
        self.patch.start()

    def tearDown(self):
        self.patch.stop()

    def test_command_returns_its_id(self):
        message, ok, _, correlation_id = self.car_access.send_command("refresh", self.car)
        [(_, wakeup)] = published(self.rc)
        self.assertTrue(ok, message)
        self.assertEqual(wakeup["correlation_id"], correlation_id)

    def test_waits_for_the_pending_command(self):
        self.rc.horn(VIN, 1)
        self.rc.mqtt_client.publish.reset_mock()

        message, ok, _, correlation_id = self.car_access.send_command("refresh", self.car)

        self.assertFalse(ok)
        self.assertIsNone(correlation_id)
        self.assertIn("Horn", message)
        self.rc.mqtt_client.publish.assert_not_called()


if __name__ == '__main__':
    unittest.main()
