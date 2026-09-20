"""Regressions for the September 2026 re-audit, on the `jarvis_new` side.

The kill switch, the permission switches and the content engine have their
repairs tested in the files that already own those subjects
(`test_services.py`, `test_permissions.py`, `test_content_edges.py`). What is
left here is the two that belong nowhere else: the telemetry call that stalled
the voice loop, and the control token that could be handed out without ever
reaching the file the dashboard reads.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    from jarvis import paths

    paths.refresh()
    paths.ensure_dirs()
    yield tmp_path / "home"
    monkeypatch.delenv("JARVIS_HOME", raising=False)
    paths.refresh()


# --------------------------------------------------------------------------- #
# V01 - reading the machine must not stop the conversation
# --------------------------------------------------------------------------- #

class TestTelemetryStaysOffTheVoiceLoop:
    """`psutil.cpu_percent(interval=0.4)` blocks for its interval.

    Called straight from an async tool, it stalled the event loop for the
    whole 400 milliseconds - the audit measured 400.5 - which is long enough
    to hear as a gap in the conversation.
    """

    def tools(self):
        import os_tools

        os_tools.reset_telemetry_cache()
        return os_tools

    def test_the_event_loop_keeps_running_while_the_machine_is_read(self):
        pytest.importorskip("psutil")
        os_tools = self.tools()

        async def exercise():
            ticks = 0
            stop = asyncio.Event()

            async def heartbeat():
                nonlocal ticks
                while not stop.is_set():
                    ticks += 1
                    await asyncio.sleep(0.005)

            beat = asyncio.create_task(heartbeat())
            await asyncio.to_thread(os_tools._sample_machine)
            stop.set()
            await beat
            return ticks

        # The sample takes ~400ms. A loop that was free to run during it got
        # tens of ticks in; a blocked one gets about one.
        assert asyncio.run(exercise()) > 10

    def test_a_recent_reading_is_answered_from_instead_of_retaken(self,
                                                                   monkeypatch):
        pytest.importorskip("psutil")
        os_tools = self.tools()
        samples: list[int] = []

        def slow_sample():
            samples.append(1)
            time.sleep(0.05)
            return "processor 3 percent"

        monkeypatch.setattr(os_tools, "_sample_machine", slow_sample)
        tools = os_tools.OSTools()

        async def exercise():
            first = await tools.system_status.__wrapped__(tools, None)
            started = time.monotonic()
            second = await tools.system_status.__wrapped__(tools, None)
            return first, second, time.monotonic() - started

        first, second, elapsed = asyncio.run(exercise())

        assert first == second
        assert len(samples) == 1, "the second ask re-read the machine"
        assert elapsed < 0.02, "the second ask waited for a fresh sample"

    def test_the_cache_expires(self):
        os_tools = self.tools()
        assert os_tools._cached_machine() is None

        os_tools._store_machine("processor 3 percent")
        assert os_tools._cached_machine() == "processor 3 percent"

        # Pretend the reading was taken a while ago.
        with os_tools._telemetry_lock:
            os_tools._telemetry = (time.monotonic() - os_tools.TELEMETRY_TTL - 1,
                                   "processor 3 percent")
        assert os_tools._cached_machine() is None


# --------------------------------------------------------------------------- #
# F32 - a token the dashboard cannot read is not a token
# --------------------------------------------------------------------------- #

class TestTheControlTokenReachesTheFile:
    """The dashboard proxy reads the token *file* to talk to the control API.

    A token that only ever existed in one process's memory is therefore not a
    boundary, it is a control API that refuses every request the dashboard
    makes - with no explanation, because everything looks fine from inside.
    The old code returned exactly that after three failed rounds, and an empty
    token file was enough to get there.
    """

    def test_a_token_is_created_and_persisted(self, home):
        from control_api import load_token, token_path

        token = load_token()
        assert token
        assert token_path().read_text(encoding="utf-8").strip() == token

    def test_asking_twice_gives_the_same_token(self, home):
        from control_api import load_token

        assert load_token() == load_token()

    def test_an_empty_token_file_does_not_produce_a_phantom_token(self, home,
                                                                   monkeypatch):
        """The audit's reproduction: an empty file made consecutive calls
        return different unpersisted tokens, and the file stayed empty."""
        import control_api
        from control_api import load_token, token_path

        # An empty file old enough to be wreckage rather than a write that is
        # about to land. Age is the only safe way to tell those apart.
        monkeypatch.setattr(control_api, "TOKEN_STALE_SECONDS", 0.0)
        token_path().write_text("", encoding="utf-8")

        first, second = load_token(), load_token()
        assert first == second, "consecutive calls invented different tokens"
        assert token_path().read_text(encoding="utf-8").strip() == first, (
            "the token handed out was never written to the file the "
            "dashboard reads")

    def test_a_fresh_empty_file_is_waited_for_rather_than_deleted(self, home,
                                                                   monkeypatch):
        """It is the winner of the race, a moment before it writes. Deleting
        it because it is empty right now is deleting the file another process
        is in the middle of writing - which is how two processes end up
        holding different tokens."""
        import control_api
        from control_api import token_path

        monkeypatch.setattr(control_api, "TOKEN_RETRY_DELAY", 0.01)
        token_path().write_text("", encoding="utf-8")

        winner = "the-other-process-token"

        def land_the_write():
            time.sleep(0.05)
            token_path().write_text(winner, encoding="utf-8")

        writer = threading.Thread(target=land_the_write)
        writer.start()
        try:
            assert control_api.load_token() == winner
        finally:
            writer.join(5)

    def test_a_token_that_cannot_be_stored_refuses_rather_than_pretending(
            self, home, monkeypatch):
        import control_api

        monkeypatch.setattr(control_api, "TOKEN_RETRY_DELAY", 0.0)
        monkeypatch.setattr(control_api.os, "open",
                            lambda *a, **k: (_ for _ in ()).throw(
                                OSError("read-only filesystem")))

        with pytest.raises(RuntimeError, match="control token"):
            control_api.load_token()

    def test_two_processes_starting_together_agree(self, home):
        """Whoever loses the race reads the winner's, rather than each going
        on believing its own."""
        from control_api import load_token

        results: list[str] = []
        errors: list[BaseException] = []
        barrier = threading.Barrier(8)

        def race():
            try:
                barrier.wait(timeout=5)
                results.append(load_token())
            except BaseException as exc:              # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=race) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)

        assert errors == []
        assert len(set(results)) == 1, f"processes disagreed: {set(results)}"
