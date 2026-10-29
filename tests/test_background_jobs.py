import json
import logging
import threading
import unittest
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from dash import dcc, no_update

from psa_car_controller.psa.otp.otp import ActivationException
from psa_car_controller.psa.setup.headless_oauth import HeadlessOAuthError
from psa_car_controller.web.tools.jobs import DONE, FAILED, RUNNING, JobBusy, JobFailure, JobRegistry, describe_error
from tests.test_worker_oauth import text_of
from tests.utils import get_web_view, isolate_jobs, wait_for_job

KIND = "login"
OPEL = "com.psa.mym.myopel"
EMAIL = "someone@example.com"
PASSWORD = "S3cret pass "
SMS_CODE = "918273"
PIN = "4711"
# what poll_job returns
STORE, POLL_DISABLED, POLL_MAX, BUTTON_DISABLED, STATUS, RESULT = range(6)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class TestRegistry(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.jobs = JobRegistry(ttl_s=100, max_finished=3, clock=self.clock)
        self.release = threading.Event()
        self.addCleanup(self.release.set)

    def blocked(self, report_step):
        report_step("second step")
        self.release.wait(10)
        return "finished", {"some": "result"}

    def finished_job(self, kind=KIND):
        job_id, _ = self.jobs.start(kind, lambda _: ("finished", None))
        wait_for_job(kind, self.jobs)
        return job_id

    def test_start_does_not_wait_for_the_work(self):
        job_id, created = self.jobs.start(KIND, self.blocked, step="first step")
        self.assertTrue(created)
        self.assertIn(self.jobs.status(job_id).status, ("pending", RUNNING))
        self.release.set()
        wait_for_job(KIND, self.jobs)
        state = self.jobs.status(job_id)
        self.assertEqual((DONE, "finished", {"some": "result"}), (state.status, state.message, state.result))

    def test_ids_cannot_be_guessed(self):
        first = self.finished_job()
        second = self.finished_job()
        self.assertNotEqual(first, second)
        self.assertGreaterEqual(len(first), 40)

    def test_second_start_follows_the_running_job(self):
        calls = []

        def work(report_step):
            calls.append(1)
            return self.blocked(report_step)

        first, _ = self.jobs.start(KIND, work)
        self.assertEqual((None, False), self.jobs.start(KIND, work))
        self.release.set()
        wait_for_job(KIND, self.jobs)
        self.assertEqual(1, len(calls))
        self.assertEqual(DONE, self.jobs.status(first).status)

    def test_other_kinds_run_side_by_side(self):
        self.jobs.start(KIND, self.blocked)
        self.assertTrue(self.jobs.start("otp", self.blocked)[1])

    def test_next_start_works_after_a_failure(self):
        job_id, _ = self.jobs.start(KIND, MagicMock(side_effect=JobFailure("no answer")))
        wait_for_job(KIND, self.jobs)
        self.assertEqual((FAILED, "no answer"), (self.jobs.status(job_id).status, self.jobs.status(job_id).message))
        self.assertTrue(self.jobs.start(KIND, lambda _: ("ok", None))[1])

    def test_steps_are_visible_while_it_runs(self):
        reported = threading.Event()

        def work(report_step):
            report_step("second step")
            reported.set()
            self.release.wait(10)
            return "", None

        self.jobs.start(KIND, work, step="first step")
        self.assertTrue(reported.wait(10))
        self.assertEqual("second step", self.jobs.active(KIND).step)

    def test_finished_jobs_are_pruned_after_the_ttl(self):
        job_id = self.finished_job()
        self.clock.now += 100
        self.assertIsNotNone(self.jobs.status(job_id))
        self.clock.now += 1
        self.assertIsNone(self.jobs.status(job_id))
        self.assertIsNone(self.jobs.latest(KIND))

    def test_number_of_finished_jobs_is_limited(self):
        ids = []
        for _ in range(5):
            ids.append(self.finished_job())
            self.clock.now += 1
        self.assertEqual([None, None], [self.jobs.status(job_id) for job_id in ids[:2]])
        self.assertTrue(all(self.jobs.status(job_id) for job_id in ids[2:]))

    def test_running_job_outlives_the_ttl(self):
        job_id, _ = self.jobs.start(KIND, self.blocked, timeout_s=10000)
        self.clock.now += 1000
        self.assertTrue(self.jobs.status(job_id).active)

    def test_timeout_fails_the_job(self):
        job_id, _ = self.jobs.start(KIND, self.blocked, timeout_s=10, timeout_message="took too long")
        self.clock.now += 11
        state = self.jobs.status(job_id)
        self.assertEqual((FAILED, "took too long"), (state.status, state.message))
        self.assertIsNone(self.jobs.active(KIND))

    def test_late_result_does_not_change_a_timeout(self):
        job_id, _ = self.jobs.start(KIND, self.blocked, timeout_s=10, timeout_message="took too long")
        self.clock.now += 11
        self.assertEqual(FAILED, self.jobs.status(job_id).status)
        thread = next(job.thread for job in self.jobs._jobs.values())  # pylint: disable=protected-access
        self.release.set()
        thread.join(10)
        self.assertEqual("took too long", self.jobs.status(job_id).message)

    def test_no_second_job_while_a_given_up_one_still_works(self):
        self.jobs.start(KIND, self.blocked, timeout_s=10)
        self.clock.now += 11
        with self.assertRaises(JobBusy):
            self.jobs.start(KIND, self.blocked)
        thread = next(job.thread for job in self.jobs._jobs.values())  # pylint: disable=protected-access
        self.release.set()
        thread.join(10)
        self.assertTrue(self.jobs.start(KIND, lambda _: ("ok", None))[1])

    def test_job_leaving_its_thread_does_not_block_the_next_one(self):
        # regression: the status is set just before the thread ends, a start at that moment was refused
        leaving = threading.Event()
        job_id, _ = self.jobs.start(KIND, lambda _: ("ok", None))
        wait_for_job(KIND, self.jobs)
        job = self.jobs._jobs[job_id]  # pylint: disable=protected-access
        job.thread.join(10)
        job.thread = threading.Thread(target=leaving.wait, args=(10,), daemon=True)
        job.thread.start()
        self.addCleanup(leaving.set)
        self.assertTrue(self.jobs.start(KIND, lambda _: ("ok", None))[1])

    def test_thread_that_cannot_start_is_a_failure(self):
        with patch("threading.Thread.start", side_effect=RuntimeError("can't start new thread")):
            job_id, created = self.jobs.start(KIND, self.blocked)
        self.assertTrue(created)
        self.assertEqual(FAILED, self.jobs.status(job_id).status)
        self.assertNotIn("new thread", self.jobs.status(job_id).message)
        self.assertTrue(self.jobs.start(KIND, lambda _: ("ok", None))[1])

    def test_unknown_or_forged_ids(self):
        for job_id in (None, "", "nope", 1, ["list"], {"a": 1}):
            self.assertIsNone(self.jobs.status(job_id))


class TestErrorsInPlainText(unittest.TestCase):
    def test_python_errors_do_not_reach_the_user(self):
        for error in (AttributeError("'NoneType' object has no attribute 'split'"), KeyError("tree"),
                      TypeError("x"), StopIteration()):
            text = describe_error(error)
            self.assertNotIn("NoneType", text)
            self.assertNotIn("tree", text)
            self.assertIn("see the log", text)

    def test_meaningful_errors_are_kept(self):
        self.assertEqual("no answer", describe_error(ConnectionError("no answer")))

    def test_unexpected_exception_in_a_job(self):
        jobs = JobRegistry()
        job_id, _ = jobs.start(KIND, MagicMock(side_effect=AttributeError("'NoneType' object")))
        wait_for_job(KIND, jobs)
        self.assertEqual(FAILED, jobs.status(job_id).status)
        self.assertNotIn("NoneType", jobs.status(job_id).message)


class TestNoSecrets(unittest.TestCase):
    def assert_no_secret(self, text):
        for secret in (EMAIL, PASSWORD, SMS_CODE, PIN):
            self.assertNotIn(secret, text)

    def test_status_and_log_hold_no_secret_nor_id(self):
        jobs = JobRegistry()

        def work(report_step):
            report_step(f"logging in as {EMAIL}")
            raise JobFailure(f"{EMAIL} with {PASSWORD} and {SMS_CODE} and {PIN} refused")

        with self.assertLogs("psa_car_controller.web.tools.jobs", logging.INFO) as logs:
            job_id, _ = jobs.start(KIND, work, hidden=(EMAIL, PASSWORD, SMS_CODE, PIN))
            wait_for_job(KIND, jobs)
        state = jobs.status(job_id)
        self.assertIn("refused", state.message)
        self.assert_no_secret(json.dumps(asdict(state)))
        self.assert_no_secret("\n".join(logs.output))
        self.assertNotIn(job_id, "\n".join(logs.output))

    def test_exception_text_is_not_logged(self):
        jobs = JobRegistry()
        with self.assertLogs("psa_car_controller.web.tools.jobs", logging.INFO) as logs:
            jobs.start(KIND, MagicMock(side_effect=RuntimeError(PASSWORD)), hidden=(PASSWORD,))
            wait_for_job(KIND, jobs)
        self.assert_no_secret("\n".join(logs.output))

    def test_job_keeps_nothing_of_the_work(self):
        jobs = JobRegistry()
        secret = PASSWORD + "only here"
        job_id, _ = jobs.start(KIND, lambda _: ("ok", secret == "never"), hidden=(secret,))
        wait_for_job(KIND, jobs)
        job = jobs._jobs[job_id]  # pylint: disable=protected-access
        job.thread.join(10)
        self.assertNotIn(secret, repr(vars(job)))
        self.assertNotIn(secret, repr(vars(job.thread)))
        self.assertFalse(hasattr(job.thread, "_args"))


class JobViewTestCase(unittest.TestCase):
    def setUp(self):
        self.views = get_web_view("config_views")
        self.job_view = get_web_view("job_view")
        self.jobs = isolate_jobs(self)
        self.release = threading.Event()
        self.addCleanup(self.release.set)

    def blocked(self, _):
        self.release.wait(10)
        return "finished", None

    def stop_jobs(self):
        """Has to run before the patches go, or a job goes on with the real InitialSetup: real downloads, real logins."""
        self.release.set()
        for job in self.jobs._jobs.values():  # pylint: disable=protected-access
            job.thread.join(10)

    def poll(self, stored, n_intervals=1):
        return self.job_view.poll_job(KIND, stored, n_intervals, lambda _: "full result", "lost")


class TestPolling(JobViewTestCase):
    def test_running_job_keeps_the_button_locked(self):
        job_id, _ = self.jobs.start(KIND, self.blocked, step="first step")
        res = self.poll({"id": job_id})
        self.assertFalse(res[POLL_DISABLED])
        self.assertTrue(res[BUTTON_DISABLED])
        self.assertEqual("first step", res[STATUS][1].children)
        self.assertIs(no_update, res[RESULT])

    def test_end_frees_the_button_stops_the_poll_and_forgets_the_id(self):
        job_id, _ = self.jobs.start(KIND, lambda _: ("ok", None))
        wait_for_job(KIND, self.jobs)
        res = self.poll({"id": job_id})
        self.assertEqual((True, False, None, "full result"),
                         (res[POLL_DISABLED], res[BUTTON_DISABLED], res[STATUS], res[RESULT]))
        self.assertIsNone(res[STORE]["id"])

    def test_failure_frees_the_button(self):
        job_id, _ = self.jobs.start(KIND, MagicMock(side_effect=JobFailure("refused")))
        wait_for_job(KIND, self.jobs)
        self.assertFalse(self.poll({"id": job_id})[BUTTON_DISABLED])

    def test_lost_id_while_waiting_frees_the_button_and_tells(self):
        res = self.poll({"id": "gone"})
        self.assertEqual((True, False), (res[POLL_DISABLED], res[BUTTON_DISABLED]))
        self.assertEqual("lost", text_of(res[RESULT]))
        self.assertIsNone(res[STORE]["id"])

    def test_old_id_found_in_the_tab_stays_silent(self):
        res = self.poll({"id": "gone"}, n_intervals=0)
        self.assertEqual((True, False), (res[POLL_DISABLED], res[BUTTON_DISABLED]))
        self.assertIs(no_update, res[RESULT])
        self.assertIsNone(res[STORE]["id"])

    def test_lost_id_with_another_job_running_follows_it(self):
        self.jobs.start(KIND, self.blocked)
        res = self.poll({"id": "gone"})
        self.assertTrue(res[BUTTON_DISABLED])
        self.assertFalse(res[POLL_DISABLED])

    def test_reload_without_id_shows_the_running_job(self):
        self.jobs.start(KIND, self.blocked, step="first step")
        res = self.poll(None, n_intervals=0)
        self.assertTrue(res[BUTTON_DISABLED])
        self.assertFalse(res[POLL_DISABLED])
        self.assertEqual("first step", res[STATUS][1].children)

    def test_followed_without_id_gets_no_details(self):
        self.jobs.start(KIND, MagicMock(side_effect=JobFailure("refused", {"debug": "screenshot"})))
        wait_for_job(KIND, self.jobs)
        res = self.poll(None)
        self.assertEqual("refused", text_of(res[RESULT]))
        self.assertNotIn("screenshot", str(res[RESULT]))
        self.assertFalse(res[BUTTON_DISABLED])

    def test_nothing_to_show_on_a_fresh_page(self):
        self.jobs.start(KIND, lambda _: ("ok", None))
        wait_for_job(KIND, self.jobs)
        res = self.poll(None, n_intervals=0)
        self.assertIs(no_update, res[RESULT])
        self.assertEqual((True, False), (res[POLL_DISABLED], res[BUTTON_DISABLED]))

    def test_every_answer_moves_the_deadline_of_the_silent_server(self):
        self.jobs.start(KIND, self.blocked)
        self.assertEqual(40 + self.job_view.SILENCE_TICKS, self.poll(None, n_intervals=40)[POLL_MAX])

    def test_polling_never_gets_the_form(self):
        from psa_car_controller.web.app import dash_app  # pylint: disable=import-outside-toplevel
        # pylint: disable=protected-access
        for spec in dash_app._callback_list:
            if "job-poll.max_intervals" in spec["output"]:
                self.assertEqual([], spec["state"])
                self.assertNotIn("psa-", json.dumps(spec["inputs"]))


class TestLayout(JobViewTestCase):
    def find(self, component, wanted):
        if getattr(component, "id", None) == wanted:
            return component
        children = getattr(component, "children", None)
        for child in children if isinstance(children, (list, tuple)) else [children]:
            if child is not None and not isinstance(child, str):
                found = self.find(child, wanted)
                if found is not None:
                    return found
        return None

    def test_idle_form(self):
        form = self.views.login_form()
        self.assertFalse(self.find(form, "submit-form").disabled)
        self.assertTrue(self.find(form, "login-job-poll").disabled)
        self.assertEqual("polite", getattr(self.find(form, "login-job-status"), "aria-live"))

    def test_page_rendered_during_a_login_shows_it_at_once(self):
        self.jobs.start(self.views.LOGIN_JOB, self.blocked, step="first step")
        form = self.views.login_form()
        self.assertTrue(self.find(form, "submit-form").disabled)
        self.assertFalse(self.find(form, "login-job-poll").disabled)
        self.assertEqual("first step", self.find(form, "login-job-status").children[1].children)

    def test_job_id_is_kept_in_the_tab_only(self):
        store = self.find(self.views.login_form(), "login-job")
        self.assertIsInstance(store, dcc.Store)
        self.assertEqual("session", store.storage_type)


class LoginJobTestCase(JobViewTestCase):
    def setUp(self):
        super().setUp()
        self.setup = MagicMock()
        self.setup.psacc.manager.generate_redirect_url.return_value = "https://example.com/authorize"
        self.setup.psacc.manager.redirect_uri = "mymop://oauth2redirect"
        config = SimpleNamespace(Login_Worker=SimpleNamespace(url=""))
        for name, target, new in (("headless", "get_oauth_code_headless", None),
                                  ("initial_setup", "InitialSetup", MagicMock(return_value=self.setup))):
            patcher = patch.object(self.views, target, new) if new else patch.object(self.views, target)
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)
        patcher = patch.object(self.views.app, "config", config)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.stop_jobs)

    def submit(self):
        return self.views.connectPSA(1, OPEL, EMAIL, PASSWORD, "DE")

    def finish(self, stored):
        wait_for_job(self.views.LOGIN_JOB)
        return self.views.poll_login(stored, 1)


class TestLoginJob(LoginJobTestCase):
    def test_start_itself_starts_the_polling_and_locks_the_button(self):
        # the first answer of the polling can get lost: nothing would ever poll again
        self.initial_setup.side_effect = lambda *_: self.release.wait(10) and self.setup
        _, _, poll_disabled, button_disabled, status = self.submit()
        self.assertFalse(poll_disabled)
        self.assertTrue(button_disabled)
        self.assertIn("app data", status[1].children)

    def test_callback_returns_while_the_login_runs(self):
        started = threading.Event()

        def slow_setup(*_):
            started.set()
            self.release.wait(10)
            return self.setup

        self.initial_setup.side_effect = slow_setup
        result, stored, *_ = self.submit()
        self.assertEqual("", result)
        self.assertTrue(started.wait(10))
        self.assertIsNotNone(self.jobs.active(self.views.LOGIN_JOB))
        res = self.views.poll_login(stored, 1)
        self.assertTrue(res[BUTTON_DISABLED])
        self.assertIn("app data", res[STATUS][1].children)
        self.release.set()
        self.assertIn("Login successful", text_of(self.finish(stored)[RESULT]))

    def test_invalid_form_starts_no_job(self):
        result, stored, *_ = self.views.connectPSA(1, None, "", "", None)
        self.assertIn("marked fields", text_of(result))
        self.assertIs(no_update, stored)
        self.assertIsNone(self.jobs.latest(self.views.LOGIN_JOB))
        self.initial_setup.assert_not_called()

    def test_steps_follow_the_login(self):
        steps = []

        def headless(*_):
            steps.append(self.jobs.active(self.views.LOGIN_JOB).step)
            return "code"

        self.headless.side_effect = headless
        self.setup.connect.side_effect = lambda _: steps.append(self.jobs.active(self.views.LOGIN_JOB).step)
        _, stored, *_ = self.submit()
        self.finish(stored)
        self.assertEqual([self.views.STEP_STELLANTIS, self.views.STEP_SAVING], steps)

    def test_password_is_not_in_what_the_page_gets(self):
        self.initial_setup.side_effect = ConnectionError(f"refused {EMAIL} {PASSWORD}")
        _, stored, *_ = self.submit()
        res = self.finish(stored)
        self.assertIn("refused", text_of(res[RESULT]))
        self.assertNotIn(PASSWORD, str(res))
        self.assertNotIn(EMAIL, str(res))
        self.assertNotIn(PASSWORD, json.dumps(stored))
        self.assertNotIn(EMAIL, json.dumps(stored))

    def test_python_error_of_the_setup_is_plain_text(self):
        self.initial_setup.side_effect = AttributeError("'NoneType' object has no attribute 'split'")
        _, stored, *_ = self.submit()
        self.assertNotIn("NoneType", text_of(self.finish(stored)[RESULT]))

    def test_debug_information_is_scrubbed(self):
        self.headless.side_effect = HeadlessOAuthError("It failed", f"https://x/?u={EMAIL}", f"<b>{EMAIL}</b>",
                                                       [f"typed {PASSWORD}"])
        _, stored, *_ = self.submit()
        res = self.finish(stored)
        self.assertIn("Go to login", text_of(res[RESULT]))
        self.assertNotIn(EMAIL, str(res[RESULT]))
        self.assertNotIn(PASSWORD, str(res[RESULT]))

    def test_link_is_made_with_the_prefix_of_the_polling_request(self):
        from psa_car_controller.web.app import dash_app  # pylint: disable=import-outside-toplevel
        self.headless.side_effect = RuntimeError("no browser")
        _, stored, *_ = self.submit()
        wait_for_job(self.views.LOGIN_JOB)
        config = dash_app.config
        previous = config.requests_pathname_prefix
        self.addCleanup(config.__dict__.pop, "_read_only", None)  # as MyProxyFix does for each request
        config.__dict__["_read_only"] = []
        self.addCleanup(config.__setitem__, "requests_pathname_prefix", previous)
        config.requests_pathname_prefix = "/api/hassio_ingress/abc/"
        alert = self.views.poll_login(stored, 1)[RESULT]
        self.assertIn("/api/hassio_ingress/abc/config_connect?url=", str(alert))

    def test_given_up_login_blocks_only_until_it_stopped(self):
        started = threading.Event()

        def slow_setup(*_):
            started.set()
            self.release.wait(10)
            return self.setup

        self.initial_setup.side_effect = slow_setup
        with patch.object(self.views, "LOGIN_TIMEOUT_S", 0):
            _, stored, *_ = self.submit()
            self.assertTrue(started.wait(10))
        res = self.views.poll_login(stored, 1)
        self.assertFalse(res[BUTTON_DISABLED])
        self.assertIn("too long", text_of(res[RESULT]))
        result, again, *_ = self.submit()
        self.assertIn("hasn't stopped", text_of(result))
        self.assertIs(no_update, again)
        self.assertEqual(1, self.initial_setup.call_count)


class TestOtpJob(JobViewTestCase):
    def setUp(self):
        super().setUp()
        patcher = patch.object(self.views, "app", MagicMock())
        self.app = patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.stop_jobs)

    def submit(self, stored=None):
        result, stored, *_ = self.views.finishOtp(1, PIN, SMS_CODE, stored)
        self.assertEqual("", result)
        return stored

    def test_success_redirects_once(self):
        previous_otp = self.app.myp.remote_client.otp
        with patch.object(self.views, "new_otp_session") as new_session:
            stored = self.submit()
            wait_for_job(self.views.OTP_JOB)
        new_session.assert_called_once_with(SMS_CODE, PIN, previous_otp)
        self.app.start_remote_control.assert_called_once_with()
        res = self.views.poll_otp(stored, 1)
        self.assertIn("OTP config finished", text_of(res[RESULT][0]))
        self.assertIsInstance(res[RESULT][1], dcc.Interval)
        self.assertEqual("otp-redirect-interval", res[RESULT][1].id)
        self.assertIsNone(res[STORE]["id"])

    def test_double_submit_spends_the_sms_code_once(self):
        with patch.object(self.views, "new_otp_session", side_effect=lambda *_: self.release.wait(10)) as new_session:
            first = self.submit()
            second = self.submit(first)
            self.assertEqual(first["id"], second["id"])
            self.release.set()
            wait_for_job(self.views.OTP_JOB)
        new_session.assert_called_once()

    def test_failed_activation_explains(self):
        error = ActivationException("hint for the user", "details")
        self.app.myp.remote_client.is_mqtt_connected.return_value = True
        with patch.object(self.views, "new_otp_session", side_effect=error):
            stored = self.submit()
            wait_for_job(self.views.OTP_JOB)
        res = self.views.poll_otp(stored, 1)
        self.assertIn("could not be activated", text_of(res[RESULT]))
        self.assertIn("previous activation", text_of(res[RESULT]))
        self.assertFalse(res[BUTTON_DISABLED])

    def test_secrets_are_not_shown(self):
        with patch.object(self.views, "new_otp_session", side_effect=ValueError(f"bad {SMS_CODE} {PIN}")):
            stored = self.submit()
            wait_for_job(self.views.OTP_JOB)
        res = self.views.poll_otp(stored, 1)
        self.assertIn("bad", text_of(res[RESULT]))
        self.assertNotIn(SMS_CODE, str(res))
        self.assertNotIn(PIN, str(res))


class TestManualCodeJob(JobViewTestCase):
    def setUp(self):
        super().setUp()
        self.oauth = get_web_view("config_oauth")

    def test_code_is_sent_once_and_the_answer_can_be_polled(self):
        setup = MagicMock()
        setup.connect.side_effect = lambda _: self.release.wait(10)
        with patch.object(self.views, "INITIAL_SETUP", setup):
            result, stored, *_ = self.oauth.finish_oauth(1, "code")
            self.assertEqual("", result)
            _, second, *_ = self.oauth.finish_oauth(2, "code", stored)
            self.assertEqual(stored["id"], second["id"])
            self.release.set()
            wait_for_job(self.views.LOGIN_JOB)
        setup.connect.assert_called_once_with("code")
        self.assertIn("PSA login finish", text_of(self.oauth.poll_oauth(stored, 1)[RESULT]))

    def test_login_running_is_not_disturbed_by_a_code(self):
        setup = MagicMock()
        self.jobs.start(self.views.LOGIN_JOB, self.blocked)
        with patch.object(self.views, "INITIAL_SETUP", setup):
            self.oauth.finish_oauth(1, "code")
        setup.connect.assert_not_called()

    def test_no_login_to_finish(self):
        with patch.object(self.views, "INITIAL_SETUP", None):
            result, stored, *_ = self.oauth.finish_oauth(1, "code")
        self.assertIn("start again", text_of(result))
        self.assertIs(no_update, stored)

    def test_page_keeps_a_place_for_the_result(self):
        layout = str(self.oauth.get_oauth_config_layout("https://example.com"))
        for wanted in ("oauth-result", "oauth-job-status", "oauth-job-poll", "oauth-job"):
            self.assertIn(f"id='{wanted}'", layout)


if __name__ == '__main__':
    unittest.main()
