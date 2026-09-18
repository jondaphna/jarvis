"""One connection per file per thread, and knowing whose process wrote a row.

Two things live here that the rest of the build leans on without saying so.

The pool is the reason the worker thread holds one handle to `jarvis.db`
rather than two. That is not a dramatic saving, but the one bug this codebase
has actually been bitten by was SQLite lock contention on the voice path, and
halving the number of readers and writers on one file is the same direction of
travel.

Process ownership is the reason a job abandoned by a crash can be told apart
from one another running copy is working on right now. A bare process id is not
enough - ids are reused - so the tag pairs it with the process start time.
"""

import sqlite3
import threading

import pytest

import db

SCHEMA_A = "CREATE TABLE IF NOT EXISTS thing_a (id INTEGER PRIMARY KEY, v TEXT);"
SCHEMA_B = "CREATE TABLE IF NOT EXISTS thing_b (id INTEGER PRIMARY KEY, v TEXT);"


@pytest.fixture(autouse=True)
def clean():
    db.close_all()
    yield
    db.close_all()


class TestSharing:

    def test_the_same_file_twice_on_one_thread_is_one_connection(self, tmp_path):
        first = db.connect(tmp_path / "jarvis.db", SCHEMA_A)
        second = db.connect(tmp_path / "jarvis.db", SCHEMA_B)
        assert first is second

    def test_both_schemas_are_applied_to_it(self, tmp_path):
        db.connect(tmp_path / "jarvis.db", SCHEMA_A)
        conn = db.connect(tmp_path / "jarvis.db", SCHEMA_B)
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"thing_a", "thing_b"} <= tables

    def test_a_schema_is_run_once_not_on_every_call(self, tmp_path):
        """Written as a schema that cannot survive being run twice, because
        counting calls on a sqlite3.Connection is not possible: it is a C type
        with no instance dictionary, so the attribute cannot be replaced."""
        once = "CREATE TABLE thing_once (id INTEGER PRIMARY KEY);"
        db.connect(tmp_path / "jarvis.db", once)
        for _ in range(5):
            db.connect(tmp_path / "jarvis.db", once)   # would raise if re-run

    def test_different_files_get_different_connections(self, tmp_path):
        assert db.connect(tmp_path / "one.db", SCHEMA_A) \
            is not db.connect(tmp_path / "two.db", SCHEMA_A)

    def test_two_threads_never_share_one(self, tmp_path):
        """The bug that shows up once a fortnight under load and never here."""
        seen = []
        db.connect(tmp_path / "jarvis.db", SCHEMA_A)
        seen.append(db.connect(tmp_path / "jarvis.db", SCHEMA_A))

        def other():
            seen.append(db.connect(tmp_path / "jarvis.db", SCHEMA_A))

        thread = threading.Thread(target=other)
        thread.start()
        thread.join(timeout=10)
        assert len(seen) == 2
        assert seen[0] is not seen[1]

    def test_the_same_path_written_differently_is_the_same_file(self, tmp_path):
        direct = db.connect(tmp_path / "jarvis.db", SCHEMA_A)
        roundabout = db.connect(tmp_path / "sub" / ".." / "jarvis.db", SCHEMA_A)
        assert direct is roundabout

    def test_a_second_schema_for_the_same_file_still_runs(self, tmp_path):
        """Two callers, two schemas, one connection: the second caller's
        tables have to arrive even though the connection is not new."""
        db.connect(tmp_path / "jarvis.db", SCHEMA_A)
        conn = db.connect(tmp_path / "jarvis.db", SCHEMA_B)
        conn.execute("INSERT INTO thing_b(v) VALUES('x')")

    def test_it_creates_the_folder_it_is_pointed_at(self, tmp_path):
        target = tmp_path / "does" / "not" / "exist" / "jarvis.db"
        db.connect(target, SCHEMA_A)
        assert target.exists()


class TestPragmas:

    def test_it_is_in_write_ahead_logging_mode(self, tmp_path):
        conn = db.connect(tmp_path / "jarvis.db", SCHEMA_A)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"

    def test_contention_is_a_wait_rather_than_an_exception(self, tmp_path):
        conn = db.connect(tmp_path / "jarvis.db", SCHEMA_A)
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] \
            == db.BUSY_TIMEOUT_MS

    def test_rows_come_back_addressable_by_name(self, tmp_path):
        conn = db.connect(tmp_path / "jarvis.db", SCHEMA_A)
        conn.execute("INSERT INTO thing_a(v) VALUES('x')")
        assert conn.execute("SELECT v FROM thing_a").fetchone()["v"] == "x"


class TestLettingGo:

    def test_closing_this_thread_closes_what_it_opened(self, tmp_path):
        db.connect(tmp_path / "one.db", SCHEMA_A)
        db.connect(tmp_path / "two.db", SCHEMA_A)
        assert db.close_thread() == 2
        assert db.open_count() == 0

    def test_closing_one_file_leaves_the_other(self, tmp_path):
        one = db.connect(tmp_path / "one.db", SCHEMA_A)
        two = db.connect(tmp_path / "two.db", SCHEMA_A)
        assert db.close_path(tmp_path / "one.db") == 1
        assert db.connect(tmp_path / "two.db", SCHEMA_A) is two
        assert db.connect(tmp_path / "one.db", SCHEMA_A) is not one

    def test_closing_something_that_was_never_open(self, tmp_path):
        assert db.close_path(tmp_path / "never.db") == 0
        assert db.close_thread() == 0

    def test_a_closed_file_reopens_and_its_schema_comes_back(self, tmp_path):
        db.connect(tmp_path / "jarvis.db", SCHEMA_A)
        db.close_path(tmp_path / "jarvis.db")
        conn = db.connect(tmp_path / "jarvis.db", SCHEMA_A)
        assert conn.execute("SELECT count(*) FROM thing_a").fetchone()[0] == 0

    def test_a_schema_is_reapplied_to_a_connection_opened_again(self, tmp_path):
        """The marks have to die with the connection they were made against.
        Kept apart from it, a reopened file believes its tables are already
        there and the next query fails on a table nothing created."""
        first = tmp_path / "jarvis.db"
        db.connect(first, SCHEMA_A)
        db.close_all()
        first.unlink()                      # as if the file were never there
        conn = db.connect(first, SCHEMA_A)
        assert conn.execute("SELECT count(*) FROM thing_a").fetchone()[0] == 0

    def test_close_all_leaves_this_thread_able_to_open_again(self, tmp_path):
        """close_all() is called from shutdown and from every test; a thread
        that had a pool must not be left holding one nothing can reach."""
        db.connect(tmp_path / "one.db", SCHEMA_A)
        db.close_all()
        db.connect(tmp_path / "one.db", SCHEMA_A)
        assert db.open_count() == 1
        assert db.close_all() == 1

    def test_close_all_reaches_other_threads(self, tmp_path):
        ready = threading.Event()
        done = threading.Event()

        def other():
            db.connect(tmp_path / "other.db", SCHEMA_A)
            ready.set()
            done.wait(timeout=10)

        thread = threading.Thread(target=other)
        thread.start()
        assert ready.wait(timeout=10)
        db.connect(tmp_path / "mine.db", SCHEMA_A)
        assert db.open_count() == 2
        assert db.close_all() == 2
        assert db.open_count() == 0
        done.set()
        thread.join(timeout=10)


class TestMigrations:

    def test_a_column_added_later_reaches_an_existing_table(self, tmp_path):
        """CREATE TABLE IF NOT EXISTS is a no-op against a table that exists,
        so a column added to the schema string never reaches anybody who ran
        the previous version - which is everybody."""
        conn = db.connect(tmp_path / "jarvis.db", SCHEMA_A)
        conn.execute("INSERT INTO thing_a(v) VALUES('before')")
        conn.commit()

        added = db.ensure_columns(conn, "thing_a",
                                  {"owner": "TEXT NOT NULL DEFAULT ''"})
        assert added == ["owner"]
        row = conn.execute("SELECT v, owner FROM thing_a").fetchone()
        assert (row["v"], row["owner"]) == ("before", "")

    def test_it_is_a_no_op_the_second_time(self, tmp_path):
        conn = db.connect(tmp_path / "jarvis.db", SCHEMA_A)
        db.ensure_columns(conn, "thing_a", {"owner": "TEXT DEFAULT ''"})
        assert db.ensure_columns(conn, "thing_a", {"owner": "TEXT DEFAULT ''"}) == []

    def test_a_table_that_does_not_exist_is_not_an_error(self, tmp_path):
        conn = db.connect(tmp_path / "jarvis.db", SCHEMA_A)
        assert db.ensure_columns(conn, "nothing_here", {"x": "TEXT"}) == []

    def test_existing_rows_keep_their_data(self, tmp_path):
        conn = db.connect(tmp_path / "jarvis.db", SCHEMA_A)
        for value in ("one", "two", "three"):
            conn.execute("INSERT INTO thing_a(v) VALUES(?)", (value,))
        conn.commit()
        db.ensure_columns(conn, "thing_a", {"owner": "TEXT NOT NULL DEFAULT ''"})
        assert [row["v"] for row in conn.execute("SELECT v FROM thing_a")] \
            == ["one", "two", "three"]


class TestWhoseProcess:

    def test_this_process_is_alive(self):
        assert db.is_alive(db.process_tag()) is True

    def test_the_tag_carries_more_than_a_process_id(self):
        """Ids are reused. A tag that is only an id reads, after a reboot, as
        belonging to whatever happens to be running under that number."""
        pid, _, started = db.process_tag().partition(":")
        assert pid.isdigit()
        assert started.isdigit()

    @pytest.mark.parametrize("tag", ["", "   ", "not-a-tag", "abc:123", ":",
                                     "99999999", "99999999:0"])
    def test_a_tag_it_cannot_verify_is_not_alive(self, tag):
        """The safe direction: the cost of being wrong here is a job marked
        interrupted that was fine, against a job stuck on running forever."""
        assert db.is_alive(tag) is False

    def test_a_live_id_with_the_wrong_start_time_is_not_alive(self):
        """This is the reuse case, and the reason the start time is there."""
        pid, _, _ = db.process_tag().partition(":")
        assert db.is_alive(f"{pid}:1") is False

    def test_an_unreadable_tag_never_raises(self):
        for tag in (None, "x" * 500, "1:2:3:4"):
            assert db.is_alive(tag) in (True, False)


class TestItIsTheOneEverythingUses:

    def test_the_two_stores_share_a_handle(self, tmp_path):
        """The whole point: one connection to jarvis.db per thread, not one
        per store."""
        from content.store import ContentStore
        from routines.store import RoutineStore

        target = tmp_path / "jarvis.db"
        content = ContentStore(target)
        routines = RoutineStore(target)
        assert content.connection() is routines.connection()
        assert db.open_count() == 1

    def test_both_sets_of_tables_are_there(self, tmp_path):
        from content.store import ContentStore
        from routines.store import RoutineStore

        target = tmp_path / "jarvis.db"
        ContentStore(target).connection()
        conn = RoutineStore(target).connection()
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"content_jobs", "content_assets", "routines",
                "routine_runs"} <= tables

    def test_one_store_closing_does_not_break_the_other(self, tmp_path):
        from content.store import ContentStore
        from routines.store import RoutineStore

        target = tmp_path / "jarvis.db"
        content, routines = ContentStore(target), RoutineStore(target)
        content.create_job("a topic")
        content.close()
        # The routine store simply reopens; nothing it does raises.
        assert routines.all() == []
        assert len(content.recent_jobs()) == 1

    def test_a_connection_is_not_used_after_close(self, tmp_path):
        from content.store import ContentStore

        content = ContentStore(tmp_path / "jarvis.db")
        conn = content.connection()
        content.close()
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")
        assert content.connection() is not conn
