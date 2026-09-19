"""A background job that fails once should not need a person to notice.

Everything the business engine does in the background talks to something that
fails for a second and then doesn't: a model endpoint that rate-limits, a
socket that resets, a file being written by something else. Before retries, a
scriptwriting job that hit one of those was simply gone - the job row said
failed, and the next anyone knew of it was a reel that never appeared.

Retries are off by default, because a retry is only right for work that is
safe to run twice, and only the caller knows whether it is. These tests are
about what happens when one is asked for: that the attempt is genuinely
repeated, that the wait grows, that a cancellation is never retried, and that
a job which runs out of attempts still ends in a final state with its last
error on it.
"""

import threading
import time

import pytest

import workers


@pytest.fixture
def host():
    made = workers.WorkerHost(name="test-retry")
    made.start()
    yield made
    made.stop()


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class TestTryingAgain:

    def test_a_failure_that_clears_ends_done(self, host):
        attempts = []

        def flaky():
            attempts.append(time.monotonic())
            if len(attempts) < 3:
                raise RuntimeError("the endpoint was busy")
            return "written"

        ref = host.submit(flaky, name="flaky", retries=2, backoff=0.01)
        assert wait_for(lambda: host.job(ref).status == "done")
        assert host.job(ref).result == "written"
        assert len(attempts) == 3
        # The error from the attempts that failed is not left on a job that
        # succeeded: the panel would show a finished job with an error on it.
        assert host.job(ref).error == ""

    def test_without_asking_it_is_tried_once(self, host):
        attempts = []

        def always_fails():
            attempts.append(1)
            raise RuntimeError("no")

        ref = host.submit(always_fails, name="once")
        assert wait_for(lambda: host.job(ref).status == "failed")
        assert attempts == [1]

    def test_running_out_of_attempts_ends_failed_with_the_last_error(self, host):
        count = []

        def always_fails():
            count.append(1)
            raise RuntimeError(f"attempt {len(count)} broke")

        ref = host.submit(always_fails, name="doomed", retries=2, backoff=0.01)
        assert wait_for(lambda: host.job(ref).status == "failed")
        assert len(count) == 3
        assert "attempt 3 broke" in host.job(ref).error
        assert host.job(ref).finished_at

    def test_the_count_is_visible_while_it_is_happening(self, host):
        def always_fails():
            raise RuntimeError("no")

        ref = host.submit(always_fails, name="counted", retries=1, backoff=0.01)
        assert wait_for(lambda: host.job(ref).status == "failed")
        row = next(job for job in host.status()["jobs"]
                   if job["ref"] == ref)
        assert (row["attempts"], row["max_attempts"]) == (2, 2)

    def test_a_job_that_asked_for_none_does_not_carry_the_count(self, host):
        ref = host.submit(lambda: "fine", name="plain")
        assert wait_for(lambda: host.job(ref).status == "done")
        row = next(job for job in host.status()["jobs"]
                   if job["ref"] == ref)
        assert "attempts" not in row

    def test_an_async_job_retries_the_same_way(self, host):
        attempts = []

        async def flaky():
            attempts.append(1)
            if len(attempts) < 2:
                raise RuntimeError("busy")
            return "ok"

        ref = host.submit(flaky, name="async flaky", retries=1, backoff=0.01)
        assert wait_for(lambda: host.job(ref).status == "done")
        assert len(attempts) == 2


class TestTheWaitBetween:

    def test_it_waits_before_trying_again(self, host):
        at = []

        def flaky():
            at.append(time.monotonic())
            raise RuntimeError("no")

        ref = host.submit(flaky, name="slow retry", retries=1, backoff=0.30)
        assert wait_for(lambda: host.job(ref).status == "failed", timeout=10)
        assert len(at) == 2
        assert at[1] - at[0] >= 0.25, "it tried again immediately"

    def test_the_wait_grows(self, host):
        """Doubling rather than a fixed pause: whatever is failing is either
        momentary or is not going to be fixed by asking again at the same
        rate, and a fixed retry against a rate limit is how one gets longer."""
        at = []

        def flaky():
            at.append(time.monotonic())
            raise RuntimeError("no")

        ref = host.submit(flaky, name="growing", retries=2, backoff=0.20)
        assert wait_for(lambda: host.job(ref).status == "failed", timeout=10)
        assert len(at) == 3
        first, second = at[1] - at[0], at[2] - at[1]
        assert second > first * 1.5, f"{first:.2f}s then {second:.2f}s"

    def test_the_wait_has_a_ceiling(self):
        """Otherwise the fifth retry of a long-lived job is half an hour
        away, and the one worker is asleep for all of it."""
        assert workers.RETRY_CEILING <= 60
        assert workers.RETRY_BACKOFF < workers.RETRY_CEILING

    def test_the_queue_moves_again_afterwards(self, host):
        """The retry sleeps in the worker, so it does hold the queue while it
        waits. That is the accepted cost at concurrency 1; what would not be
        acceptable is the queue never recovering."""
        done = threading.Event()

        def flaky():
            raise RuntimeError("no")

        host.submit(flaky, name="blocker", retries=1, backoff=0.10)
        after = host.submit(lambda: done.set(), name="after")
        assert wait_for(lambda: host.job(after).status == "done", timeout=10)
        assert done.is_set()


class TestWhatIsNeverRetried:

    def test_a_cancelled_job_is_not_tried_again(self, host):
        started, attempts = threading.Event(), []

        def slow():
            attempts.append(1)
            started.set()
            time.sleep(5)

        ref = host.submit(slow, name="cancel me", retries=3, backoff=0.01)
        assert started.wait(timeout=5)
        host.cancel(ref)
        assert wait_for(lambda: host.job(ref).status == "cancelled")
        time.sleep(0.2)
        assert attempts == [1], "a cancelled job was retried"

    def test_a_job_cancelled_in_the_queue_never_runs(self, host):
        release, ran = threading.Event(), []

        host.submit(release.wait, 5, name="in front")
        ref = host.submit(lambda: ran.append(1), name="behind", retries=2)
        host.cancel(ref)
        release.set()
        assert wait_for(lambda: host.job(ref).status == "cancelled")
        assert ran == []

    def test_shutting_down_does_not_start_another_attempt(self):
        """A host going down must not hold the process open for a backoff."""
        made = workers.WorkerHost(name="test-retry-stop")
        made.start()
        began = threading.Event()

        def flaky():
            began.set()
            raise RuntimeError("no")

        made.submit(flaky, name="failing", retries=5, backoff=10.0)
        assert began.wait(timeout=5)
        started = time.monotonic()
        made.stop()
        assert time.monotonic() - started < workers.SHUTDOWN_SECONDS + 2
        assert not made.running
