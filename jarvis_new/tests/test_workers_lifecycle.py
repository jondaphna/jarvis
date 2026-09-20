"""The worker host under the conditions that only show up after hours of use.

`test_workers.py` proves the happy path: work is queued, work runs, failures
are recorded. This file is about the rest of the life of a long-running
process — the parts nobody sees in a five-minute demo and everybody sees at
two in the morning.

Four things are held still here.

**Cancellation is honest.** A cancelled job stops, says it stopped, and the
worker carries on to the next one rather than dying with it. A cancelled
*blocking* job is a partial promise — Python cannot stop a thread from
outside — and the test says so out loud, because a cancel that silently keeps
running is worse than one that admits what it cannot do.

**The bookkeeping does not grow forever.** The in-memory job history is a
cache in front of the database, and a cache with no eviction is a leak with a
release note. The subtle version of that bug — eviction that stops the moment
one long job sits at the front of the queue — is what this tests.

**Shutdown finishes.** A cancelled worker has to run its `finally` block, or
its job is left on "running" for good and the process prints task-destroyed
warnings on the way out.

**State is not read half-written.** `status()` reads several fields of one job
in a pass while a worker is writing them, and a job seen finished-but-not-timed
reports a duration measured from now, which grows every time you look.
"""

import threading
import time

import pytest

import workers


@pytest.fixture
def host():
    made = workers.WorkerHost(name="lifecycle-workers")
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


# --------------------------------------------------------------------------- #
# Cancellation
# --------------------------------------------------------------------------- #

class TestCancelling:
    def test_a_queued_job_is_never_started(self, host):
        """The cheapest cancel: it hasn't begun, so nothing has to be stopped."""
        let_go = threading.Event()
        ran = threading.Event()

        host.submit(lambda: let_go.wait(timeout=5.0), name="blocker")
        second = host.submit(lambda: ran.set(), name="should-not-run")

        assert host.cancel(second) is True
        assert host.job(second).status == "cancelled"

        let_go.set()
        assert wait_for(lambda: host.job(second).finished_at > 0)
        time.sleep(0.2)
        assert not ran.is_set(), "a cancelled job ran anyway"

    def test_a_running_coroutine_is_actually_stopped(self, host):
        """The real cancel: an await point exists, so the work can be cut off."""
        started = threading.Event()
        finished = threading.Event()

        async def slow():
            import asyncio

            started.set()
            await asyncio.sleep(5.0)
            finished.set()
            return "should never happen"

        ref = host.submit(slow, name="slow-coroutine")
        assert started.wait(timeout=5.0)

        assert host.cancel(ref) is True
        assert wait_for(lambda: host.job(ref).status == "cancelled")
        assert not finished.is_set()
        assert host.job(ref).finished_at > 0

    def test_the_worker_survives_a_cancelled_job(self, host):
        """Cancelling one job must not take the host down with it."""
        async def slow():
            import asyncio

            await asyncio.sleep(5.0)

        ref = host.submit(slow, name="slow")
        assert wait_for(lambda: host.job(ref).status == "running")
        host.cancel(ref)
        assert wait_for(lambda: host.job(ref).status == "cancelled")

        after = host.submit(lambda: "still here", name="after")
        assert wait_for(lambda: host.job(after).status == "done")
        assert host.job(after).result == "still here"

    def test_cancelling_a_blocking_job_is_admitted_to_be_partial(self, host):
        """Python cannot kill a thread, and the API must not pretend it can.

        The job is cancelled from the caller's side immediately; the thread
        runs to its own end with nobody waiting. Documented in `cancel()` and
        pinned here so nobody later "fixes" the docstring to claim more.
        """
        ran_to_completion = threading.Event()

        def stubborn():
            time.sleep(0.5)
            ran_to_completion.set()
            return "finished anyway"

        ref = host.submit(stubborn, name="stubborn")
        assert wait_for(lambda: host.job(ref).status == "running")

        assert host.cancel(ref) is True
        assert wait_for(lambda: host.job(ref).status == "cancelled")
        # The caller is free immediately...
        assert host.job(ref).result is None
        # ...and the thread still finishes on its own, which is the honest
        # half of the promise.
        assert ran_to_completion.wait(timeout=3.0)

    def test_cancelling_a_finished_job_changes_nothing(self, host):
        ref = host.submit(lambda: "done", name="quick")
        assert wait_for(lambda: host.job(ref).status == "done")
        assert host.cancel(ref) is False
        assert host.job(ref).status == "done"

    def test_cancelling_something_that_never_existed_is_false(self, host):
        assert host.cancel("no-such-reference") is False


# --------------------------------------------------------------------------- #
# Not growing forever
# --------------------------------------------------------------------------- #

class TestTheHistoryStaysBounded:
    def test_finished_jobs_are_eventually_forgotten(self, host):
        budget = workers.HISTORY * 2
        for index in range(budget + 30):
            ref = host.submit(lambda: None, name=f"job-{index}")
            assert wait_for(lambda r=ref: host.job(r) is None
                            or host.job(r).status in workers.FINAL)
        assert len(host._order) <= budget
        assert len(host._jobs) <= budget

    def test_a_long_job_at_the_front_does_not_stop_eviction(self):
        """Eviction keeps working while a long job is in flight.

        Two workers, because that is what the scenario needs: one held by the
        long job while the other keeps finishing work behind it. With a single
        worker nothing behind the long job can finish, so there would be
        nothing evictable at all and the property would be untested.
        """
        made = workers.WorkerHost(name="eviction", concurrency=2)
        made.start()
        let_go = threading.Event()
        try:
            stuck = made.submit(lambda: let_go.wait(timeout=30.0), name="stuck")
            assert wait_for(lambda: made.job(stuck).status == "running")

            budget = workers.HISTORY * 2
            for index in range(budget + 30):
                ref = made.submit(lambda: None, name=f"filler-{index}")
                assert wait_for(lambda r=ref: made.job(r) is None
                                or made.job(r).status in workers.FINAL, timeout=10.0)

            assert len(made._order) <= budget, \
                "eviction gave up at the first live job, so nothing is forgotten"
            assert len(made._jobs) <= budget
            # And the live job itself was never thrown away to make room.
            assert made.job(stuck) is not None
            assert made.job(stuck).status == "running"
        finally:
            let_go.set()
            made.stop()

    def test_a_long_running_job_is_never_hidden_by_newer_ones(self):
        """The bug this was written for, and it is a reporting bug.

        The history is a fixed-size window over the newest jobs. An overnight
        render is the *oldest* entry in that window long before it finishes,
        so ordering by recency alone drops it off the end while it is still
        running - and "what is the content engine doing" answers "nothing",
        which is the one answer that is never acceptable while it is busy.

        Live jobs are therefore listed first and never cut.
        """
        made = workers.WorkerHost(name="visibility", concurrency=2)
        made.start()
        let_go = threading.Event()
        try:
            stuck = made.submit(lambda: let_go.wait(timeout=30.0), name="overnight")
            assert wait_for(lambda: made.job(stuck).status == "running")

            # Bury it under far more finished work than the window can hold.
            for index in range(workers.HISTORY * 2 + 20):
                ref = made.submit(lambda: None, name=f"filler-{index}")
                assert wait_for(lambda r=ref: made.job(r) is None
                                or made.job(r).status in workers.FINAL, timeout=10.0)

            state = made.status()
            assert state["in_progress"] == 1
            shown = [row["ref"] for row in state["jobs"]]
            assert stuck in shown, "the running job dropped out of the status window"
            assert shown[0] == stuck, "live work should be reported first"
        finally:
            let_go.set()
            made.stop()

    def test_everything_queued_is_reported_even_when_the_window_is_full(self):
        """Same property, for work that has not started rather than work in
        flight: a backlog you cannot see is a backlog you do not clear."""
        made = workers.WorkerHost(name="backlog", concurrency=1)
        made.start()
        let_go = threading.Event()
        try:
            blocker = made.submit(lambda: let_go.wait(timeout=30.0),
                                  name="blocker")
            # Wait for it to actually occupy the worker, as the test above
            # does. Without this the count below is a race: on a loaded
            # machine the blocker can still be queued itself when `status()`
            # is read, and the backlog comes back one too many.
            assert wait_for(lambda: made.job(blocker).status == "running")
            waiting = [made.submit(lambda: None, name=f"waiting-{i}")
                       for i in range(workers.HISTORY + 10)]
            state = made.status()
            shown = {row["ref"] for row in state["jobs"]}
            assert state["queued"] == len(waiting)
            assert all(ref in shown for ref in waiting), \
                "part of the backlog was invisible to the status tool"
        finally:
            let_go.set()
            made.stop()

    def test_a_live_job_is_never_evicted(self):
        made = workers.WorkerHost(name="keep-live", concurrency=2)
        made.start()
        let_go = threading.Event()
        try:
            stuck = made.submit(lambda: let_go.wait(timeout=30.0), name="stuck")
            assert wait_for(lambda: made.job(stuck).status == "running")
            for index in range(workers.HISTORY * 3):
                ref = made.submit(lambda: None, name=f"filler-{index}")
                wait_for(lambda r=ref: made.job(r) is None
                         or made.job(r).status in workers.FINAL, timeout=10.0)
            assert made.job(stuck) is not None
            assert made.job(stuck).status == "running"
        finally:
            let_go.set()
            made.stop()


# --------------------------------------------------------------------------- #
# Shutdown
# --------------------------------------------------------------------------- #

class TestShutdown:
    def test_a_job_in_flight_does_not_hold_shutdown_open(self):
        made = workers.WorkerHost(name="shutdown-inflight")
        made.start()

        async def slow():
            import asyncio

            await asyncio.sleep(30.0)

        made.submit(slow, name="slow")
        assert wait_for(lambda: made.status()["in_progress"] == 1)

        began = time.monotonic()
        made.stop(timeout=3.0)
        elapsed = time.monotonic() - began

        assert made.running is False
        assert elapsed < 3.0, f"shutdown took {elapsed:.1f}s with a job in flight"

    def test_the_thread_is_gone_afterwards(self):
        made = workers.WorkerHost(name="shutdown-thread")
        made.start()
        thread = made._thread
        made.stop()
        assert thread is not None
        assert wait_for(lambda: not thread.is_alive())

    def test_submitting_after_a_stop_brings_the_host_back(self):
        """Not a leak, a usability property: the shared host is per-process
        and a call ending must not make the next call's engine dead."""
        made = workers.WorkerHost(name="restart")
        made.start()
        made.stop()
        ref = made.submit(lambda: "back", name="after-stop")
        try:
            assert ref is not None
            assert wait_for(lambda: made.job(ref).status == "done")
        finally:
            made.stop()


# --------------------------------------------------------------------------- #
# Reading state while it is being written
# --------------------------------------------------------------------------- #

class TestConcurrentReads:
    def test_status_never_reports_a_finished_job_with_no_finish_time(self, host):
        """A job seen finished-but-not-timed reports a duration counted from
        now, which grows every time the status tool is called."""
        for index in range(40):
            host.submit(lambda: None, name=f"job-{index}")

        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            for row in host.status()["jobs"]:
                if row["status"] in workers.FINAL:
                    assert row["seconds"] < 60, \
                        f"{row['ref']} reported {row['seconds']}s - read half-written"
            time.sleep(0.005)

    def test_many_threads_submitting_at_once_all_get_a_reference(self, host):
        """The voice thread is not the only caller: a scheduled routine and a
        settings-panel request can arrive at the same moment."""
        refs: list[str] = []
        lock = threading.Lock()

        def submit_a_few() -> None:
            for _ in range(20):
                ref = host.submit(lambda: None, name="concurrent")
                with lock:
                    refs.append(ref)

        threads = [threading.Thread(target=submit_a_few) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        assert len(refs) == 160
        assert all(ref is not None for ref in refs)
        assert len(set(refs)) == 160, "two jobs were given the same reference"

    def test_jobs_run_one_at_a_time_by_default(self, host):
        """These jobs spend money and share one browser profile. Serialising
        them is worth more than throughput, so it is a tested property."""
        concurrent = 0
        peak = 0
        lock = threading.Lock()

        def work():
            nonlocal concurrent, peak
            with lock:
                concurrent += 1
                peak = max(peak, concurrent)
            time.sleep(0.05)
            with lock:
                concurrent -= 1

        refs = [host.submit(work, name=f"serial-{i}") for i in range(6)]
        for ref in refs:
            assert wait_for(lambda r=ref: host.job(r).status == "done", timeout=10)
        assert peak == 1, f"{peak} jobs ran at once with concurrency 1"
