"""The routines table: what survives a restart, and what stops a double fire.

The whole reason routines live in SQLite rather than in a JSON file is that
two processes look at them - the voice agent, which fires them, and the
settings API, which edits them and can fire one on demand. So the interesting
tests here are not "can it store a row". They are: what happens when both
processes decide the same routine is due, what happens when the process dies
between the due time and the run, and what happens when eight threads write at
once.
"""

import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest

from routines.cron import ScheduleError
from routines.store import (
    KEEP_RUNS,
    STATUS_DONE,
    STATUS_FAILED,
    RoutineStore,
    from_dt,
    slug,
    to_dt,
)


@pytest.fixture
def store(tmp_path):
    return RoutineStore(tmp_path / "jarvis.db")


def a_routine(**over):
    base = {"name": "Good morning briefing", "action": "briefing",
            "schedule": "07:30"}
    base.update(over)
    return base


class TestSaving:

    def test_a_routine_comes_back_as_it_went_in(self, store):
        saved = store.save(a_routine(instruction="mention the weather"))
        assert saved["name"] == "Good morning briefing"
        assert saved["schedule"] == "30 7 * * *"
        assert saved["instruction"] == "mention the weather"
        assert saved["enabled"] == 1

    def test_the_id_comes_from_the_name(self, store):
        assert store.save(a_routine())["id"] == "good-morning-briefing"

    @pytest.mark.parametrize("name,expected", [
        ("Good morning briefing", "good-morning-briefing"),
        ("  Spaces   everywhere  ", "spaces-everywhere"),
        ("Ünïcode & symbols!", "n-code-symbols"),
        ("-----", "routine"),
    ])
    def test_ids_are_readable_and_never_empty(self, name, expected):
        assert slug(name) == expected

    def test_saving_the_same_name_twice_edits_rather_than_duplicates(self, store):
        store.save(a_routine())
        store.save(a_routine(schedule="08:00"))
        rows = store.all()
        assert len(rows) == 1
        assert rows[0]["schedule"] == "0 8 * * *"

    def test_the_schedule_is_validated_at_the_door(self, store):
        """Not at four in the morning, when nobody is looking at a screen."""
        with pytest.raises(ScheduleError):
            store.save(a_routine(schedule="half past whenever"))
        assert store.all() == []

    def test_a_routine_needs_a_name(self, store):
        with pytest.raises(ValueError):
            store.save({"name": "", "schedule": "07:30"})

    def test_friendly_schedules_are_stored_as_cron(self, store):
        assert store.save(a_routine(schedule="every 15 minutes"))["schedule"] \
            == "*/15 * * * *"

    def test_fields_the_panel_sends_back_unchanged_are_ignored(self, store):
        """The panel hands back the row it was given, counters and all. That
        must not let it rewrite the run count."""
        saved = store.save(a_routine())
        store.finish_run(store.start_run(saved["id"]), saved["id"], STATUS_DONE)
        again = store.save({**store.get(saved["id"]), "runs": 999})
        assert again["runs"] == 1

    def test_deleting_takes_the_history_with_it(self, store):
        saved = store.save(a_routine())
        store.finish_run(store.start_run(saved["id"]), saved["id"], STATUS_DONE)
        assert store.delete(saved["id"]) is True
        assert store.runs(saved["id"]) == []
        assert store.delete(saved["id"]) is False

    def test_turning_one_off(self, store):
        saved = store.save(a_routine())
        assert store.set_enabled(saved["id"], False) is True
        assert store.get(saved["id"])["enabled"] == 0
        assert store.all(enabled_only=True) == []

    def test_turning_off_something_that_is_not_there(self, store):
        assert store.set_enabled("nothing-here", False) is False


class TestDueAndClaiming:

    def test_due_finds_what_is_in_the_past(self, store):
        saved = store.save(a_routine())
        past = datetime.now(timezone.utc) - timedelta(minutes=1)
        store.set_next_run(saved["id"], past)
        assert [row["id"] for row in store.due()] == [saved["id"]]

    def test_due_ignores_the_future(self, store):
        saved = store.save(a_routine())
        store.set_next_run(saved["id"],
                           datetime.now(timezone.utc) + timedelta(hours=1))
        assert store.due() == []

    def test_due_ignores_what_is_switched_off(self, store):
        saved = store.save(a_routine(enabled=False))
        store.set_next_run(saved["id"],
                           datetime.now(timezone.utc) - timedelta(minutes=1))
        assert store.due() == []

    def test_due_ignores_a_routine_that_was_never_armed(self, store):
        store.save(a_routine())
        assert store.due() == []

    def test_only_one_claim_can_win(self, store):
        """This is the entire double-fire protection, so it gets its own test.

        Two readers both see the same due time. Both try to move it on. The
        second one's WHERE clause no longer matches, so it is told no and does
        nothing - rather than running the morning briefing a second time.
        """
        saved = store.save(a_routine())
        due = datetime.now(timezone.utc) - timedelta(minutes=1)
        store.set_next_run(saved["id"], due)
        seen = from_dt(due)
        later = datetime.now(timezone.utc) + timedelta(days=1)

        assert store.claim(saved["id"], seen, later) is True
        assert store.claim(saved["id"], seen, later) is False

    def test_a_claim_on_a_disabled_routine_loses(self, store):
        saved = store.save(a_routine())
        due = datetime.now(timezone.utc) - timedelta(minutes=1)
        store.set_next_run(saved["id"], due)
        store.set_enabled(saved["id"], False)
        assert store.claim(saved["id"], from_dt(due), None) is False

    def test_claiming_under_eight_threads_still_yields_one_winner(self, store):
        """The same race, from eight directions, against one SQLite file."""
        saved = store.save(a_routine())
        due = datetime.now(timezone.utc) - timedelta(minutes=1)
        store.set_next_run(saved["id"], due)
        seen = from_dt(due)
        later = datetime.now(timezone.utc) + timedelta(days=1)

        wins: list[bool] = []
        lock = threading.Lock()
        start = threading.Barrier(8)

        def race():
            # Its own store, so its own connection - the way two processes see
            # it, rather than two threads sharing one handle.
            mine = RoutineStore(store.path())
            start.wait(timeout=10)
            won = mine.claim(saved["id"], seen, later)
            with lock:
                wins.append(won)
            mine.close()

        threads = [threading.Thread(target=race) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        assert sum(wins) == 1

    def test_a_claim_can_clear_the_next_run_entirely(self, store):
        saved = store.save(a_routine())
        due = datetime.now(timezone.utc) - timedelta(minutes=1)
        store.set_next_run(saved["id"], due)
        assert store.claim(saved["id"], from_dt(due), None) is True
        assert store.get(saved["id"])["next_run_at"] is None


class TestRuns:

    def test_a_run_records_what_happened(self, store):
        saved = store.save(a_routine())
        run_id = store.start_run(saved["id"], trigger="manual")
        store.finish_run(run_id, saved["id"], STATUS_DONE, output="Good morning.")

        row = store.runs(saved["id"])[0]
        assert row["status"] == STATUS_DONE
        assert row["output"] == "Good morning."
        assert row["trigger"] == "manual"
        assert row["finished_at"]

    def test_the_summary_lands_on_the_routine_too(self, store):
        saved = store.save(a_routine())
        store.finish_run(store.start_run(saved["id"]), saved["id"],
                         STATUS_DONE, output="Morning.")
        row = store.get(saved["id"])
        assert row["last_status"] == STATUS_DONE
        assert row["last_output"] == "Morning."
        assert row["runs"] == 1
        assert row["failures"] == 0

    def test_failures_are_counted_separately(self, store):
        saved = store.save(a_routine())
        store.finish_run(store.start_run(saved["id"]), saved["id"],
                         STATUS_FAILED, error="no key")
        row = store.get(saved["id"])
        assert (row["runs"], row["failures"]) == (1, 1)
        assert row["last_error"] == "no key"

    def test_history_is_trimmed_but_not_emptied(self, store):
        saved = store.save(a_routine())
        for index in range(KEEP_RUNS + 10):
            store.finish_run(store.start_run(saved["id"]), saved["id"],
                             STATUS_DONE, output=str(index))
        rows = store.runs(saved["id"], limit=500)
        assert len(rows) == KEEP_RUNS
        # The newest survive, not the oldest.
        assert rows[0]["output"] == str(KEEP_RUNS + 9)

    def test_trimming_one_routine_leaves_another_alone(self, store):
        first = store.save(a_routine())
        second = store.save(a_routine(name="Evening"))
        store.finish_run(store.start_run(second["id"]), second["id"], STATUS_DONE)
        for _ in range(KEEP_RUNS + 5):
            store.finish_run(store.start_run(first["id"]), first["id"],
                             STATUS_DONE)
        assert len(store.runs(second["id"])) == 1

    def test_runs_across_everything(self, store):
        first = store.save(a_routine())
        second = store.save(a_routine(name="Evening"))
        store.finish_run(store.start_run(first["id"]), first["id"], STATUS_DONE)
        store.finish_run(store.start_run(second["id"]), second["id"], STATUS_DONE)
        assert len(store.runs(limit=10)) == 2


class TestDelivery:

    def test_something_with_output_waits_to_be_heard(self, store):
        saved = store.save(a_routine())
        store.finish_run(store.start_run(saved["id"]), saved["id"],
                         STATUS_DONE, output="Good morning, sir.")
        waiting = store.undelivered()
        assert [row["output"] for row in waiting] == ["Good morning, sir."]

    def test_a_failure_is_not_read_out(self, store):
        saved = store.save(a_routine())
        store.finish_run(store.start_run(saved["id"]), saved["id"],
                         STATUS_FAILED, error="no key")
        assert store.undelivered() == []

    def test_an_empty_result_is_not_read_out(self, store):
        saved = store.save(a_routine())
        store.finish_run(store.start_run(saved["id"]), saved["id"],
                         STATUS_DONE, output="")
        assert store.undelivered() == []

    def test_once_delivered_it_stays_delivered(self, store):
        saved = store.save(a_routine())
        store.finish_run(store.start_run(saved["id"]), saved["id"],
                         STATUS_DONE, output="Morning.")
        waiting = store.undelivered()
        assert store.mark_delivered([row["id"] for row in waiting]) == 1
        assert store.undelivered() == []

    def test_marking_nothing_is_not_an_error(self, store):
        assert store.mark_delivered([]) == 0


class TestPersistence:

    def test_a_second_store_on_the_same_file_sees_everything(self, store):
        saved = store.save(a_routine())
        store.set_next_run(saved["id"], datetime(2027, 1, 1, tzinfo=timezone.utc))
        fresh = RoutineStore(store.path())
        row = fresh.get(saved["id"])
        assert row["schedule"] == "30 7 * * *"
        assert to_dt(row["next_run_at"]).year == 2027

    def test_timestamps_survive_the_round_trip_as_utc(self, store):
        saved = store.save(a_routine())
        when = datetime(2026, 9, 18, 7, 30, tzinfo=timezone.utc)
        store.set_next_run(saved["id"], when)
        assert to_dt(store.get(saved["id"])["next_run_at"]) == when

    def test_rubbish_in_a_timestamp_is_not_an_exception(self):
        assert to_dt("not a date") is None
        assert to_dt("") is None
        assert to_dt(None) is None

    def test_a_naive_timestamp_is_read_as_utc(self):
        assert to_dt("2026-09-18T07:30:00").tzinfo is timezone.utc

    def test_the_tables_are_additive(self, store):
        """An older build opening this database must see what it always saw."""
        store.save(a_routine())
        names = {row[0] for row in store.connection().execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"routines", "routine_runs"} <= names

    def test_eight_threads_writing_runs_at_once(self, store):
        """WAL plus a busy timeout, under the load a briefing plus a render
        plus the settings panel would actually produce."""
        saved = store.save(a_routine())
        errors: list[Exception] = []
        start = threading.Barrier(8)

        def write():
            mine = RoutineStore(store.path())
            try:
                start.wait(timeout=10)
                for index in range(5):
                    mine.finish_run(mine.start_run(saved["id"]), saved["id"],
                                    STATUS_DONE, output=str(index))
            except Exception as exc:                # pragma: no cover
                errors.append(exc)
            finally:
                mine.close()

        threads = [threading.Thread(target=write) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        assert not errors
        assert store.get(saved["id"])["runs"] == 40

    def test_it_never_holds_one_connection_across_threads(self, store):
        """Sharing a sqlite3 connection between threads is the bug that shows
        up once a fortnight under load, so the store is asserted not to."""
        seen: list[sqlite3.Connection] = []
        store.connection()
        seen.append(store.connection())

        def other():
            seen.append(store.connection())

        thread = threading.Thread(target=other)
        thread.start()
        thread.join(timeout=10)
        assert seen[0] is not seen[1]
