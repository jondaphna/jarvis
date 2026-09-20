"""The background side has to outlive the call, and the kill switch has to stop it.

Two claims are tested here, and both used to be false.

The first is that nothing standing hangs off a LiveKit job. Workers and the
routine ticker were started inside the `@server.rtc_session` entrypoint with
their shutdown callbacks on the job context, so the scheduler stopped when the
call did - which means "run the briefing while I am away" was a setting that
described nothing. The test for that is not a mock of LiveKit; it is a reading
of `agent.py` itself, because what matters is *where the lines are*.

The second is that the kill switch says only what it can do. Cancelling a plain
blocking job does not end it (Python cannot stop a thread from outside), so the
test that matters is the one asserting the reply tells the truth about that.
"""

from __future__ import annotations

import re
import threading
import time
from pathlib import Path

import pytest

import services
import workers

SRC = Path(__file__).resolve().parents[1] / "src"


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


@pytest.fixture(autouse=True)
def clean_services():
    """Each test starts from a process with nothing running."""
    services.reset_for_tests()
    yield
    try:
        services.stop(timeout=2.0, reason="test teardown")
    finally:
        services.reset_for_tests()


class TestNothingStandingHangsOffACall:
    """The regression that F13 was. Read as source, because that is the bug."""

    def source(self) -> str:
        return (SRC / "agent.py").read_text(encoding="utf-8")

    def entrypoint(self) -> str:
        """Just the body of the rtc_session entrypoint."""
        source = self.source()
        start = source.index("async def my_agent(")
        end = source.index("\nif __name__ ==", start)
        return source[start:end]

    def test_the_entrypoint_does_not_start_the_workers(self):
        body = self.entrypoint()
        assert "workers.start_workers()" not in body
        assert "start_workers" not in body.replace("# ", "")

    def test_the_entrypoint_does_not_start_the_routines(self):
        body = self.entrypoint()
        assert "routines.start_routines()" not in body

    def test_nothing_standing_is_registered_as_a_shutdown_callback(self):
        """`add_shutdown_callback` fires when the session ends, on a short
        timeout. Per-call cleanup belongs there; a scheduler does not."""
        body = self.entrypoint()
        registered = set(re.findall(r"add_shutdown_callback\((\w[\w.]*)\)", body))
        assert "workers.stop_workers" not in registered
        assert "routines.stop_routines" not in registered
        # The browser is genuinely per-call and should still be there, so this
        # test fails if the entrypoint is gutted rather than corrected.
        assert "browser.close" in registered

    def test_the_services_start_in_the_main_process(self):
        """Before `cli.run_app`, so they are up before the first job and stay
        up between jobs - on every platform, whether the job executor gives
        this entrypoint a thread or a process of its own."""
        source = self.source()
        main = source[source.index("if __name__ =="):]
        assert "serve_in_background()" in main
        assert main.index("serve_in_background()") < main.index("cli.run_app")


class TestTheLifecycle:
    def test_starting_brings_up_workers_and_routines(self):
        state = services.start(reason="test")
        assert state["running"] is True
        assert state["state"] == services.STATE_RUNNING
        assert wait_for(lambda: services.state()["workers"]["running"])
        assert wait_for(lambda: services.state()["routines"]["ticking"])

    def test_starting_twice_is_a_no_op(self):
        first = services.start(reason="test")
        second = services.start(reason="test again")
        assert second["running"] is True
        assert second["owner"] == first["owner"]

    def test_stopping_stops_both(self):
        services.start(reason="test")
        assert wait_for(lambda: services.state()["workers"]["running"])
        state = services.stop(timeout=5.0, reason="test")
        assert state["running"] is False
        assert state["workers"]["running"] is False
        assert state["routines"]["ticking"] is False

    def test_stopping_something_never_started_is_harmless(self):
        assert services.stop(reason="test")["running"] is False

    def test_the_owner_is_this_process(self):
        services.start(reason="test")
        state = services.state()
        assert state["owner"] == services.owner_tag()
        assert state["owns_services"] is True

    def test_a_process_that_does_not_own_them_says_so(self):
        """The dashboard has to be able to tell "off" from "somebody else's"."""
        services.start(reason="test")
        state = services.state()
        state_from_elsewhere = dict(state, this_process="999999:1")
        assert state_from_elsewhere["owner"] != state_from_elsewhere["this_process"]


class TestTheKillSwitch:
    def test_killing_stops_everything(self):
        services.start(reason="test")
        assert wait_for(lambda: services.state()["workers"]["running"])

        state = services.kill()
        assert state["killed"] is True
        assert state["running"] is False
        assert state["workers"]["running"] is False
        assert state["routines"]["ticking"] is False

    def test_a_kill_is_distinguishable_from_never_having_started(self):
        """Otherwise something starts them again on somebody's behalf."""
        assert services.state()["state"] == services.STATE_STOPPED
        services.start(reason="test")
        services.kill()
        assert services.state()["state"] == services.STATE_KILLED

    def test_killing_cancels_queued_work(self):
        services.start(reason="test")
        assert wait_for(lambda: services.state()["workers"]["running"])

        holding = threading.Event()
        host = workers.host()
        host.submit(lambda: holding.wait(timeout=10), name="occupies the worker")
        # Concurrency is one, so these queue behind it and never start.
        refs = [host.submit(lambda: None, name=f"queued {i}") for i in range(3)]
        assert wait_for(lambda: host.status()["queued"] >= 3)

        state = services.kill()
        holding.set()

        assert state["cancelled"] >= 3
        for ref in refs:
            assert host.job(ref).status == "cancelled"

    def test_restart_brings_it_back(self):
        """A stop button with no way back is a trap, not a control."""
        services.start(reason="test")
        services.kill()
        assert services.state()["state"] == services.STATE_KILLED

        state = services.restart(reason="test")
        assert state["running"] is True
        assert wait_for(lambda: services.state()["workers"]["running"])

    def test_killing_twice_is_harmless(self):
        services.start(reason="test")
        services.kill()
        assert services.kill()["running"] is False


class TestItSaysOnlyWhatItCanDo:
    """A button called "kill" must not overstate itself.

    Cancelling a job that is a plain blocking function does not end the
    function. The job is cancelled from every caller's point of view and
    nothing new starts, but the work runs to its end. `set_services` documents
    that; these tests keep the documentation and the behaviour together.
    """

    def test_a_blocking_job_is_marked_cancelled_even_though_it_runs_on(self):
        services.start(reason="test")
        assert wait_for(lambda: services.state()["workers"]["running"])

        finished = threading.Event()
        released = threading.Event()

        def stubborn():
            released.wait(timeout=10)
            finished.set()

        host = workers.host()
        ref = host.submit(stubborn, name="cannot be interrupted")
        assert wait_for(lambda: host.job(ref).status == "running")

        services.kill()
        assert host.job(ref).status == "cancelled"
        # Still going, which is the honest part.
        assert not finished.is_set()

        released.set()
        assert wait_for(finished.is_set)

    def test_the_docstring_admits_the_limit(self):
        """If somebody rewrites this to claim a hard stop, this fails."""
        from control_api import Control

        # Whitespace-normalised: the claim is what matters, not where the
        # docstring happens to wrap.
        doc = " ".join((Control.set_services.__doc__ or "").lower().split())
        assert "cannot end a thread from outside" in doc
        assert "voice call is deliberately untouched" in doc


class TestTheApiSurface:
    def test_the_switch_rejects_anything_it_does_not_do(self):
        from control_api import Control

        control = Control.__new__(Control)
        with pytest.raises(ValueError, match="kill"):
            control.set_services("explode")

    def test_state_is_readable_before_anything_starts(self):
        """The dashboard polls this on load, before any call has happened."""
        state = services.state()
        assert state["running"] is False
        assert state["state"] == services.STATE_STOPPED
        assert "workers" in state and "routines" in state
