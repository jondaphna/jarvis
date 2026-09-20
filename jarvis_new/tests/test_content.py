"""The content engine, held to the three things that actually go wrong.

**The model's answer is not JSON.** It comes back fenced, prefaced with "Here
you go!", or as one object where a list was asked for. Every one of those is a
normal Tuesday for a language model and none of them may be treated as a
failed generation, because a business that stops on a formatting habit is a
business that stops.

**A bad script gets published.** A script with no hook is not a near miss, it
is nothing, and the difference between "rejected and retried" and "quietly
produced" is the difference between a channel and an embarrassment.

**The job vanishes.** A job row left on 'running' forever is
indistinguishable from a dead worker, so every exit path - success, failure,
a model that refused - has to land the row in a final state.
"""

import asyncio
import json
import time

import pytest

from content import pipeline, styles
from content.models import Beat, ReelScript
from content.scriptwriter import Scriptwriter, extract_json
from content.store import ContentStore
from content.tools import ContentStudio

GOOD_SCRIPT = {
    "title": "sour espresso",
    "hook": "Your espresso tastes sour because the water is too cold.",
    "beats": [
        {"at": 2.0, "voiceover": "Ninety-three degrees is the number.",
         "on_screen": "93 degrees", "visual": "a thermometer in a portafilter"},
        {"at": 6.0, "voiceover": "Below ninety it under-extracts and goes sharp.",
         "on_screen": "too cold = sour", "visual": "a pale thin espresso shot"},
        {"at": 11.0, "voiceover": "Flush the group head first and it fixes itself.",
         "on_screen": "flush first", "visual": "water running through a group head"},
    ],
    "caption": "Sour shots are almost always a temperature problem.",
    "call_to_action": "Save this for your next bad shot.",
    "hashtags": ["espresso", "#coffee", "barista"],
    "seconds": 30,
}


class FakeBackend:
    """A brain that says whatever the test tells it to, in order."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.asked = []

    def ask(self, system, prompt, effort):
        self.asked.append({"system": system, "prompt": prompt, "effort": effort})
        return self.answers.pop(0) if self.answers else "{}"


@pytest.fixture
def db(tmp_path):
    store = ContentStore(db_path=tmp_path / "jarvis.db")
    yield store
    store.close()


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A throwaway JARVIS folder, so tests never touch the real one."""
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


# --------------------------------------------------------------------------- #
# Reading whatever the model said
# --------------------------------------------------------------------------- #

def test_plain_json_is_read():
    assert extract_json('[{"hook": "x"}]') == [{"hook": "x"}]


def test_a_fenced_answer_is_read():
    raw = 'Here you go!\n```json\n[{"hook": "x"}]\n```\nHope that helps.'
    assert extract_json(raw) == [{"hook": "x"}]


def test_an_answer_buried_in_prose_is_read():
    raw = 'Sure. [{"hook": "x"}] Let me know if you want more.'
    assert extract_json(raw) == [{"hook": "x"}]


def test_prose_with_no_json_gives_nothing():
    assert extract_json("I'd rather not write that.") is None
    assert extract_json("") is None


def test_a_single_object_becomes_a_list_of_one():
    writer = Scriptwriter(backend=FakeBackend(json.dumps(GOOD_SCRIPT)))
    scripts = writer.write("espresso", count=1)
    assert len(scripts) == 1
    assert scripts[0].hook.startswith("Your espresso")


def test_a_wrapped_list_is_unwrapped():
    body = json.dumps({"scripts": [GOOD_SCRIPT]})
    writer = Scriptwriter(backend=FakeBackend(body))
    assert len(writer.write("espresso", count=1)) == 1


# --------------------------------------------------------------------------- #
# The script itself
# --------------------------------------------------------------------------- #

def test_the_narration_is_the_hook_plus_the_beats_in_order():
    script = ReelScript.from_dict(GOOD_SCRIPT)
    spoken = script.voiceover_text()
    assert spoken.startswith("Your espresso tastes sour")
    assert spoken.endswith("fixes itself.")
    assert "Ninety-three" in spoken


def test_hashtags_are_normalised_deduplicated_and_capped():
    script = ReelScript.from_dict(dict(GOOD_SCRIPT,
                                       hashtags=["Coffee", "#coffee", "!!!", "espresso"]))
    assert script.hashtags == ["#coffee", "#espresso"]


def test_a_caption_is_never_longer_than_instagram_allows():
    script = ReelScript.from_dict(dict(GOOD_SCRIPT, caption="x" * 3000))
    assert len(script.full_caption()) <= 2200


def test_a_script_without_a_hook_is_not_usable():
    script = ReelScript.from_dict(dict(GOOD_SCRIPT, hook=""))
    assert not script.is_usable()
    assert "no hook" in script.problems()


def test_a_script_with_one_beat_is_not_usable():
    script = ReelScript.from_dict(dict(GOOD_SCRIPT, beats=GOOD_SCRIPT["beats"][:1]))
    assert not script.is_usable()


def test_beat_timings_are_rebuilt_when_the_model_leaves_them_out():
    raw = dict(GOOD_SCRIPT,
               beats=[dict(beat, at=0) for beat in GOOD_SCRIPT["beats"]])
    writer = Scriptwriter(backend=FakeBackend(json.dumps([raw])))
    script = writer.write("espresso", count=1)[0]
    timings = [beat.at for beat in script.beats]
    assert timings == sorted(timings)
    assert all(at > 0 for at in timings)


def test_the_estimate_is_in_the_right_neighbourhood():
    script = ReelScript(hook="one two three four five",
                        beats=[Beat(voiceover="six seven eight nine ten")])
    # Ten words at roughly three a second.
    assert 3.0 <= script.estimated_seconds() <= 4.0


# --------------------------------------------------------------------------- #
# Writing, retrying, and refusing to ship rubbish
# --------------------------------------------------------------------------- #

def test_a_bad_first_answer_is_retried_once(home):
    backend = FakeBackend("I'm not going to do that.", json.dumps([GOOD_SCRIPT]))
    writer = Scriptwriter(backend=backend)
    scripts = writer.write("espresso", count=1)
    assert len(scripts) == 1
    assert len(backend.asked) == 2
    assert "rejected" in backend.asked[1]["prompt"]


def test_two_bad_answers_raise_rather_than_return_rubbish(home):
    backend = FakeBackend("nope", "still nope")
    with pytest.raises(RuntimeError) as failure:
        Scriptwriter(backend=backend).write("espresso", count=1)
    assert "usable" in str(failure.value)


def test_an_empty_topic_is_refused():
    with pytest.raises(ValueError):
        Scriptwriter(backend=FakeBackend()).write("   ")


def test_the_batch_is_capped(home):
    backend = FakeBackend(json.dumps([GOOD_SCRIPT] * 5))
    Scriptwriter(backend=backend).write("espresso", count=99)
    assert "array of 5 object" in backend.asked[0]["prompt"]


def test_the_house_style_reaches_the_model(home):
    backend = FakeBackend(json.dumps([GOOD_SCRIPT]))
    Scriptwriter(backend=backend).write("espresso", count=1)
    assert "House style" in backend.asked[0]["system"]


# --------------------------------------------------------------------------- #
# The job record
# --------------------------------------------------------------------------- #

def test_a_finished_job_is_recorded_with_its_scripts(db, home):
    ref = db.create_job("espresso")
    writer = Scriptwriter(backend=FakeBackend(json.dumps([GOOD_SCRIPT])))
    result = pipeline.run_script_job(ref, "espresso", count=1, db=db, writer=writer)

    job = db.job(ref)
    assert job["status"] == "done"
    assert job["finished_at"]
    assert result["count"] == 1
    assert db.assets(ref, kind="script")


def test_a_failed_job_is_left_in_a_final_state(db, home):
    ref = db.create_job("espresso")
    writer = Scriptwriter(backend=FakeBackend("nope", "nope"))
    with pytest.raises(RuntimeError):
        pipeline.run_script_job(ref, "espresso", count=1, db=db, writer=writer)

    job = db.job(ref)
    assert job["status"] == "failed"
    assert job["error"]
    assert job["finished_at"], "a job stuck on running looks like a dead worker"


def test_jobs_come_back_newest_first(db):
    first = db.create_job("one")
    second = db.create_job("two")
    refs = [job["ref"] for job in db.recent_jobs(limit=5)]
    assert refs[:2] == [second, first]


# --------------------------------------------------------------------------- #
# What the voice sees
# --------------------------------------------------------------------------- #

def test_asking_for_scripts_comes_back_immediately(db, home, monkeypatch):
    """The tool answers in the same breath, however long the writing takes."""
    import workers

    started = []

    def slow_job(*args, **kwargs):
        started.append(time.monotonic())
        time.sleep(1.0)

    monkeypatch.setattr(pipeline, "run_script_job", slow_job)
    studio = ContentStudio(db=db)

    began = time.monotonic()
    answer = _run(studio, studio.write_reel_scripts, topic="espresso", count=2)
    elapsed = time.monotonic() - began

    assert elapsed < 0.25, f"the voice tool blocked for {elapsed:.2f}s"
    assert "Started" in answer
    workers.host().stop()


def test_the_reference_is_spoken_back_so_it_can_be_asked_for(db, home, monkeypatch):
    monkeypatch.setattr(pipeline, "run_script_job", lambda *a, **k: None)
    studio = ContentStudio(db=db)
    answer = _run(studio, studio.write_reel_scripts, topic="espresso", count=1)

    # The row is written by the worker, not by the tool - that is what keeps
    # the voice off SQLite's write lock - so wait for it rather than assuming
    # it is already there.
    deadline = time.monotonic() + 10
    rows = []
    while time.monotonic() < deadline and not rows:
        rows = db.recent_jobs(limit=1)
        time.sleep(0.01)
    assert rows, "the worker never wrote the job row"
    assert rows[0]["ref"] in answer


def test_asking_with_no_topic_is_refused(db):
    from livekit.agents.llm import ToolError

    studio = ContentStudio(db=db)
    with pytest.raises(ToolError):
        _run(studio, studio.write_reel_scripts, topic="  ")


def test_status_says_what_the_next_stage_needs(db, home):
    studio = ContentStudio(db=db)
    said = _run(studio, studio.content_engine_status)
    assert "script" in said.lower()
    # The placeholder style is the thing most worth surfacing until the
    # reference reels have been described.
    assert "placeholder" in said.lower()


def test_reading_back_a_finished_job_gives_the_hooks(db, home):
    ref = db.create_job("espresso")
    writer = Scriptwriter(backend=FakeBackend(json.dumps([GOOD_SCRIPT])))
    pipeline.run_script_job(ref, "espresso", count=1, db=db, writer=writer)

    studio = ContentStudio(db=db)
    said = _run(studio, studio.read_reel_script, reference=ref)
    assert "Your espresso tastes sour" in said


def test_reading_back_a_job_still_running_says_so(db):
    ref = db.create_job("espresso")
    db.start_job(ref, stage="script")
    studio = ContentStudio(db=db)
    assert "still being written" in _run(studio, studio.read_reel_script, reference=ref)


# --------------------------------------------------------------------------- #
# Stages and style
# --------------------------------------------------------------------------- #

def test_the_script_stage_is_ready_and_publishing_is_not(home):
    state = pipeline.readiness()
    assert "script" in state["ready"]
    assert "publish" in state["blocked"]


def test_publishing_is_designed_against_the_official_api(home):
    publish = pipeline.STAGE_BY_KEY["publish"]
    assert "INSTAGRAM_ACCESS_TOKEN" in publish.keys
    assert "Graph API" in publish.notes


def test_the_style_file_is_written_where_it_can_be_edited(home):
    written = styles.write_starter_file()
    assert written.exists()
    body = json.loads(written.read_text("utf-8"))
    assert body["default"] in body["profiles"]


def test_an_edited_style_file_is_what_the_model_is_told(home):
    styles.write_starter_file()
    path = styles.path()
    body = json.loads(path.read_text("utf-8"))
    body["profiles"]["reference"]["voiceover"] = "Two narrators, arguing."
    body["profiles"]["reference"]["placeholder"] = False
    path.write_text(json.dumps(body), encoding="utf-8")

    profile = styles.get()
    assert profile.placeholder is False
    assert "Two narrators" in profile.as_prompt_block()


def test_a_broken_style_file_falls_back_rather_than_stopping(home):
    styles.write_starter_file()
    styles.path().write_text("{not json", encoding="utf-8")
    assert styles.get().name == styles.DEFAULT.name


def _run(studio, tool, **kwargs):
    """Call a tool the way the agent would.

    `function_tool` replaces the method with a FunctionTool object and keeps
    the original on `__wrapped__`, unbound - so the instance and the run
    context (unused by these tools) are passed explicitly.
    """
    return asyncio.run(tool.__wrapped__(studio, None, **kwargs))
