"""The content engine on its bad days.

`test_content.py` covers the shape of the work. This file covers what happens
when the inputs are wrong, the model is unhelpful, several threads want the
database at once, and the user has switched the whole thing off.

The organising idea is that none of these may raise into a conversation. A
malformed answer from a model is a Tuesday, not an incident; a locked database
is a wait, not a failure; a switched-off capability is an absence, not an
error. The only acceptable outcomes are a usable result, or a job in a final
state with a reason attached.
"""

import asyncio
import json
import threading
import time

import pytest

from content import pipeline, store, styles
from content.models import CAPTION_LIMIT, MAX_HASHTAGS, Beat, ReelScript
from content.scriptwriter import MAX_PER_BATCH, Scriptwriter, extract_json
from content.store import ContentStore
from content.tools import ContentStudio

GOOD_BEATS = [
    {"at": 2.0, "voiceover": "One.", "on_screen": "one", "visual": "a lit match"},
    {"at": 6.0, "voiceover": "Two.", "on_screen": "two", "visual": "a dark room"},
]
GOOD = {
    "hook": "A hook that earns the next two seconds.",
    "beats": GOOD_BEATS,
    "caption": "A caption.",
    "hashtags": ["one", "two"],
    "seconds": 30,
}


class FakeBackend:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.asked = []

    def ask(self, system, prompt, effort):
        self.asked.append({"system": system, "prompt": prompt, "effort": effort})
        return self.answers.pop(0) if self.answers else "{}"


@pytest.fixture
def db(tmp_path):
    made = ContentStore(db_path=tmp_path / "jarvis.db")
    yield made
    made.close()


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    from jarvis import paths

    paths.refresh()
    # The content engine ships switched off, and `queue_scripts` now refuses
    # when it is off rather than relying on the tool having been filtered out
    # of the model's list. A test that exercises the engine is a test of a
    # machine where somebody turned it on, so turn it on.
    from jarvis.config import Settings

    settings = Settings.load()
    settings.set("permissions.content", True)
    settings.save()
    yield tmp_path / "home"
    monkeypatch.delenv("JARVIS_HOME", raising=False)
    paths.refresh()


def write(backend_answer, topic="a topic", count=1):
    return Scriptwriter(backend=FakeBackend(backend_answer)).write(topic, count=count)


# --------------------------------------------------------------------------- #
# Answers that are not what was asked for
# --------------------------------------------------------------------------- #

class TestMalformedModelOutput:
    def test_truncated_json_is_not_a_crash(self, home):
        """Models get cut off mid-object at a token limit. Routinely."""
        cut = json.dumps([GOOD])[:60]
        with pytest.raises(RuntimeError):
            write(cut)                      # reported, never a traceback

    def test_a_trailing_comma_is_rejected_cleanly(self, home):
        with pytest.raises(RuntimeError):
            write('[{"hook": "x",}]')

    def test_a_fence_inside_a_string_does_not_confuse_the_extractor(self):
        body = json.dumps([dict(GOOD, caption="use ```json for code blocks```")])
        parsed = extract_json(body)
        assert parsed[0]["caption"].startswith("use ```json")

    def test_a_list_of_strings_yields_no_scripts(self, home):
        with pytest.raises(RuntimeError):
            write(json.dumps(["just", "some", "strings"]))

    def test_null_is_not_a_script(self, home):
        with pytest.raises(RuntimeError):
            write("null")

    def test_an_empty_array_is_not_a_script(self, home):
        with pytest.raises(RuntimeError):
            write("[]")

    def test_a_refusal_in_prose_is_reported_not_published(self, home):
        with pytest.raises(RuntimeError) as failure:
            write("I can't help with that request.")
        assert "usable" in str(failure.value)

    def test_the_deepest_wrapper_key_is_still_unwrapped(self, home):
        assert len(write(json.dumps({"results": [GOOD]}))) == 1

    def test_a_dict_that_is_itself_a_script_is_accepted(self, home):
        assert len(write(json.dumps(GOOD))) == 1


# --------------------------------------------------------------------------- #
# Fields that arrive in the wrong shape
# --------------------------------------------------------------------------- #

class TestSchemaValidation:
    def test_a_beat_given_as_a_bare_string_becomes_narration(self):
        script = ReelScript.from_dict(dict(GOOD, beats=["just a spoken line", *GOOD_BEATS]))
        assert script.beats[0].voiceover == "just a spoken line"

    def test_a_beat_with_a_string_timestamp_is_read_as_a_number(self):
        script = ReelScript.from_dict(dict(GOOD, beats=[dict(GOOD_BEATS[0], at="4.5"),
                                                        GOOD_BEATS[1]]))
        assert script.beats[0].at == 4.5

    def test_an_unparseable_timestamp_falls_back_rather_than_raising(self):
        script = ReelScript.from_dict(dict(GOOD, beats=[dict(GOOD_BEATS[0], at="soon"),
                                                        GOOD_BEATS[1]]))
        assert script.beats[0].at == 0.0

    def test_a_negative_timestamp_is_clamped(self):
        script = ReelScript.from_dict(dict(GOOD, beats=[dict(GOOD_BEATS[0], at=-9),
                                                        GOOD_BEATS[1]]))
        assert script.beats[0].at >= 0.0

    def test_beats_given_as_a_string_are_ignored_rather_than_exploding(self):
        script = ReelScript.from_dict(dict(GOOD, beats="two beats, trust me"))
        assert script.beats == []
        assert not script.is_usable()

    def test_empty_beats_are_dropped(self):
        script = ReelScript.from_dict(
            dict(GOOD, beats=[*GOOD_BEATS, {"at": 9, "voiceover": "", "visual": ""}]))
        assert len(script.beats) == 2

    def test_seconds_given_as_a_string_is_read(self):
        assert ReelScript.from_dict(dict(GOOD, seconds="45")).seconds == 45

    def test_hashtags_given_as_a_sentence_are_extracted(self):
        script = ReelScript.from_dict(dict(GOOD, hashtags="#coffee #espresso, barista"))
        assert script.hashtags == ["#coffee", "#espresso", "#barista"]

    def test_hashtags_are_capped(self):
        script = ReelScript.from_dict(dict(GOOD, hashtags=[f"tag{i}" for i in range(40)]))
        assert len(script.hashtags) == MAX_HASHTAGS

    def test_hashtags_of_pure_punctuation_are_dropped(self):
        assert ReelScript.from_dict(dict(GOOD, hashtags=["!!!", "###", "ok"])).hashtags \
            == ["#ok"]

    def test_a_caption_at_exactly_the_limit_is_untouched(self):
        body = "x" * CAPTION_LIMIT
        script = ReelScript(caption=body)
        assert len(script.full_caption()) == CAPTION_LIMIT

    def test_the_call_to_action_and_hashtags_ride_in_the_caption(self):
        script = ReelScript.from_dict(dict(GOOD, call_to_action="Save this."))
        caption = script.full_caption()
        assert "Save this." in caption
        assert "#one" in caption

    def test_emoji_and_accents_survive_the_round_trip(self):
        script = ReelScript.from_dict(dict(GOOD, caption="café ☕ crème"))
        again = ReelScript.from_dict(json.loads(script.as_json()))
        assert again.caption == "café ☕ crème"

    def test_a_script_missing_every_visual_is_not_usable(self):
        bare = [{"at": b["at"], "voiceover": b["voiceover"]} for b in GOOD_BEATS]
        assert "no visual directions" in ReelScript.from_dict(dict(GOOD, beats=bare)).problems()

    def test_an_empty_narration_estimates_zero_rather_than_dividing_by_nothing(self):
        assert ReelScript().estimated_seconds() == 0.0

    def test_the_narration_is_what_the_voiceover_stage_will_be_handed(self):
        script = ReelScript(hook="Hook.", beats=[Beat(voiceover="Body.")])
        assert script.voiceover_text() == "Hook. Body."


class TestBatching:
    def test_a_batch_of_zero_still_asks_for_one(self, home):
        backend = FakeBackend(json.dumps([GOOD]))
        Scriptwriter(backend=backend).write("a topic", count=0)
        assert "array of 1 object" in backend.asked[0]["prompt"]

    def test_a_negative_batch_still_asks_for_one(self, home):
        backend = FakeBackend(json.dumps([GOOD]))
        Scriptwriter(backend=backend).write("a topic", count=-5)
        assert "array of 1 object" in backend.asked[0]["prompt"]

    def test_the_cap_is_the_documented_one(self, home):
        backend = FakeBackend(json.dumps([GOOD] * MAX_PER_BATCH))
        Scriptwriter(backend=backend).write("a topic", count=MAX_PER_BATCH + 50)
        assert f"array of {MAX_PER_BATCH} object" in backend.asked[0]["prompt"]

    def test_a_partial_batch_is_returned_rather_than_thrown_away(self, home):
        """Two good scripts out of three asked for is two scripts, not nothing."""
        half = [dict(GOOD, hook="The first hook, which is a real one."),
                dict(GOOD, hook=""),
                dict(GOOD, hook="The second hook, also real.")]
        backend = FakeBackend(json.dumps(half), json.dumps(half))
        scripts = Scriptwriter(backend=backend).write("a topic", count=3)
        assert len(scripts) == 2
        assert all(script.is_usable() for script in scripts)

    def test_the_two_attempts_are_added_together_not_swapped(self, home):
        """Two usable from the first pass and two from the second is three
        scripts when three were asked for - not two, which is what replacing
        the batch wholesale used to give."""
        first = [dict(GOOD, hook="Hook one."), dict(GOOD, hook="Hook two."),
                 dict(GOOD, hook="")]
        second = [dict(GOOD, hook="Hook three."), dict(GOOD, hook="Hook four.")]
        backend = FakeBackend(json.dumps(first), json.dumps(second))
        scripts = Scriptwriter(backend=backend).write("a topic", count=3)
        assert [script.hook for script in scripts] == [
            "Hook one.", "Hook two.", "Hook three."]

    def test_the_retry_asks_only_for_what_is_missing(self, home):
        first = [dict(GOOD, hook="Hook one."), dict(GOOD, hook="Hook two."),
                 dict(GOOD, hook="")]
        second = [dict(GOOD, hook="Hook three.")]
        backend = FakeBackend(json.dumps(first), json.dumps(second))
        Scriptwriter(backend=backend).write("a topic", count=3)
        retry = backend.asked[1]["prompt"]
        assert "array of 1 object" in retry
        assert "Hook one." in retry          # so it does not repeat itself

    def test_the_same_hook_twice_is_one_script(self, home):
        """A model asked again for what it got wrong hands back what it got
        right along with it. Shipping the same Reel twice is worse than
        shipping one."""
        same = [dict(GOOD, hook="Exactly the same hook.")]
        backend = FakeBackend(json.dumps(same), json.dumps(same))
        scripts = Scriptwriter(backend=backend).write("a topic", count=2)
        assert len(scripts) == 1

    def test_hooks_that_differ_only_in_spacing_are_the_same_hook(self, home):
        first = [dict(GOOD, hook="A hook worth keeping.")]
        second = [dict(GOOD, hook="  a   HOOK worth   keeping. ")]
        backend = FakeBackend(json.dumps(first), json.dumps(second))
        scripts = Scriptwriter(backend=backend).write("a topic", count=2)
        assert len(scripts) == 1


# --------------------------------------------------------------------------- #
# The database, under pressure
# --------------------------------------------------------------------------- #

class TestPersistence:
    def test_many_threads_writing_at_once_all_land(self, db):
        """Background jobs run on pool threads, and each gets its own
        connection. SQLite serialises the writes; nothing may be lost or raise."""
        errors: list[Exception] = []
        refs: list[str] = []
        lock = threading.Lock()

        def churn(n: int) -> None:
            try:
                for i in range(15):
                    ref = db.create_job(f"topic {n}-{i}")
                    db.start_job(ref, stage="script")
                    db.add_asset(ref, kind="script", body={"hook": f"h{n}-{i}"})
                    db.finish_job(ref, result={"count": 1}, stage="script")
                    with lock:
                        refs.append(ref)
            except Exception as exc:
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=churn, args=(n,)) for n in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        assert not errors, f"database contention raised: {errors[:3]}"
        assert len(refs) == 120
        assert len(set(refs)) == 120, "two jobs were given the same reference"
        stored = db.recent_jobs(limit=200)
        assert len({row["ref"] for row in stored}) == 120
        assert all(row["status"] == "done" for row in stored)

    def test_a_reader_is_not_blocked_by_a_writer(self, db):
        """WAL is on for exactly this: the voice thread reading status while a
        worker writes must not wait on it."""
        stop = threading.Event()
        read_counts: list[int] = []

        def writer() -> None:
            while not stop.is_set():
                ref = db.create_job("churn")
                db.finish_job(ref, result={"count": 0})

        thread = threading.Thread(target=writer)
        thread.start()
        try:
            for _ in range(50):
                read_counts.append(len(db.recent_jobs(limit=5)))
        finally:
            stop.set()
            thread.join(timeout=30)
        assert len(read_counts) == 50

    def test_rows_survive_a_new_store_on_the_same_file(self, tmp_path):
        """A job outlives the call that started it, which means it outlives
        the objects too."""
        path = tmp_path / "jarvis.db"
        first = ContentStore(db_path=path)
        ref = first.create_job("espresso", style="reference")
        first.add_asset(ref, kind="script", body={"hook": "remembered"})
        first.finish_job(ref, result={"count": 1}, stage="script")
        first.close()

        second = ContentStore(db_path=path)
        try:
            job = second.job(ref)
            assert job is not None
            assert job["status"] == "done"
            assert job["result"]["count"] == 1
            assert second.assets(ref)[0]["body"]["hook"] == "remembered"
        finally:
            second.close()

    def test_a_result_round_trips_without_losing_its_shape(self, db):
        ref = db.create_job("espresso")
        payload = {"count": 2, "hooks": ["a", "b"],
                   "scripts": [ReelScript.from_dict(GOOD).as_dict()]}
        db.finish_job(ref, result=payload)
        back = db.job(ref)["result"]
        assert back["hooks"] == ["a", "b"]
        assert back["scripts"][0]["beats"][0]["visual"] == "a lit match"

    def test_an_unknown_reference_is_none_rather_than_an_error(self, db):
        assert db.job("does-not-exist") is None
        assert db.assets("does-not-exist") == []

    def test_filtering_by_status_returns_only_that_status(self, db):
        done = db.create_job("done one")
        db.finish_job(done, result={"count": 1})
        failed = db.create_job("failed one")
        db.fail_job(failed, "it broke")
        db.create_job("queued one")

        refs = [row["ref"] for row in db.recent_jobs(limit=10, status=store.STATUS_DONE)]
        assert refs == [done]

    def test_a_very_long_error_is_stored_rather_than_rejected(self, db):
        ref = db.create_job("espresso")
        db.fail_job(ref, "x" * 10_000)
        assert len(db.job(ref)["error"]) <= 2000

    def test_unicode_survives_the_database(self, db):
        ref = db.create_job("café ☕")
        db.add_asset(ref, kind="script", body={"hook": "crème brûlée 🔥"})
        assert db.job(ref)["topic"] == "café ☕"
        assert db.assets(ref)[0]["body"]["hook"] == "crème brûlée 🔥"


# --------------------------------------------------------------------------- #
# The job lifecycle end to end
# --------------------------------------------------------------------------- #

class TestJobLifecycle:
    def test_a_job_that_cannot_reach_a_worker_is_marked_failed(self, db, home,
                                                               monkeypatch):
        """A queued job nobody will ever run must not sit on 'queued' forever."""
        import workers

        class DeadHost:
            def submit(self, *args, **kwargs):
                return None

        monkeypatch.setattr(workers, "host", lambda: DeadHost())
        ref = pipeline.queue_scripts("espresso", count=1, db=db)
        job = db.job(ref)
        assert job["status"] == store.STATUS_FAILED
        assert "worker" in job["error"]

    def test_the_voice_is_told_when_the_engine_could_not_start(self, db, home,
                                                               monkeypatch):
        from livekit.agents.llm import ToolError

        import workers

        class DeadHost:
            def submit(self, *args, **kwargs):
                return None

        monkeypatch.setattr(workers, "host", lambda: DeadHost())
        studio = ContentStudio(db=db)
        with pytest.raises(ToolError):
            asyncio.run(studio.write_reel_scripts.__wrapped__(
                studio, None, topic="espresso", count=1))

    def test_a_job_whose_model_refuses_ends_failed_with_a_reason(self, db, home):
        ref = db.create_job("espresso")
        writer = Scriptwriter(backend=FakeBackend("no", "still no"))
        with pytest.raises(RuntimeError):
            pipeline.run_script_job(ref, "espresso", count=1, db=db, writer=writer)
        job = db.job(ref)
        assert job["status"] == store.STATUS_FAILED
        assert job["error"]
        assert job["finished_at"]

    def test_every_asset_of_a_batch_is_stored(self, db, home):
        ref = db.create_job("espresso")
        writer = Scriptwriter(backend=FakeBackend(json.dumps([GOOD, GOOD, GOOD])))
        result = pipeline.run_script_job(ref, "espresso", count=3, db=db, writer=writer)
        assert result["count"] == 3
        assert len(db.assets(ref, kind="script")) == 3

    def test_reading_back_a_reference_that_never_existed_says_so(self, db):
        from livekit.agents.llm import ToolError

        studio = ContentStudio(db=db)
        with pytest.raises(ToolError):
            asyncio.run(studio.read_reel_script.__wrapped__(
                studio, None, reference="nope"))

    def test_reading_back_a_failed_job_gives_the_reason(self, db):
        ref = db.create_job("espresso")
        db.fail_job(ref, "the model refused")
        studio = ContentStudio(db=db)
        said = asyncio.run(studio.read_reel_script.__wrapped__(
            studio, None, reference=ref))
        assert "the model refused" in said


class TestAJobIsFinishedAllAtOnceOrNotAtAll:
    """What the September 2026 re-audit found (F14), inverted.

    Scripts used to be inserted one at a time, committed, and only then was
    the job marked done. Three things fell out of that, and script jobs ship
    with retries on, so all three were reachable:

    - a failure while storing assets left the row on "running" forever;
    - the retry then inserted the whole batch a second time;
    - a job that failed and was retried successfully kept its old error.

    And a worker dying between the commit and the caller seeing the result
    meant the retry paid for the same scripts twice.
    """

    def writer(self, count=2):
        return Scriptwriter(backend=FakeBackend(json.dumps([GOOD] * count)))

    def test_a_failure_while_storing_leaves_no_half_written_job(self, db, home,
                                                                monkeypatch):
        import sqlite3

        ref = db.create_job("espresso")
        monkeypatch.setattr(
            db, "complete_scripts",
            lambda *a, **k: (_ for _ in ()).throw(
                sqlite3.OperationalError("simulated disk failure")))

        with pytest.raises(sqlite3.OperationalError):
            pipeline.run_script_job(ref, "espresso", count=2, db=db,
                                    writer=self.writer())

        job = db.job(ref)
        assert job["status"] == store.STATUS_FAILED, (
            "a job whose storage failed must not be left running forever")
        assert db.assets(ref, kind="script") == [], (
            "the assets were committed even though finishing the job failed")

    def test_a_retry_does_not_duplicate_the_scripts(self, db, home):
        """`complete_scripts` replaces the script assets, so the second
        attempt leaves two, not four."""
        ref = db.create_job("espresso")
        pipeline.run_script_job(ref, "espresso", count=2, db=db,
                                writer=self.writer())
        assert len(db.assets(ref, kind="script")) == 2

        # Force the work to run again rather than replaying the result.
        db.start_job(ref, stage="script")
        pipeline.run_script_job(ref, "espresso", count=2, db=db,
                                writer=self.writer())
        assert len(db.assets(ref, kind="script")) == 2

    def test_replaying_a_finished_job_does_not_call_the_model_again(self, db,
                                                                    home):
        """Scripts cost money. A worker that died after committing must not
        buy the same batch twice."""
        ref = db.create_job("espresso")
        first = pipeline.run_script_job(ref, "espresso", count=2, db=db,
                                        writer=self.writer())

        class Refuses:
            def write(self, *args, **kwargs):
                raise AssertionError("the model was asked a second time")

        again = pipeline.run_script_job(ref, "espresso", count=2, db=db,
                                        writer=Refuses())
        assert again == first
        assert len(db.assets(ref, kind="script")) == 2

    def test_a_successful_retry_clears_the_old_error(self, db, home):
        """Otherwise the panel shows a green job with a red reason under it."""
        ref = db.create_job("espresso")
        db.fail_job(ref, "the model refused", stage="script")
        assert db.job(ref)["error"]

        pipeline.run_script_job(ref, "espresso", count=2, db=db,
                                writer=self.writer())
        job = db.job(ref)
        assert job["status"] == store.STATUS_DONE
        assert job["error"] == ""

    def test_restarting_a_job_clears_its_old_finish_time(self, db, home):
        """A row that is running and yet finished in the past is not a state
        the job was ever in."""
        ref = db.create_job("espresso")
        db.finish_job(ref, result={"done": True}, stage="script")
        assert db.job(ref)["finished_at"]

        db.start_job(ref, stage="script")
        job = db.job(ref)
        assert job["status"] == store.STATUS_RUNNING
        assert not job["finished_at"]

    def test_a_storage_failure_reports_itself_and_not_the_bookkeeping(
            self, db, home, monkeypatch):
        """If the database is what broke, `fail_job` breaks too. The caller
        needs the original exception, not a second one about writing it down."""
        import sqlite3

        ref = db.create_job("espresso")

        class Boom:
            def write(self, *args, **kwargs):
                raise RuntimeError("the model refused, which is the real cause")

        monkeypatch.setattr(
            db, "fail_job",
            lambda *a, **k: (_ for _ in ()).throw(
                sqlite3.OperationalError("and the database is down too")))

        with pytest.raises(RuntimeError, match="the real cause"):
            pipeline.run_script_job(ref, "espresso", db=db, writer=Boom())


# --------------------------------------------------------------------------- #
# The switch
# --------------------------------------------------------------------------- #

class TestThePermissionSwitch:
    def names(self, tools):
        return {tool.info.name for tool in tools}

    def test_the_content_tools_are_removed_when_the_switch_is_off(self):
        import permissions

        class Off:
            def get(self, key, default=None):
                return False if key == "permissions.content" else default

        studio = ContentStudio()
        kept = permissions.filter_tools(list(studio.tools), Off())
        assert kept == []

    def test_the_content_tools_are_present_when_the_switch_is_on(self):
        import permissions

        class On:
            def get(self, key, default=None):
                return True if key == "permissions.content" else default

        studio = ContentStudio()
        kept = permissions.filter_tools(list(studio.tools), On())
        assert self.names(kept) == {"write_reel_scripts", "content_engine_status",
                                    "read_reel_script"}

    def test_the_switch_ships_off(self):
        import permissions

        assert permissions.BY_KEY["content"].default is False

    def test_the_switch_governs_exactly_the_tools_that_exist(self):
        import permissions

        studio = ContentStudio()
        assert set(permissions.BY_KEY["content"].tools) == self.names(studio.tools)

    def test_an_unreadable_settings_file_falls_back_to_the_default(self):
        import permissions

        class Broken:
            def get(self, key, default=None):
                raise OSError("settings.json is half-written")

        assert permissions.allowed("content", Broken()) is False

    def test_being_switched_off_is_explained_in_the_prompt(self):
        import permissions

        class Off:
            def get(self, key, default=None):
                return False if key == "permissions.content" else default

        assert "run the content engine" in permissions.summary(Off()).lower()


class TestSwitchingItOffStopsTheWorkAndNotJustTheTool:
    """What the September 2026 re-audit found (F04), inverted.

    Removing a tool from the list the model is handed is the right first move
    and is not enforcement. It does nothing about a routine, the dashboard, a
    retry, or a tool handed to a conversation that was already open when the
    switch was turned off - `queue_scripts` accepted all of those without
    asking. So the check is at the work, where every caller passes through,
    and again at the model call, which is the step that costs money.
    """

    def switch(self, on: bool) -> None:
        from jarvis.config import Settings

        settings = Settings.load()
        settings.set("permissions.content", on)
        settings.save()

    def test_queuing_is_refused_outright(self, db, home):
        self.switch(False)
        with pytest.raises(PermissionError, match="switched off"):
            pipeline.queue_scripts("a harmless topic", db=db)

    def test_nothing_reaches_a_worker(self, db, home, monkeypatch):
        """The acceptance the audit asked for: zero submissions, not a
        submission that fails politely later."""
        import workers

        submitted: list[str] = []

        class Recording:
            def submit(self, *args, **kwargs):
                submitted.append(kwargs.get("name", ""))
                return "would-have-run"

        monkeypatch.setattr(workers, "host", lambda: Recording())
        self.switch(False)

        with pytest.raises(PermissionError):
            pipeline.queue_scripts("a harmless topic", db=db)
        assert submitted == [], "a switched-off engine still queued work"

    def test_switching_it_off_after_the_job_was_accepted_stops_the_model_call(
            self, db, home):
        """A job accepted while it was on, running after it was turned off -
        a retry's backoff, or a restart picking the job back up. The model
        call is the step that cannot be taken back, so it is the step that
        has to ask again."""
        self.switch(True)
        ref = db.create_job("a harmless topic")

        asked: list[str] = []

        class Writer:
            def write(self, *args, **kwargs):
                asked.append("called the model")
                return []

        self.switch(False)

        with pytest.raises(PermissionError, match="switched off before"):
            pipeline.run_script_job(ref, "a harmless topic", db=db, writer=Writer())

        assert asked == [], "the model was called after the engine was switched off"
        assert db.job(ref)["status"] == store.STATUS_FAILED

    def test_a_refusal_is_not_retried(self, home):
        """A retry is for something that might work next time. A switch that
        is off is not that, and retrying it just prints two alarming lines
        about a job "failing" before refusing again."""
        import workers

        host = workers.WorkerHost()
        assert host.start()
        try:
            attempts: list[int] = []

            def refuses():
                attempts.append(1)
                raise PermissionError("the content engine is switched off")

            ref = host.submit(refuses, name="refused work", retries=2)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if host.job(ref).status in ("failed", "cancelled"):
                    break
                time.sleep(0.01)

            assert host.job(ref).status == "failed"
            assert attempts == [1], f"a refusal was retried {len(attempts)} times"
        finally:
            host.stop(timeout=2)

    def test_with_the_switch_on_nothing_is_in_the_way(self, db, home,
                                                      monkeypatch):
        """The check must not become a reason the engine never runs."""
        import workers

        submitted: list[str] = []

        class Recording:
            def submit(self, *args, **kwargs):
                submitted.append(kwargs.get("name", ""))
                return "queued"

        monkeypatch.setattr(workers, "host", lambda: Recording())
        self.switch(True)

        ref = pipeline.queue_scripts("a harmless topic", db=db)
        assert ref
        assert len(submitted) == 1


# --------------------------------------------------------------------------- #
# Style profiles
# --------------------------------------------------------------------------- #

class TestStyleProfiles:
    def test_an_unknown_profile_name_falls_back_to_the_default(self, home):
        styles.write_starter_file()
        assert styles.get("no-such-style").name == styles.DEFAULT.name

    def test_absurd_numbers_in_the_file_are_clamped(self, home):
        profile = styles.StyleProfile.from_dict(
            {"seconds": 99999, "beats": 0, "hashtags": -4}, name="wild")
        assert 5 <= profile.seconds <= 180
        assert profile.beats >= 2
        assert profile.hashtags >= 0

    def test_the_starter_file_is_not_overwritten_on_a_second_run(self, home):
        first = styles.write_starter_file()
        first.write_text('{"default": "mine", "profiles": {"mine": {"seconds": 45}}}',
                         encoding="utf-8")
        styles.write_starter_file()
        assert styles.get().seconds == 45

    def test_a_missing_file_still_gives_a_usable_profile(self, home):
        assert styles.get().name == styles.DEFAULT.name
        assert styles.get().as_prompt_block()
