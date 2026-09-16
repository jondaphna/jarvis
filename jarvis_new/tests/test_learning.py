"""Teaching it once, and it being better the second time.

This is the feature the user asked for in his own words: "if he does it once,
so he will remember to the next time it will get better at the tasks I give
him because most of the times I give him the same tasks."

So the things worth holding still are the ones that decide whether that is
true in practice rather than in principle:

* A lesson taught out loud survives a restart and reaches the next call's
  instructions, because a lesson the model never sees changes nothing.
* Correcting replaces rather than accumulates, because two contradictory
  recipes mean it can still pick the wrong one.
* Watching never overwrites teaching, because the thing you said out loud
  outranks a guess made from a tool trace.
* Nothing learned is worthless: questions, one-offs and failed attempts stay
  out, or the block fills with noise and buries the useful lessons.
* None of it can take down a call.
"""

import tempfile
from pathlib import Path

import pytest
from jarvis.core.memory import Memory

import learning


@pytest.fixture
def db() -> Memory:
    return Memory(Path(tempfile.mkdtemp()) / "lessons.db")


@pytest.fixture
def lessons(db) -> learning.Lessons:
    taught = learning.Lessons(memory=None)
    taught.db = db
    return taught


class TestTeachingItOnce:
    async def test_what_you_teach_it_comes_back_in_its_instructions(self, lessons) -> None:
        """The whole mechanism in one test: teach, then find it in the block
        that starts the next call."""
        await lessons.remember_how._func(
            lessons, None, "put music on",
            "open_url with site spotify, then click the play button")

        block = lessons.block()
        assert "put music on" in block
        assert "click the play button" in block

    async def test_it_survives_a_restart(self, db) -> None:
        """Teaching that lasts one session is not teaching, it is context."""
        first = learning.Lessons(memory=None)
        first.db = db
        await first.remember_how._func(first, None, "open my email",
                                       "open_url with site gmail")

        # A completely new object, as if the machine had been rebooted.
        later = learning.Lessons(memory=None)
        later.db = Memory(db.path)
        assert "open_url with site gmail" in later.block()

    async def test_correcting_replaces_the_wrong_way(self, lessons) -> None:
        """Being told twice is a failure. Keeping both recipes means it can
        still pick the one you just rejected."""
        await lessons.remember_how._func(lessons, None, "open my music",
                                         "open_url with site youtube")
        await lessons.remember_how._func(lessons, None, "open my music",
                                         "open_url with site spotify")

        block = lessons.block()
        assert "spotify" in block
        assert "youtube" not in block

    async def test_a_correction_is_recorded_as_a_correction(self, lessons) -> None:
        await lessons.remember_how._func(lessons, None, "do the thing", "step one")
        await lessons.remember_how._func(lessons, None, "do the thing", "step two")
        assert lessons.db.lessons()[0]["source"] == "corrected"

    async def test_teaching_nothing_is_refused(self, lessons) -> None:
        from livekit.agents.llm import ToolError

        with pytest.raises(ToolError):
            await lessons.remember_how._func(lessons, None, "  ", "something")
        with pytest.raises(ToolError):
            await lessons.remember_how._func(lessons, None, "something", "  ")

    async def test_the_same_job_said_differently_still_matches(self, lessons) -> None:
        """People do not repeat themselves word for word. A store that only
        answers to an exact phrase learns each variation separately and fires
        for none of them."""
        await lessons.remember_how._func(lessons, None, "open my spotify",
                                         "open_url with site spotify")
        for phrase in ("Jarvis, open Spotify please", "open the spotify",
                       "OPEN MY SPOTIFY"):
            assert lessons.db.lesson_for(phrase), phrase

    async def test_a_different_job_does_not_match(self, lessons) -> None:
        """A wrong recipe applied confidently is worse than no recipe."""
        await lessons.remember_how._func(lessons, None, "open my spotify",
                                         "open_url with site spotify")
        assert lessons.db.lesson_for("close this window") is None
        assert lessons.db.lesson_for("what is the weather") is None


class TestWatchingYou:
    def test_a_request_that_worked_is_written_down(self, lessons) -> None:
        lessons.heard("open my netflix")
        lessons.watched([("open_url", '{"site": "netflix"}', False)])

        block = lessons.block()
        assert "open my netflix" in block
        assert 'site="netflix"' in block

    def test_a_watched_lesson_says_where_it_came_from(self, lessons) -> None:
        """So you can read the list back and know which ones you actually said
        and which ones it inferred."""
        lessons.heard("open my netflix")
        lessons.watched([("open_url", '{"site": "netflix"}', False)])
        assert "from watching you" in lessons.block()

    def test_watching_never_overwrites_what_you_said_out_loud(self, lessons) -> None:
        lessons.db.learn("open my netflix", "the way I told you to do it",
                         said="open my netflix", source="taught")
        lessons.heard("open my netflix")
        lessons.watched([("open_url", '{"site": "example.com"}', False)])
        assert "the way I told you to do it" in lessons.block()

    def test_a_failed_attempt_is_not_learned(self, lessons) -> None:
        """Learning a recipe that did not work means reproducing the mistake
        faster next time."""
        lessons.heard("open my netflix")
        lessons.watched([("open_url", '{"site": "netflix"}', True)])
        assert lessons.block() == ""

    def test_bookkeeping_is_not_a_recipe(self, lessons) -> None:
        """"When they say X, call remember" is worse than no lesson, because
        it will be followed."""
        lessons.heard("remember that I like jazz")
        lessons.watched([("remember", '{"key": "music"}', False)])
        assert lessons.block() == ""

    def test_a_question_is_not_a_job(self, lessons) -> None:
        """Questions have answers, not recipes, and an answer learned today is
        wrong tomorrow."""
        lessons.heard("what is on this page")
        lessons.watched([("read_web_page", "{}", False)])
        assert lessons.block() == ""

    def test_a_one_off_ramble_is_not_learned(self, lessons) -> None:
        lessons.heard("could you have a look at that thing we talked about "
                      "yesterday and see whether it is still broken or not")
        lessons.watched([("open_url", '{"site": "github"}', False)])
        assert lessons.block() == ""

    async def test_teaching_in_a_turn_beats_watching_that_turn(self, lessons) -> None:
        """Otherwise the tools that ran while you were explaining overwrite
        the explanation you just gave."""
        lessons.heard("open my music")
        await lessons.remember_how._func(lessons, None, "open my music",
                                         "open_url with site spotify")
        lessons.watched([("open_url", '{"site": "wrong"}', False)])
        assert "spotify" in lessons.block()
        assert "wrong" not in lessons.block()

    def test_a_useful_lesson_rises_up_the_list(self, lessons) -> None:
        """The block is capped, so what gets in matters. Something you ask for
        weekly should outrank something taught once and never used."""
        lessons.db.learn("open my spotify", "open_url with site spotify")
        lessons.db.learn("open my obscure thing", "open_url with site example")
        for _ in range(5):
            lessons.heard("open my spotify")
        top = lessons.db.lessons(limit=2)[0]
        assert top["trigger"] == "open spotify"
        assert top["uses"] == 5


class TestItNeverBreaksACall:
    def test_no_database_is_survivable(self) -> None:
        broken = learning.Lessons(memory=None)
        broken.db = None
        assert broken.block() == ""
        broken.heard("open my spotify")
        assert broken.watched([("open_url", "{}", False)]) == ""

    def test_a_database_that_throws_is_survivable(self, lessons) -> None:
        class Explodes:
            def __getattr__(self, name):
                def boom(*_args, **_kwargs):
                    raise RuntimeError("disk on fire")
                return boom

        lessons.db = Explodes()
        assert lessons.block() == ""
        lessons.heard("open my spotify")          # must not raise
        assert lessons.watched([("open_url", "{}", False)]) == ""

    def test_broken_arguments_do_not_stop_a_recipe(self, lessons) -> None:
        """Tool arguments arrive as a JSON string from the model. Malformed
        JSON is a bad lesson, not a dead call."""
        assert learning.describe_call("open_url", "{not json") == "open_url"
        assert learning.describe_call("open_url", None) == "open_url"


class TestTheModelIsToldToUseIt:
    def test_the_block_tells_it_to_follow_rather_than_reason(self, lessons) -> None:
        lessons.db.learn("open my spotify", "open_url with site spotify")
        block = lessons.block()
        assert "follow it exactly and immediately" in block
        assert "don't reason it out again" in block

    def test_it_is_told_to_learn_without_being_asked(self, lessons) -> None:
        guidance = lessons.guidance()
        assert "remember_how" in guidance
        assert "Being told twice is a failure" in guidance

    def test_it_is_told_not_to_make_a_performance_of_learning(self, lessons) -> None:
        """A butler who announces every note he takes is worse than one who
        just remembers."""
        assert "quietly" in lessons.guidance()
        assert "Never ask permission to learn" in lessons.guidance()

    def test_the_tool_covers_teaching_and_correcting(self) -> None:
        doc = " ".join((learning.Lessons.remember_how.__doc__ or "").split())
        assert "correct" in doc.lower()
        assert "replaces the old steps" in doc

    def test_it_is_governed_by_a_permission_switch(self) -> None:
        import permissions

        assert "remember_how" in permissions.BY_KEY["learning"].tools

    def test_the_block_stays_small_enough_to_be_read(self, lessons) -> None:
        """A lessons block long enough to bury the current request defeats the
        purpose of having one."""
        for i in range(80):
            lessons.db.learn(f"do job number {i}", f"open_url with site site{i}")
        assert lessons.block().count("\n- ") <= 25
