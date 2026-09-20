"""The promise the whole design rests on: the voice does not wait for the work.

Everything else in the content engine is a convenience. This is the property
that decides whether the assistant is usable while the business runs, and it
is the one that cannot be checked by reading the code - a tool that blocks
raises nothing, logs nothing, and shows up only as an assistant that went
quiet in the middle of a sentence.

Two numbers are measured, because they fail differently.

**Tool call latency** is how long the model waits for a tool to return. It is
what a person would call "the voice is slow".

**Event loop lag** is how late a scheduled callback runs on the loop the audio
pipeline shares. This is the one that actually causes stutter: a tool can
return quickly and still leave something on the loop that starves the audio
frames behind it. A design that moved the work to a thread but left, say, a
synchronous database write on the voice loop would pass the first measurement
and fail this one.

Both are measured on a persistent event loop, the way the agent really calls
them, rather than through `asyncio.run` per call - loop setup is not part of
what a tool costs.

The load is the shipped configuration: one background job at a time, doing the
kind of work these jobs actually do. Content jobs wait on other computers - an
API call for the script, an API call for the voice, an API call for the images
- and ffmpeg renders in a subprocess of its own. All of that releases the GIL.
`TestUnderPathologicalLoad` covers the case that does not, and records what it
costs rather than pretending it away.
"""

import asyncio
import json
import statistics
import threading
import time

import pytest

import workers
from content.store import ContentStore
from content.tools import ContentStudio

#: What a voice tool must stay under, per call, under the load it will really
#: see. Generous next to the hundreds of milliseconds a realtime turn absorbs,
#: and tight enough that anything genuinely blocking misses it by an order of
#: magnitude.
BUDGET_MS = 20.0

#: How late a callback on the voice loop may run. Audio frames arrive every
#: 10-20ms, so lag past this is where a listener starts to hear it.
LOOP_LAG_MS = 20.0

#: How long to allow a routine to actually fire, in the one test that needs it
#: to. This is a precondition rather than a budget: nothing is being measured
#: against it, so it is set to "long enough on the slowest machine we run on"
#: instead of "the number we want". The loose version of this was the single
#: flakiest test in the suite.
FIRING_DEADLINE = 180.0

SCRIPT = {
    "hook": "A hook that earns the next two seconds.",
    "beats": [
        {"at": 2.0, "voiceover": "One.", "on_screen": "one", "visual": "a lit match"},
        {"at": 6.0, "voiceover": "Two.", "on_screen": "two", "visual": "a dark room"},
    ],
    "caption": "A caption.",
    "hashtags": ["one", "two"],
    "seconds": 30,
}


@pytest.fixture(autouse=True)
def offline_brain():
    """Every job in this file gets a fake model.

    Latency work queues real jobs, and some of them finish after the test that
    queued them. Without this they reach the real backend, fail on a missing
    key, and print a page of noise that has nothing to do with what is being
    measured.
    """
    from content import scriptwriter

    original = scriptwriter.Scriptwriter.backend

    class FakeBrain:
        label = "fake brain"

        def ask(self, system, prompt, effort):
            return json.dumps([SCRIPT])

    scriptwriter.Scriptwriter.backend = lambda self: (FakeBrain(), "")
    yield
    scriptwriter.Scriptwriter.backend = original


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    from jarvis import paths

    paths.refresh()
    yield tmp_path / "home"
    monkeypatch.delenv("JARVIS_HOME", raising=False)
    paths.refresh()


def summarise(samples: list[float], label: str) -> dict:
    ordered = sorted(samples)
    report = {"label": label, "n": len(ordered),
              "p50": statistics.median(ordered),
              "p95": ordered[max(0, int(len(ordered) * 0.95) - 1)],
              "max": ordered[-1]}
    print(f"\n  {label}: n={report['n']} p50={report['p50']:.2f}ms "
          f"p95={report['p95']:.2f}ms max={report['max']:.2f}ms")
    return report


class Load:
    """A background host and a database writer, running the whole time."""

    def __init__(self, db_path, concurrency=1, cpu_bound=False, jobs=6):
        self.host = workers.WorkerHost(name="latency-load", concurrency=concurrency)
        self.db = ContentStore(db_path=db_path)
        self.db_path = db_path
        self._stop = threading.Event()
        self._writer: threading.Thread | None = None
        self._topper: threading.Thread | None = None
        self.cpu_bound = cpu_bound
        self.jobs = jobs

    def start(self) -> "Load":
        self.host.start()
        self._topper = threading.Thread(target=self._keep_busy, daemon=True)
        self._topper.start()
        self._writer = threading.Thread(target=self._hammer, daemon=True)
        self._writer.start()
        time.sleep(0.3)             # let it actually get going
        return self

    def _keep_busy(self):
        """Keep work in flight without ever monopolising the host.

        Short jobs topped up continuously rather than a handful of long ones.
        The shipped concurrency is one, so a few eight-second jobs would mean
        a real job queued behind them waits the best part of a minute - which
        measures the fixture, not the engine.
        """
        while not self._stop.is_set():
            if self.host.status()["queued"] < self.jobs:
                self.host.submit(self._work, name="load")
            time.sleep(0.01)

    def _work(self):
        deadline = time.monotonic() + 0.15
        if not self.cpu_bound:
            # What these jobs really do: wait on somebody else's computer.
            while time.monotonic() < deadline and not self._stop.is_set():
                time.sleep(0.02)
                json.loads(json.dumps(SCRIPT))       # the CPU they really use
            return "waited"
        total = 0
        while time.monotonic() < deadline and not self._stop.is_set():
            total += sum(i * i for i in range(20_000))
        return total

    def _hammer(self):
        writer = ContentStore(db_path=self.db_path)
        try:
            while not self._stop.is_set():
                ref = writer.create_job("load")
                writer.add_asset(ref, kind="script", body=SCRIPT)
                writer.finish_job(ref, result={"count": 1})
                time.sleep(0.002)
        except Exception as exc:                     # pragma: no cover
            print(f"  (load writer stopped: {exc})")
        finally:
            writer.close()

    def stop(self):
        self._stop.set()
        for thread in (self._topper, self._writer):
            if thread is not None:
                thread.join(timeout=15)
        self.host.stop()
        self.db.close()


@pytest.fixture
def busy(tmp_path, home):
    """The shipped configuration under realistic load."""
    load = Load(tmp_path / "jarvis.db").start()
    try:
        yield load
    finally:
        load.stop()


def measure_on_one_loop(calls, rounds=80):
    """Call the tools on a persistent loop, timing each call.

    Returns per-call latencies and the lag of a 10ms ticker sharing the loop,
    which is the closest thing to what the audio pipeline experiences.

    A bounded number of rounds rather than a fixed duration: an unbounded loop
    queues thousands of real jobs in two seconds, and then the measurement is
    dominated by a backlog no real user could create.
    """
    async def run():
        latencies: list[float] = []
        lags: list[float] = []
        done = asyncio.Event()

        async def ticker():
            while not done.is_set():
                expected = time.perf_counter() + 0.01
                await asyncio.sleep(0.01)
                lags.append((time.perf_counter() - expected) * 1000)

        async def caller():
            try:
                for index in range(rounds):
                    for call in calls:
                        began = time.perf_counter()
                        await call(index)
                        latencies.append((time.perf_counter() - began) * 1000)
                    await asyncio.sleep(0.005)
            finally:
                done.set()

        await asyncio.gather(ticker(), caller())
        return latencies, lags

    return asyncio.run(run())


class TestTheVoiceStaysResponsive:
    def test_every_tool_answers_inside_the_budget(self, busy, monkeypatch):
        """All three tools, on a live loop, while the engine is working."""
        from content import pipeline

        monkeypatch.setattr(pipeline, "run_script_job", lambda *a, **k: None)
        studio = ContentStudio(db=busy.db)
        ref = busy.db.create_job("espresso")
        busy.db.finish_job(ref, result={"count": 1, "scripts": [SCRIPT]})

        calls = [
            lambda i: studio.write_reel_scripts.__wrapped__(
                studio, None, topic=f"topic {i}", count=1),
            lambda i: studio.content_engine_status.__wrapped__(studio, None),
            lambda i: studio.read_reel_script.__wrapped__(
                studio, None, reference=ref),
        ]
        latencies, _ = measure_on_one_loop(calls)
        report = summarise(latencies, "all three tools, realistic load")
        assert report["p95"] < BUDGET_MS, f"p95 was {report['p95']:.1f}ms"

    def test_starting_work_is_the_fastest_of_them(self, busy, monkeypatch):
        """The one a user actually triggers. It touches no database at all,
        which is the reason it can promise anything under contention."""
        from content import pipeline

        monkeypatch.setattr(pipeline, "run_script_job", lambda *a, **k: None)
        studio = ContentStudio(db=busy.db)
        calls = [lambda i: studio.write_reel_scripts.__wrapped__(
            studio, None, topic=f"topic {i}", count=1)]

        latencies, _ = measure_on_one_loop(calls)
        report = summarise(latencies, "write_reel_scripts")
        assert report["p95"] < BUDGET_MS, f"p95 was {report['p95']:.1f}ms"
        assert report["max"] < BUDGET_MS * 5, f"max was {report['max']:.1f}ms"

    def test_the_voice_event_loop_is_not_starved(self, busy, monkeypatch):
        """The measurement that stands in for audio stutter."""
        from content import pipeline

        monkeypatch.setattr(pipeline, "run_script_job", lambda *a, **k: None)
        studio = ContentStudio(db=busy.db)
        calls = [
            lambda i: studio.write_reel_scripts.__wrapped__(
                studio, None, topic=f"loop {i}", count=1),
            lambda i: studio.content_engine_status.__wrapped__(studio, None),
        ]
        _, lags = measure_on_one_loop(calls)
        report = summarise(lags, "voice loop lag")
        assert report["p95"] < LOOP_LAG_MS, \
            f"the voice loop ran {report['p95']:.1f}ms late at p95"


class TestUnderPathologicalLoad:
    def test_cpu_bound_background_work_is_recorded_not_hidden(self, tmp_path, home,
                                                              monkeypatch):
        """What it costs when background jobs hold the GIL, and why they don't.

        Four pure-Python CPU-bound jobs running flat out will slow every other
        thread in the process, including the voice. That is CPython, not this
        design: no amount of care in a tool makes it responsive while four
        threads never yield the interpreter.

        It is recorded rather than asserted tightly because the engine does
        not create that load. Its jobs wait on model APIs and on ffmpeg in a
        subprocess of its own, both of which release the GIL, and the shipped
        concurrency is one job at a time. The number below is the ceiling for
        anyone who later raises concurrency and adds CPU-bound work - and the
        reason to reach for a subprocess when they do.
        """
        from content import pipeline

        load = Load(tmp_path / "jarvis.db", concurrency=4, cpu_bound=True).start()
        try:
            monkeypatch.setattr(pipeline, "run_script_job", lambda *a, **k: None)
            studio = ContentStudio(db=load.db)
            calls = [lambda i: studio.write_reel_scripts.__wrapped__(
                studio, None, topic=f"topic {i}", count=1)]
            latencies, lags = measure_on_one_loop(calls)
            tools = summarise(latencies, "write_reel_scripts, GIL-saturated")
            summarise(lags, "voice loop lag, GIL-saturated")
            # Loose on purpose: this documents a ceiling, it does not defend a
            # promise. A regression that made it seconds would still fail.
            assert tools["p50"] < 250, f"p50 was {tools['p50']:.1f}ms"
        finally:
            load.stop()

    def test_the_shipped_concurrency_is_one(self):
        """The mitigation the measurement above argues for is the default."""
        assert workers.WorkerHost().concurrency == 1


class TestTheWorkStillHappens:
    def test_a_real_job_completes_end_to_end_while_the_host_is_busy(self, busy, home):
        """Responsiveness is worthless if the work never lands.

        The full path in one test: the tool queues it, a worker writes it, it
        reaches SQLite, and the read-back tool speaks the hook - all while the
        host is working and the database is being written to.
        """
        from content import pipeline, scriptwriter

        class FakeBrain:
            label = "fake brain"

            def ask(self, system, prompt, effort):
                time.sleep(0.2)          # a model takes time; the voice must not
                return "```json\n" + json.dumps([SCRIPT]) + "\n```"

        # Not a fixture: the job runs on a worker thread and has to see this.
        scriptwriter.Scriptwriter.backend = lambda self: (FakeBrain(), "")
        studio = ContentStudio(db=busy.db)

        began = time.perf_counter()
        answer = asyncio.run(studio.write_reel_scripts.__wrapped__(
            studio, None, topic="why home espresso tastes sour", count=1))
        queued_in = (time.perf_counter() - began) * 1000
        print(f"\n  queue-and-answer under load: {queued_in:.2f}ms")

        assert "Started" in answer
        reference = answer.split("Reference ")[1].split(".")[0].strip()

        deadline = time.monotonic() + 30
        job = None
        while time.monotonic() < deadline:
            job = busy.db.job(reference)
            if job and job["status"] in ("done", "failed"):
                break
            time.sleep(0.05)

        assert job is not None and job["status"] == "done", job
        assert job["result"]["count"] == 1
        assert busy.db.assets(reference, kind="script")

        said = asyncio.run(studio.read_reel_script.__wrapped__(
            studio, None, reference=reference))
        assert "A hook that earns" in said
        assert pipeline.readiness()["style"]["placeholder"] is True


class TestTheRoutineTickerCostsTheVoiceNothing:
    """The standing routines add a second thing polling the same database.

    A scheduler is a loop that asks "is anything due" forever. The content
    engine's own lesson was that the cost of touching SQLite is not the write,
    it is the lock - so a ticker doing an indexed SELECT every few seconds
    beside a busy writer is worth measuring rather than assuming, and it is
    measured here at a tick far faster than the one that ships.
    """

    def ticking(self, tmp_path, host, every=0.05):
        from routines.engine import RoutineEngine
        from routines.store import RoutineStore

        engine = RoutineEngine(store=RoutineStore(tmp_path / "jarvis.db"),
                               host=host, tick_seconds=every)
        for index in range(6):
            engine.save({"name": f"Routine {index}", "action": "note",
                         "schedule": "@daily", "instruction": "nothing"})
        engine.start(seed=False)
        return engine

    def test_the_tools_stay_inside_the_budget_while_it_ticks(
            self, busy, tmp_path, monkeypatch):
        from content import pipeline

        monkeypatch.setattr(pipeline, "run_script_job", lambda *a, **k: None)
        engine = self.ticking(tmp_path, busy.host)
        try:
            studio = ContentStudio(db=busy.db)
            ref = busy.db.create_job("espresso")
            busy.db.finish_job(ref, result={"count": 1, "scripts": [SCRIPT]})
            calls = [
                lambda i: studio.write_reel_scripts.__wrapped__(
                    studio, None, topic=f"topic {i}", count=1),
                lambda i: studio.content_engine_status.__wrapped__(studio, None),
                lambda i: studio.read_reel_script.__wrapped__(
                    studio, None, reference=ref),
            ]
            latencies, _ = measure_on_one_loop(calls)
        finally:
            engine.stop()
        report = summarise(latencies, "all three tools, routines ticking")
        assert report["p95"] < BUDGET_MS, f"p95 was {report['p95']:.1f}ms"

    def test_the_voice_loop_is_not_starved_while_it_ticks(
            self, busy, tmp_path, monkeypatch):
        from content import pipeline

        monkeypatch.setattr(pipeline, "run_script_job", lambda *a, **k: None)
        engine = self.ticking(tmp_path, busy.host)
        try:
            studio = ContentStudio(db=busy.db)
            calls = [lambda i: studio.content_engine_status.__wrapped__(
                studio, None)]
            _, lags = measure_on_one_loop(calls)
        finally:
            engine.stop()
        report = summarise(lags, "voice loop lag, routines ticking")
        assert report["p95"] < LOOP_LAG_MS, \
            f"the voice loop ran {report['p95']:.1f}ms late at p95"

    def test_a_routine_firing_does_not_pause_the_voice(self, busy, tmp_path,
                                                       monkeypatch):
        """Not just idle ticking - one actually running while the tools are
        called, which is what half past seven looks like if he is up early."""
        from content import pipeline

        monkeypatch.setattr(pipeline, "run_script_job", lambda *a, **k: None)
        engine = self.ticking(tmp_path, busy.host)
        try:
            saved = engine.save({"name": "Fires now", "action": "note",
                                 "schedule": "* * * * *",
                                 "instruction": "good morning"})
            engine.store.set_next_run(saved["id"], time_in_the_past())
            studio = ContentStudio(db=busy.db)
            calls = [lambda i: studio.content_engine_status.__wrapped__(
                studio, None)]
            # A longer window than the other cases: the routine's job queues
            # behind the load at the shipped concurrency of one, and a
            # measurement that ends before it runs would be measuring an idle
            # ticker again.
            latencies, lags = measure_on_one_loop(calls, rounds=240)
            # The job is queued behind the load, so the wait is for it to
            # reach a worker. What is asserted is that it really ran - a
            # measurement taken beside a routine that never fired would prove
            # nothing at all.
            #
            # The deadline is generous because it is not what this test is
            # about. 240 rounds of synthetic load queue ahead of the routine on
            # a host that runs one job at a time, so on a small shared CI
            # machine draining that backlog is minutes rather than seconds. A
            # tight deadline here fails the test for being slow, which is not
            # the property under test - the latency assertions below are - and
            # it reads as a regression in the voice path when it is nothing of
            # the kind.
            deadline = time.time() + FIRING_DEADLINE
            row = engine.store.get(saved["id"])
            while time.time() < deadline:
                row = engine.store.get(saved["id"])
                if row["runs"] >= 1 and row["last_output"]:
                    break
                time.sleep(0.05)
            assert row["last_output"] == "good morning", (
                f"the routine never fired within {FIRING_DEADLINE:.0f}s, so "
                f"these measurements were taken beside an idle ticker "
                f"(runs={row['runs']}, status={row['last_status']!r}, "
                f"error={row['last_error']!r})")
        finally:
            engine.stop()
        tools = summarise(latencies, "status tool while a routine fires")
        loop = summarise(lags, "voice loop lag while a routine fires")
        assert tools["p95"] < BUDGET_MS
        assert loop["p95"] < LOOP_LAG_MS


def time_in_the_past(seconds: int = 30):
    from datetime import datetime, timedelta, timezone

    return datetime.now(timezone.utc) - timedelta(seconds=seconds)
