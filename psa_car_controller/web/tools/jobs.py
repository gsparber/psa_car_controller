"""Long actions of the web UI run in a thread and the page asks for their state, so the answer can't get lost.

A login takes minutes. The web server handles one request at a time (run_simple without threaded) and a reverse proxy
gives up long before that (Cloudflare: 100 s), which left the page without an answer and the login running unseen.
The app is one process, a dict is enough to share the state between the requests.
"""
import logging
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, Optional, Tuple

from psa_car_controller.common.sensitive_filter import MASK

logger = logging.getLogger(__name__)

PENDING, RUNNING, DONE, FAILED = "pending", "running", "done", "failed"
DEFAULT_TIMEOUT_S = 300
# long enough to come back to the page, short enough not to keep screenshots of the login page around
RESULT_TTL_S = 15 * 60
MAX_FINISHED_JOBS = 20
# their text says nothing to the user: 'NoneType' object has no attribute 'split', 'tree'...
INTERNAL_ERRORS = (AttributeError, ImportError, LookupError, NameError, StopIteration, TypeError)


class JobFailure(Exception):
    """The job ended without success. Its message is shown to the user, result holds what a page needs to say more."""

    def __init__(self, message, result=None):
        super().__init__(message)
        self.result = result


class JobBusy(Exception):
    """A job given up after its timeout has not stopped yet: starting the next one would do the same work twice."""


def describe_error(error: Exception) -> str:
    """Text for the user, the log holds the rest."""
    text = str(error)
    if isinstance(error, INTERNAL_ERRORS) or not text:
        return f"Something went wrong ({type(error).__name__}), see the log for details."
    return text


def scrub(text: Any, hidden: Iterable[str]) -> Any:
    """Removes what the user typed in from a text, whatever library put it there."""
    if not isinstance(text, str):
        return text
    for value in hidden:
        if value:
            text = text.replace(value, MASK)
    return text


@dataclass(frozen=True)
class JobState:
    """What a page may know about a job. Has no id: a visitor who gets the state of a running job gets nothing more."""
    kind: str
    status: str
    step: str
    message: str
    result: Any

    @property
    def active(self) -> bool:
        return self.status in (PENDING, RUNNING)


class _Job:  # pylint: disable=too-few-public-methods,too-many-instance-attributes
    # pylint: disable-next=too-many-arguments,too-many-positional-arguments
    def __init__(self, kind, step, timeout_s, timeout_message, now):
        self.kind = kind
        self.status = PENDING
        self.step = step
        self.message = ""
        self.result = None
        self.timeout_s = timeout_s
        self.timeout_message = timeout_message
        self.created = now
        self.finished = None
        self.given_up = False
        self.thread: Optional[threading.Thread] = None

    @property
    def active(self):
        return self.status in (PENDING, RUNNING)

    def stopped(self):
        return self.thread is None or not self.thread.is_alive()

    def state(self):
        return JobState(self.kind, self.status, self.step, self.message, self.result)


class JobRegistry:
    """At most one job of a kind runs. Starting another one while it runs gives back the running one."""

    def __init__(self, ttl_s=RESULT_TTL_S, max_finished=MAX_FINISHED_JOBS, clock: Callable[[], float] = time.monotonic):
        self._ttl_s = ttl_s
        self._max_finished = max_finished
        self._clock = clock
        self._lock = threading.Lock()
        self._jobs: Dict[str, _Job] = {}

    # pylint: disable=too-many-arguments
    def start(self, kind: str, work: Callable[[Callable[[str], None]], Tuple[str, Any]], *, step: str = "",
              timeout_s: float = DEFAULT_TIMEOUT_S, timeout_message: str = "This took too long, it was given up on.",
              hidden: Iterable[str] = ()) -> Tuple[Optional[str], bool]:
        """Runs work(report_step) in a thread; it returns (message, result) or raises. The arguments live only as long
        as the thread: the password stays in the closure of work and nothing here keeps it.
        Returns (id of the job, True), or (None, False) if a job of this kind is running: its id is not handed out,
        because the id gives access to the details of the result and the caller may be another visitor."""
        with self._lock:
            self._expire()
            self._prune()
            jobs = [job for job in self._jobs.values() if job.kind == kind]
            if any(job.active for job in jobs):
                logger.info("A %s job is already running, not starting another one", kind)
                return None, False
            # a job which ended by itself may still be leaving its thread, that one has no more work to do
            if any(job.given_up and not job.stopped() for job in jobs):
                raise JobBusy(kind)
            job_id = secrets.token_urlsafe(32)
            job = _Job(kind, step, timeout_s, timeout_message, self._clock())
            self._jobs[job_id] = job
            job.thread = threading.Thread(target=self._run, args=(job, work, tuple(hidden)), name=f"job-{kind}",
                                          daemon=True)
            try:
                job.thread.start()
            except RuntimeError as e:
                logger.error("Can't start the %s job: %s", kind, e)
                self._finish(job, FAILED, "The server can't start this right now, please try again later.")
        return job_id, True

    def status(self, job_id: Any) -> Optional[JobState]:
        """None when the job is unknown: finished long ago, app restarted, or an id that never existed."""
        if not isinstance(job_id, str):
            return None
        with self._lock:
            self._expire()
            self._prune()
            job = self._jobs.get(job_id)
            return job.state() if job else None

    def active(self, kind: str) -> Optional[JobState]:
        """The running job of this kind: a reloaded page or another device can follow it without knowing the id."""
        with self._lock:
            self._expire()
            return next((job.state() for job in self._jobs.values() if job.kind == kind and job.active), None)

    def latest(self, kind: str) -> Optional[JobState]:
        """The last job of this kind."""
        with self._lock:
            self._expire()
            self._prune()
            jobs = [job for job in self._jobs.values() if job.kind == kind]
            return max(jobs, key=lambda job: job.created).state() if jobs else None

    def _run(self, job: _Job, work, hidden):
        def report_step(step):
            with self._lock:
                if job.active:
                    job.step = scrub(step, hidden)

        with self._lock:
            if job.active:
                job.status = RUNNING
        logger.info("%s job started", job.kind)
        status, message, result = DONE, "", None
        try:
            message, result = work(report_step)
        except JobFailure as e:
            status, message, result = FAILED, str(e), e.result
        except Exception as e:  # pylint: disable=broad-except
            status, message = FAILED, describe_error(e)
            logger.error("%s job failed: %s", job.kind, type(e).__name__)
        with self._lock:
            self._finish(job, status, scrub(message, hidden), result)
        logger.info("%s job ended: %s", job.kind, status)

    def _finish(self, job, status, message, result=None):
        # a job given up on stays failed: the page has been told already, and it can't take the result anymore
        if job.active:
            job.status, job.message, job.result, job.step = status, message, result, ""
            job.finished = self._clock()

    def _expire(self):
        for job in self._jobs.values():
            if job.active and self._clock() - job.created > job.timeout_s:
                logger.warning("%s job gave up after %ss", job.kind, job.timeout_s)
                self._finish(job, FAILED, job.timeout_message)
                job.given_up = True

    def _prune(self):
        now = self._clock()
        # a job which is still stopping stays: start() has to know about it
        finished = sorted(((job_id, job) for job_id, job in self._jobs.items() if not job.active and job.stopped()),
                          key=lambda item: item[1].finished)
        for position, (job_id, job) in enumerate(finished):
            if now - job.finished > self._ttl_s or len(finished) - position > self._max_finished:
                del self._jobs[job_id]


JOBS = JobRegistry()
