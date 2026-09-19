"""What a crash leaves behind, and how the next start clears it.

A job row is written before the work begins and updated when it ends. Between
those two writes the process can simply stop existing: a reboot, a power cut,
a laptop lid closed on a render. The row is then stuck on "running" for good.
Nothing crashes, nothing is logged, and the symptom is a status tool that
reports work in progress that nothing anywhere is doing, and a job reference
somebody was given which never resolves into anything.

The hard part is telling that apart from a job another running copy is working
on right now, which must not be touched. That is what the ownership tag is
for: a process id paired with that process's start time, so a row can say not
just "some process has this" but "*that* process, the one that started at that
second, has this" - and after a reboot the id may exist again while the pairing
never will.
"""

import os

import pytest

import db
from content.store import ContentStore
from routines.store import RoutineStore

#: A process that cannot exist: an id far beyond the usual maximum, paired
#: with a start time no machine has reached.
DEAD = "99999999:1700000000"


@pytest.fixture(autouse=True)
def clean():
    db.close_all()
    yield
    db.close_all()


@pytest.fixture
def content(tmp_path):
    return ContentStore(tmp_path / "jarvis.db")


@pytest.fixture
def routines(tmp_path):
    return RoutineStore(tmp_path / "jarvis.db")


def orphan_job(store, ref):
    """Make an existing job look like one a dead process was running."""
    conn = store.connection()
    conn.execute("UPDATE content_jobs SET status='running', owner=? WHERE ref=?",
                 (DEAD, ref))
    conn.commit()


class TestContentJobs:

    def test_a_job_left_by_a_dead_process_is_closed(self, content):
        ref = content.create_job("a topic")
        orphan_job(content, ref)

        assert content.recover_interrupted() == [ref]
        job = content.job(ref)
        assert job["status"] == "failed"
        assert "interrupted" in job["error"]
        assert job["finished_at"]

    def test_a_job_this_process_is_running_is_left_alone(self, content):
        """The one that must not go wrong. Recovery runs at start-up, and a
        second copy of the assistant starting must not fail the first one's
        work out from under it."""
        ref = content.create_job("a topic")
        content.start_job(ref, stage="writing")
        assert content.recover_interrupted() == []
        assert content.job(ref)["status"] == "running"

    def test_a_finished_job_is_not_reopened(self, content):
        ref = content.create_job("a topic")
        content.finish_job(ref, result={"scripts": []})
        assert content.recover_interrupted() == []
        assert content.job(ref)["status"] == "done"

    def test_a_queued_job_from_a_dead_process_counts_too(self, content):
        """Queued is as abandoned as running: the row was written by a process
        that is gone, so nothing is ever going to pick it up."""
        ref = content.create_job("a topic")
        conn = content.connection()
        conn.execute("UPDATE content_jobs SET owner=? WHERE ref=?", (DEAD, ref))
        conn.commit()
        assert content.recover_interrupted() == [ref]

    def test_an_untagged_row_from_an_older_build_is_closed(self, content):
        """Rows written before ownership existed have an empty tag. Unknown
        is treated as dead, which is the safe direction: the cost is a job
        marked interrupted that was fine, against one stuck forever."""
        ref = content.create_job("a topic")
        conn = content.connection()
        conn.execute("UPDATE content_jobs SET status='running', owner='' "
                     "WHERE ref=?", (ref,))
        conn.commit()
        assert content.recover_interrupted() == [ref]

    def test_nothing_to_do_is_not_an_error(self, content):
        assert content.recover_interrupted() == []

    def test_it_is_safe_to_run_twice(self, content):
        ref = content.create_job("a topic")
        orphan_job(content, ref)
        assert content.recover_interrupted() == [ref]
        assert content.recover_interrupted() == []

    def test_several_at_once(self, content):
        refs = [content.create_job(f"topic {n}") for n in range(4)]
        for ref in refs[:3]:
            orphan_job(content, ref)
        assert sorted(content.recover_interrupted()) == sorted(refs[:3])
        assert content.job(refs[3])["status"] == "queued"


class TestRoutineRuns:

    def make(self, store, name="Good morning"):
        return store.save({"name": name, "action": "briefing",
                           "schedule": "07:30"})["id"]

    def orphan_run(self, store, routine_id):
        run_id = store.start_run(routine_id)
        conn = store.connection()
        conn.execute("UPDATE routine_runs SET owner=? WHERE id=?",
                     (DEAD, run_id))
        conn.execute("UPDATE routines SET last_status='running' WHERE id=?",
                     (routine_id,))
        conn.commit()
        return run_id

    def test_a_run_left_by_a_dead_process_is_closed(self, routines):
        routine_id = self.make(routines)
        run_id = self.orphan_run(routines, routine_id)

        assert routines.recover_interrupted() == [run_id]
        run = routines.runs(routine_id)[0]
        assert run["status"] == "failed"
        assert "interrupted" in run["error"]

    def test_the_routine_stops_saying_it_is_running(self, routines):
        """The sharper edge: the summary on the routine row is what the panel
        shows, so without this it reads 'in progress' until somebody asks."""
        routine_id = self.make(routines)
        self.orphan_run(routines, routine_id)
        routines.recover_interrupted()
        assert routines.get(routine_id)["last_status"] == "failed"

    def test_a_run_this_process_started_is_left_alone(self, routines):
        routine_id = self.make(routines)
        routines.start_run(routine_id)
        assert routines.recover_interrupted() == []

    def test_a_finished_run_is_not_touched(self, routines):
        routine_id = self.make(routines)
        run_id = routines.start_run(routine_id)
        routines.finish_run(run_id, routine_id, "done", output="morning")
        assert routines.recover_interrupted() == []
        assert routines.runs(routine_id)[0]["status"] == "done"

    def test_a_routine_that_succeeded_since_keeps_its_summary(self, routines):
        """The summary is only cleared if it still says running: a later
        successful run must not be overwritten by an older abandoned one."""
        routine_id = self.make(routines)
        self.orphan_run(routines, routine_id)
        later = routines.start_run(routine_id)
        routines.finish_run(later, routine_id, "done", output="morning")

        routines.recover_interrupted()
        assert routines.get(routine_id)["last_status"] == "done"

    def test_it_is_safe_to_run_twice(self, routines):
        routine_id = self.make(routines)
        run_id = self.orphan_run(routines, routine_id)
        assert routines.recover_interrupted() == [run_id]
        assert routines.recover_interrupted() == []


class TestAtStartUp:

    def test_the_content_wrapper_reports_what_it_closed(self, tmp_path, capsys):
        from content.pipeline import recover_interrupted

        store = ContentStore(tmp_path / "jarvis.db")
        ref = store.create_job("a topic")
        orphan_job(store, ref)

        assert recover_interrupted(store) == [ref]
        assert "interrupted" in capsys.readouterr().out.lower()

    def test_it_says_nothing_when_there_is_nothing(self, tmp_path, capsys):
        from content.pipeline import recover_interrupted

        store = ContentStore(tmp_path / "jarvis.db")
        store.connection()
        assert recover_interrupted(store) == []
        assert capsys.readouterr().out == ""

    def test_a_broken_database_does_not_stop_the_agent_starting(self, tmp_path):
        """It runs on a worker during start-up. Whatever it finds, the one
        thing it may not do is prevent the assistant coming up."""
        from content.pipeline import recover_interrupted

        class Broken:
            def recover_interrupted(self):
                raise RuntimeError("the database file is a photo of a cat")

        assert recover_interrupted(Broken()) == []

    def test_the_engine_recovers_before_it_arms(self, tmp_path):
        """Order matters: a routine armed while its last run still says
        running would fire into a state the panel contradicts."""
        from routines.engine import RoutineEngine

        store = RoutineStore(tmp_path / "jarvis.db")
        routine_id = store.save({"name": "Morning", "action": "briefing",
                                 "schedule": "07:30"})["id"]
        run_id = store.start_run(routine_id)
        conn = store.connection()
        conn.execute("UPDATE routine_runs SET owner=? WHERE id=?",
                     (DEAD, run_id))
        conn.execute("UPDATE routines SET last_status='running' WHERE id=?",
                     (routine_id,))
        conn.commit()

        RoutineEngine(store=store).prepare()
        assert store.get(routine_id)["last_status"] == "failed"
        assert store.get(routine_id)["next_run_at"]


class TestTheTagItself:

    def test_this_process_owns_what_it_writes(self, content):
        ref = content.create_job("a topic")
        owner = content.job(ref)["owner"]
        assert owner.startswith(f"{os.getpid()}:")
        assert db.is_alive(owner)

    def test_a_reused_id_is_not_the_same_process(self):
        """The whole reason the start time is in there. After a reboot the id
        that wrote a row may well exist again, belonging to something else."""
        assert db.is_alive(f"{os.getpid()}:1") is False
