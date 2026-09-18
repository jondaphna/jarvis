"""The engine: arming, firing, catching up, and surviving a restart.

These are the tests that answer "is this actually operational", as opposed to
"does the code look right". Two of them matter more than the rest:

* `test_a_missed_briefing_runs_when_the_machine_comes_back` builds a routine
  that was due while the process was down, throws the engine away, builds a
  new one on the same database, and asserts the briefing runs. That is the
  restart, in a test.
* `test_two_engines_on_one_database_fire_it_once` runs two engines - the voice
  agent and the settings API, in effect - against the same file at the same
  moment, and asserts one run.

Everything here uses a real WorkerHost, on a real thread, with a real event
loop. Faking it would test the fake.
"""

import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

import workers
from routines.engine import DEFAULT_GRACE, RoutineEngine
from routines.store import (
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_SKIPPED,
    RoutineStore,
    from_dt,
)

#: Long enough that a loaded machine does not fail the test, short enough that
#: a real hang is still a failure rather than a coffee break.
WAIT = 10.0


@pytest.fixture
def host():
    made = workers.WorkerHost(name="test-routines", concurrency=1)
    made.start()
    yield made
    made.stop(timeout=3.0)


@pytest.fixture
def db(tmp_path):
    return tmp_path / "jarvis.db"


@pytest.fixture
def engine(db, host):
    return RoutineEngine(store=RoutineStore(db), host=host, tick_seconds=1.0)


def a_note(engine, **over):
    """A routine that needs nothing: no key, no network, no model."""
    payload = {"name": "Say a thing", "action": "note", "schedule": "07:30",
               "instruction": "This is the routine speaking."}
    payload.update(over)
    return engine.save(payload)


def make_due(engine, routine_id, seconds_ago=60):
    when = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    engine.store.set_next_run(routine_id, when)
    return from_dt(when)


def wait_for(predicate, timeout=WAIT):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    return None


class TestArming:

    def test_a_new_routine_is_armed_as_it_is_saved(self, engine):
        saved = a_note(engine)
        assert saved["next_run_at"]

    def test_the_armed_time_matches_the_schedule(self, engine):
        saved = a_note(engine, schedule="07:30")
        assert saved["next_run_at"].endswith(":00+00:00")

    def test_editing_the_schedule_re_arms_it(self, engine):
        """Otherwise it fires once at the time you just changed away from."""
        first = a_note(engine, schedule="23:00")
        second = a_note(engine, schedule="06:00")
        assert first["next_run_at"] != second["next_run_at"]

    def test_switching_one_off_disarms_it(self, engine):
        saved = a_note(engine)
        engine.set_enabled(saved["id"], False)
        assert engine.store.get(saved["id"])["next_run_at"] is None

    def test_switching_it_back_on_arms_it_from_now(self, engine):
        saved = a_note(engine)
        engine.set_enabled(saved["id"], False)
        engine.set_enabled(saved["id"], True)
        assert engine.store.get(saved["id"])["next_run_at"]

    def test_enabling_something_that_is_not_there(self, engine):
        assert engine.set_enabled("no-such-routine", True) is False

    def test_arm_leaves_a_time_that_is_already_set(self, engine):
        """Including one in the past - that is what makes catch-up possible."""
        saved = a_note(engine)
        due = make_due(engine, saved["id"])
        assert engine.arm() == 0
        assert engine.store.get(saved["id"])["next_run_at"] == due

    def test_arm_fills_in_what_is_missing(self, engine):
        saved = a_note(engine)
        engine.store.set_next_run(saved["id"], None)
        assert engine.arm() == 1
        assert engine.store.get(saved["id"])["next_run_at"]

    def test_a_broken_schedule_is_not_armed_and_does_not_raise(self, engine):
        saved = a_note(engine)
        engine.store.connection().execute(
            "UPDATE routines SET schedule='nonsense', next_run_at=NULL WHERE id=?",
            (saved["id"],))
        engine.store.connection().commit()
        assert engine.arm() == 0
        assert engine.store.get(saved["id"])["next_run_at"] is None


class TestSeeding:

    def test_a_fresh_database_gets_the_starting_routines(self, engine):
        assert engine.seed_defaults() == 3
        ids = {row["id"] for row in engine.store.all()}
        assert {"good-morning", "morning-system-check"} <= ids

    def test_the_morning_ones_are_on_and_the_evening_one_is_not(self, engine):
        engine.seed_defaults()
        by_id = {row["id"]: row for row in engine.store.all()}
        assert by_id["good-morning"]["enabled"] == 1
        assert by_id["morning-system-check"]["enabled"] == 1
        assert by_id["evening-wrap-up"]["enabled"] == 0

    def test_seeding_twice_adds_nothing(self, engine):
        engine.seed_defaults()
        assert engine.seed_defaults() == 0

    def test_deleting_one_keeps_it_deleted(self, engine):
        """Seeding asks whether the table has ever had rows, not whether these
        particular ids are present - so a routine you deleted stays deleted."""
        engine.seed_defaults()
        engine.store.delete("good-morning")
        engine.seed_defaults()
        assert engine.store.get("good-morning") is None

    def test_the_seeded_ones_are_armed(self, engine):
        engine.seed_defaults()
        armed = [row for row in engine.store.all(enabled_only=True)
                 if row["next_run_at"]]
        assert len(armed) == 2


class TestFiring:

    def test_nothing_due_fires_nothing(self, engine):
        a_note(engine)
        assert engine.tick() == []

    def test_something_due_runs_and_records_what_it_said(self, engine):
        saved = a_note(engine)
        make_due(engine, saved["id"])

        assert len(engine.tick()) == 1
        row = wait_for(lambda: engine.store.get(saved["id"])
                       if engine.store.get(saved["id"])["last_status"] else None)
        assert row["last_status"] == STATUS_DONE
        assert row["last_output"] == "This is the routine speaking."
        assert row["runs"] == 1

    def test_the_run_history_gets_a_row(self, engine):
        saved = a_note(engine)
        due = make_due(engine, saved["id"])
        engine.tick()
        run = wait_for(lambda: (engine.store.runs(saved["id"]) or [None])[0])
        assert run["trigger"] == "schedule"
        assert run["due_at"] == due

    def test_firing_moves_the_next_run_forward(self, engine):
        saved = a_note(engine)
        due = make_due(engine, saved["id"])
        engine.tick()
        assert engine.store.get(saved["id"])["next_run_at"] != due

    def test_it_does_not_fire_the_same_due_time_twice(self, engine):
        saved = a_note(engine)
        make_due(engine, saved["id"])
        first = engine.tick()
        second = engine.tick()
        assert len(first) == 1
        assert second == []

    def test_run_now_fires_regardless_of_the_schedule(self, engine):
        saved = a_note(engine, schedule="0 0 1 1 *")
        assert engine.run_now(saved["id"])
        run = wait_for(lambda: (engine.store.runs(saved["id"]) or [None])[0])
        assert run["trigger"] == "manual"

    def test_run_now_on_something_that_is_not_there(self, engine):
        with pytest.raises(ValueError):
            engine.run_now("no-such-routine")

    def test_run_now_does_not_wait_for_the_work(self, engine):
        """The settings panel presses a button; it must not hang on a render."""
        saved = engine.save({"name": "Slow", "action": "note",
                             "schedule": "@daily", "instruction": "x"})
        started = time.time()
        engine.run_now(saved["id"])
        assert time.time() - started < 1.0

    def test_several_due_at_once_all_fire(self, engine):
        ids = []
        for index in range(3):
            saved = a_note(engine, name=f"Routine {index}")
            make_due(engine, saved["id"])
            ids.append(saved["id"])
        assert len(engine.tick()) == 3
        for routine_id in ids:
            assert wait_for(lambda rid=routine_id:
                            engine.store.get(rid)["last_status"] == STATUS_DONE)


class TestWhenItGoesWrong:

    def test_an_action_that_throws_is_recorded_not_lost(self, engine):
        saved = a_note(engine)
        engine.store.connection().execute(
            "UPDATE routines SET action='no-such-action' WHERE id=?",
            (saved["id"],))
        engine.store.connection().commit()
        make_due(engine, saved["id"])
        engine.tick()

        row = wait_for(lambda: engine.store.get(saved["id"])
                       if engine.store.get(saved["id"])["last_status"] else None)
        assert row["last_status"] == STATUS_FAILED
        assert "isn't an action" in row["last_error"]

    def test_one_routine_failing_does_not_stop_the_next(self, engine):
        broken = a_note(engine, name="Broken")
        engine.store.connection().execute(
            "UPDATE routines SET action='nope' WHERE id=?", (broken["id"],))
        engine.store.connection().commit()
        working = a_note(engine, name="Working")
        make_due(engine, broken["id"])
        make_due(engine, working["id"])

        engine.tick()
        assert wait_for(
            lambda: engine.store.get(working["id"])["last_status"] == STATUS_DONE)

    def test_a_failure_is_not_read_out_to_the_user(self, engine):
        saved = a_note(engine)
        engine.store.connection().execute(
            "UPDATE routines SET action='nope' WHERE id=?", (saved["id"],))
        engine.store.connection().commit()
        make_due(engine, saved["id"])
        engine.tick()
        wait_for(lambda: engine.store.get(saved["id"])["last_status"])
        assert engine.pending_block() == ""

    def test_a_host_that_will_not_start_records_the_failure(self, db):
        class DeadHost:
            def submit(self, *args, **kwargs):
                return None

        engine = RoutineEngine(store=RoutineStore(db), host=DeadHost())
        saved = a_note(engine)
        make_due(engine, saved["id"])
        engine.tick()
        row = engine.store.get(saved["id"])
        assert row["last_status"] == STATUS_FAILED
        assert "wouldn't start" in row["last_error"]


class TestCatchingUp:

    def test_a_missed_briefing_runs_when_the_machine_comes_back(self, db, host):
        """The restart, in a test.

        A routine is due while nothing is running. The engine is thrown away -
        which is what a reboot is, from the database's point of view - and a
        new one is built on the same file. The briefing still happens.
        """
        first = RoutineEngine(store=RoutineStore(db), host=host)
        saved = a_note(first)
        make_due(first, saved["id"], seconds_ago=1800)
        first.store.close()
        del first

        second = RoutineEngine(store=RoutineStore(db), host=host)
        assert second.arm() == 0            # the missed time is left in place
        assert len(second.tick()) == 1
        assert wait_for(
            lambda: second.store.get(saved["id"])["last_status"] == STATUS_DONE)

    def test_something_missed_by_days_is_skipped_when_told_not_to_catch_up(
            self, engine):
        saved = a_note(engine, catch_up=False, grace_seconds=600)
        make_due(engine, saved["id"], seconds_ago=3 * 86400)

        assert engine.tick() == []
        row = engine.store.get(saved["id"])
        assert row["last_status"] == STATUS_SKIPPED
        assert "missed by" in row["last_error"]

    def test_a_skip_still_moves_the_schedule_on(self, engine):
        saved = a_note(engine, catch_up=False, grace_seconds=600)
        make_due(engine, saved["id"], seconds_ago=3 * 86400)
        engine.tick()
        assert engine.store.get(saved["id"])["next_run_at"]
        assert engine.tick() == []

    def test_inside_the_grace_window_it_runs_even_without_catch_up(self, engine):
        saved = a_note(engine, catch_up=False, grace_seconds=3600)
        make_due(engine, saved["id"], seconds_ago=60)
        assert len(engine.tick()) == 1

    def test_catch_up_runs_it_however_late(self, engine):
        saved = a_note(engine, catch_up=True, grace_seconds=60)
        make_due(engine, saved["id"], seconds_ago=30 * 86400)
        assert len(engine.tick()) == 1

    def test_the_default_grace_covers_a_laptop_that_was_shut(self, engine):
        assert DEFAULT_GRACE >= 3600

    def test_a_missed_run_fires_once_not_once_per_missed_slot(self, engine):
        """Coalescing. A briefing missed for a week is one briefing, not seven."""
        saved = a_note(engine, schedule="@hourly")
        make_due(engine, saved["id"], seconds_ago=7 * 86400)
        assert len(engine.tick()) == 1
        assert engine.tick() == []


class TestTwoOfThem:

    def test_two_engines_on_one_database_fire_it_once(self, db, host):
        """The voice agent and the settings API, both looking at the same row.

        Without the compare-and-swap claim, both would see the routine as due
        and both would run it, and the morning briefing would be delivered
        twice - which is the single most visible way a scheduler can be wrong.
        """
        setup = RoutineEngine(store=RoutineStore(db), host=host)
        saved = a_note(setup)
        make_due(setup, saved["id"])

        engines = [RoutineEngine(store=RoutineStore(db), host=host)
                   for _ in range(2)]
        results: list[list[str]] = []
        lock = threading.Lock()
        start = threading.Barrier(len(engines))

        def race(runner):
            start.wait(timeout=10)
            fired = runner.tick()
            with lock:
                results.append(fired)

        threads = [threading.Thread(target=race, args=(runner,))
                   for runner in engines]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        assert sum(len(fired) for fired in results) == 1
        wait_for(lambda: setup.store.get(saved["id"])["runs"] == 1)
        assert setup.store.get(saved["id"])["runs"] == 1

    def test_eight_ticks_at_once_fire_it_once(self, db, host):
        setup = RoutineEngine(store=RoutineStore(db), host=host)
        saved = a_note(setup)
        make_due(setup, saved["id"])

        fired: list[int] = []
        lock = threading.Lock()
        start = threading.Barrier(8)

        def race():
            runner = RoutineEngine(store=RoutineStore(db), host=host)
            start.wait(timeout=10)
            count = len(runner.tick())
            with lock:
                fired.append(count)

        threads = [threading.Thread(target=race) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        assert sum(fired) == 1


class TestTheTicker:

    def test_starting_it_arms_seeds_and_ticks(self, engine):
        assert engine.start() is True
        assert engine.started is True
        # Seeding and arming happen on the worker, not on the caller's thread,
        # so this waits rather than asserting into a race.
        assert wait_for(lambda: engine.store.all())

    def test_starting_it_does_no_database_work_on_the_caller(self, engine,
                                                             monkeypatch):
        """`start()` is called as a call opens, on the voice thread."""
        def explode(*args, **kwargs):
            raise AssertionError("start() touched the database")

        monkeypatch.setattr(engine.store, "connection", explode)
        assert engine.start() is True

    def test_preparing_recovers_seeds_and_arms(self, engine):
        report = engine.prepare()
        assert report["seeded"] == 3
        assert report["armed"] == 0        # seeding arms them as it saves them
        assert report["recovered"] == 0

    def test_starting_twice_is_a_no_op(self, engine):
        engine.start()
        assert engine.start() is True

    def test_the_ticker_fires_something_due_without_being_asked(self, engine):
        """The end-to-end proof: nothing calls tick, and it still runs."""
        saved = a_note(engine)
        engine.start(seed=False)
        make_due(engine, saved["id"])
        assert wait_for(
            lambda: engine.store.get(saved["id"])["last_status"] == STATUS_DONE,
            timeout=15)

    def test_the_ticker_does_not_occupy_a_worker_slot(self, engine, host):
        """At concurrency one, a ticker in the queue would be the whole pool.

        So it is spawned beside the queue rather than submitted to it, and an
        ordinary job must still run while it ticks.
        """
        engine.start(seed=False)
        done = threading.Event()
        host.submit(done.set, name="ordinary job")
        assert done.wait(timeout=WAIT)

    def test_stopping_it(self, engine):
        engine.start(seed=False)
        engine.stop()
        assert engine.started is False

    def test_stopping_one_that_never_started(self, engine):
        engine.stop()
        assert engine.started is False


class TestReadingItBack:

    def test_the_listing_says_the_schedule_in_words(self, engine):
        a_note(engine, schedule="07:30")
        assert engine.listing()[0]["schedule_in_words"] == "every day at 07:30"

    def test_the_listing_survives_a_broken_schedule(self, engine):
        saved = a_note(engine)
        engine.store.connection().execute(
            "UPDATE routines SET schedule='rubbish' WHERE id=?", (saved["id"],))
        engine.store.connection().commit()
        assert "broken schedule" in engine.listing()[0]["schedule_in_words"]

    def test_the_listing_names_the_action_in_words(self, engine):
        a_note(engine)
        assert engine.listing()[0]["action_label"] == "Remind me of something"

    def test_the_summary_counts_what_matters(self, engine):
        a_note(engine, name="On")
        off = a_note(engine, name="Off")
        engine.set_enabled(off["id"], False)
        state = engine.summary()
        assert state["total"] == 2
        assert state["enabled"] == 1
        assert state["armed"] == 1
        assert state["next"]["name"] == "On"

    def test_the_summary_names_what_is_failing(self, engine):
        saved = a_note(engine)
        engine.store.finish_run(engine.store.start_run(saved["id"]),
                                saved["id"], STATUS_FAILED, error="nope")
        assert engine.summary()["failing"] == ["Say a thing"]

    def test_the_summary_on_an_empty_database(self, engine):
        state = engine.summary()
        assert state == {"total": 0, "enabled": 0, "armed": 0, "next": None,
                         "failing": [], "ticking": False}


class TestDelivery:

    def test_what_ran_overnight_is_waiting_in_the_prompt(self, engine):
        saved = a_note(engine)
        make_due(engine, saved["id"])
        engine.tick()
        wait_for(lambda: engine.store.get(saved["id"])["last_status"])

        block = engine.pending_block()
        assert "While you were away" in block
        assert "This is the routine speaking." in block

    def test_it_is_only_said_once(self, engine):
        saved = a_note(engine)
        make_due(engine, saved["id"])
        engine.tick()
        wait_for(lambda: engine.store.get(saved["id"])["last_status"])
        assert engine.pending_block()
        assert engine.pending_block() == ""

    def test_reading_without_marking_leaves_it_waiting(self, engine):
        saved = a_note(engine)
        make_due(engine, saved["id"])
        engine.tick()
        wait_for(lambda: engine.store.get(saved["id"])["last_status"])
        assert engine.pending_block(mark=False)
        assert engine.pending_block(mark=False)

    def test_nothing_waiting_is_an_empty_block_not_a_heading(self, engine):
        a_note(engine)
        assert engine.pending_block() == ""

    def test_the_standing_list_is_in_the_prompt(self, engine):
        a_note(engine, name="Good morning briefing", schedule="07:30")
        block = engine.routines_block()
        assert "Good morning briefing" in block
        assert "every day at 07:30" in block

    def test_something_switched_off_is_not_in_the_list(self, engine):
        saved = a_note(engine, name="Off one")
        engine.set_enabled(saved["id"], False)
        assert engine.routines_block() == ""

    def test_the_blocks_never_raise_on_a_broken_database(self, tmp_path, host):
        broken = tmp_path / "not-a-database.db"
        broken.write_text("this is not sqlite")
        engine = RoutineEngine(store=RoutineStore(broken), host=host)
        assert engine.pending_block() == ""
        assert engine.routines_block() == ""
