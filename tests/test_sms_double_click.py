"""Every SMS counts for the OTP limit of PSA: a double click on "Send SMS" must not send two of them."""
import threading
import unittest
from unittest.mock import MagicMock, patch

from psa_car_controller.psa.RemoteClient import SmsRequestException
from tests.test_unclicked_callbacks import callback_of
from tests.utils import get_web_view


def text(alert):
    return str(alert.children)


class TestSmsCooldown(unittest.TestCase):

    def setUp(self):
        self.views = get_web_view("config_views")
        app_patch = patch.object(self.views, "app", MagicMock())
        self.get_sms = app_patch.start().myp.remote_client.get_sms_otp_code
        self.addCleanup(app_patch.stop)
        cooldown_patch = patch.object(self.views, "SMS_COOLDOWN", self.views.Cooldown(30))
        self.cooldown = cooldown_patch.start()
        self.addCleanup(cooldown_patch.stop)

    def test_second_click_sends_nothing(self):
        self.assertIn("SMS sent", text(self.views.askCode(1)))
        answer = self.views.askCode(2)
        self.get_sms.assert_called_once_with()
        self.assertEqual("info", answer.color)
        self.assertRegex(text(answer), r"another one in \d+ s")

    def test_two_requests_at_the_same_time(self):
        """Since the server is threaded a double click can really run in parallel."""
        started, finish = threading.Event(), threading.Event()

        def slow_sms():
            started.set()
            self.assertTrue(finish.wait(10))

        self.get_sms.side_effect = slow_sms
        first = threading.Thread(target=self.views.askCode, args=(1,))
        first.start()
        self.addCleanup(first.join)
        self.addCleanup(finish.set)
        self.assertTrue(started.wait(10))
        self.assertEqual("info", self.views.askCode(2).color)
        finish.set()
        first.join(10)
        self.get_sms.assert_called_once_with()

    def test_again_after_the_cooldown(self):
        self.views.askCode(1)
        with patch.object(self.views.time, "monotonic", return_value=self.views.time.monotonic() + 31):
            self.assertIn("SMS sent", text(self.views.askCode(2)))
        self.assertEqual(2, self.get_sms.call_count)

    def test_a_failure_can_be_retried_at_once(self):
        """The error says to try again, PSA has sent nothing."""
        for error in (SmsRequestException("PSA couldn't send the SMS", "HTTP 500"), ValueError("broken")):
            with self.subTest(error=error):
                self.get_sms.reset_mock()
                self.get_sms.side_effect = [error, None]
                self.views.askCode(1)
                self.assertIn("SMS sent", text(self.views.askCode(2)))
                self.assertEqual(2, self.get_sms.call_count)
                self.cooldown.release()

    def test_button_is_disabled_while_it_runs(self):
        running = callback_of("sms-demand-result.children")["running"]
        self.assertEqual({"ask-sms.disabled": True}, running["running"])
        self.assertEqual({"ask-sms.disabled": False}, running["runningOff"])
