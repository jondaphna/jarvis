"""Background work must be background: that is the whole claim to test.

The failure this file exists to catch is not a crash. It is a Reel job that
quietly runs on the voice loop, and an assistant that goes deaf for forty
seconds every time somebody asks for content. That does not raise anything, it
does not appear in a log, and the only symptom is a user saying "he stopped
listening again".

So the assertions are about time and isolation: submitting returns at once
however slow the job is, a job that explodes takes nothing else with it, and
the host stops when it is told to.
"""

import threading
import time

import pytest

import workers


@pytest.fixture
def host():
    made = workers.WorkerHost(name="test-workers")
    made.start()
    yield made
    made.stop()


def wait_for(predicate, timeout=5.0):
    """Poll until true. Returns whether it got there, so the test can assert."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_submitting_returns_before_the_job_does(host):
    """The point of the whole module: the caller is not kept waiting."""
    released = threading.Event()

    def slow():
        released.wait(timeout=5.0)
        return "eventually"

    began = time.monotonic()
    ref = host.submit(slow, name="slow")
    elapsed = time.monotonic() - began

    assert ref is not None
    # Generous by two orders of magnitude, and still fails loudly if anyone
    # ever makes submit wait for the result.
    assert elapsed < 0.25, f"submit blocked for {elapsed:.2f}s"
    released.set()
    assert wait_for(lambda: host.job(ref).status == "done")
    assert host.job(ref).result == "eventually"


def test_a_sync_job_runs_off_the_worker_loop(host):
    """A blocking callable must not stall the jobs queued behind it."""
    first_in = threading.Event()
    let_go = threading.Event()

    def blocker():
        first_in.set()
        let_go.wait(timeout=5.0)
        return "first"

    host.submit(blocker, name="blocker")
    assert first_in.wait(timeout=5.0)
    # The loop itself is still responsive while the blocking job sits in a
    # thread: a second submit is accepted rather than queuing behind a frozen
    # event loop.
    second = host.submit(lambda: "second", name="quick")
    assert second is not None
    let_go.set()
    assert wait_for(lambda: host.job(second).status == "done")


def test_an_async_job_is_awaited(host):
    async def work():
        return 21 * 2

    ref = host.submit(work, name="async")
    assert wait_for(lambda: host.job(ref).status == "done")
    assert host.job(ref).result == 42


def test_a_failing_job_is_recorded_and_the_host_survives(host):
    def explode():
        raise RuntimeError("the render died")

    bad = host.submit(explode, name="bad")
    assert wait_for(lambda: host.job(bad).status == "failed")
    assert "the render died" in host.job(bad).error

    # The host took nothing with it: the next job still runs.
    good = host.submit(lambda: "fine", name="good")
    assert wait_for(lambda: host.job(good).status == "done")
    assert host.job(good).result == "fine"


def test_arguments_reach_the_job(host):
    ref = host.submit(lambda a, b=0: a + b, 2, b=3, name="sum")
    assert wait_for(lambda: host.job(ref).status == "done")
    assert host.job(ref).result == 5


def test_status_counts_what_is_outstanding(host):
    ref = host.submit(lambda: "x", name="one")
    assert wait_for(lambda: host.job(ref).status == "done")
    state = host.status()
    assert state["running"] is True
    assert any(job["ref"] == ref for job in state["jobs"])


def test_stopping_twice_is_harmless():
    made = workers.WorkerHost(name="stop-twice")
    made.start()
    made.stop()
    made.stop()
    assert made.running is False


def test_starting_twice_keeps_one_thread():
    made = workers.WorkerHost(name="start-twice")
    try:
        assert made.start() is True
        first = made._thread
        assert made.start() is True
        assert made._thread is first
    finally:
        made.stop()


def test_the_shared_host_is_one_object():
    assert workers.host() is workers.host()


# --------------------------------------------------------------------------- #
# Pausing
# --------------------------------------------------------------------------- #

def test_pausing_holds_new_jobs_without_losing_them(host):
    """Pause is not stop. The queue survives, and resuming runs it.

    The bug this guards against is the obvious implementation: stopping the
    host on pause. That tears the loop down, the queue goes with it, and the
    jobs somebody queued before going to bed are gone in the morning.
    """
    ran = threading.Event()

    host.pause()
    assert host.paused is True

    ref = host.submit(ran.set, name="held")
    assert ref is not None
    # Long enough that a worker which was going to take it, would have.
    assert not ran.wait(timeout=0.5), "a paused host started a job anyway"
    assert host.job(ref).status == "queued"

    host.resume()
    assert host.paused is False
    assert ran.wait(timeout=5.0), "resuming did not release the queued job"
    assert wait_for(lambda: host.job(ref).status == "done")


def test_a_job_already_running_is_left_alone_by_pause(host):
    """Killing a render halfway is worse than finishing it."""
    release = threading.Event()
    started = threading.Event()

    def slow():
        started.set()
        release.wait(timeout=5.0)
        return "finished"

    ref = host.submit(slow, name="in flight")
    assert started.wait(timeout=5.0)

    host.pause()
    release.set()
    assert wait_for(lambda: host.job(ref).status == "done")
    assert host.job(ref).result == "finished"


def test_the_status_says_whether_it_is_paused(host):
    """The dashboard draws this, so it has to be in the payload."""
    assert host.status()["paused"] is False
    host.pause()
    assert host.status()["paused"] is True
    host.resume()
    assert host.status()["paused"] is False


def test_pausing_before_the_host_starts_still_holds(host):
    """The flag is read when the loop is built, not only while it is up."""
    quiet = workers.WorkerHost(name="test-workers-paused")
    try:
        quiet.set_paused(True)
        ran = threading.Event()
        ref = quiet.submit(ran.set, name="held from cold")
        assert ref is not None
        assert not ran.wait(timeout=0.5)
        quiet.resume()
        assert ran.wait(timeout=5.0)
    finally:
        quiet.stop()
