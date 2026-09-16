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


class TestItLearnsTheWholeJobNotTheLastStep:
    """Found by stress-testing, not by the original tests.

    A real request runs tools in several rounds: "play Daft Punk on Spotify"
    is search, then look at the page, then click. Each round arrives as its
    own event, and each one used to REPLACE the recipe - so what got learned
    was "click the play button" with no search in front of it. Following that
    next time clicks whatever happens to be on screen.
    """

    def test_a_job_done_in_several_rounds_is_learned_whole(self, lessons) -> None:
        lessons.heard("play daft punk on spotify")
        lessons.watched([("search_on_site", '{"site":"spotify","query":"daft punk"}', False)])
        lessons.watched([("inspect_page", "{}", False)])
        lessons.watched([("click", '{"target":"play button"}', False)])

        steps = lessons.db.lessons()[0]["steps"]
        assert "search_on_site" in steps, f"the search was lost: {steps}"
        assert "click" in steps
        assert steps.index("search_on_site") < steps.index("click"), "out of order"

    def test_a_later_failure_throws_out_the_whole_recipe(self, lessons) -> None:
        """Half a job is not a job. If the click failed, the search that came
        before it is not a recipe worth keeping."""
        lessons.heard("play daft punk on spotify")
        lessons.watched([("search_on_site", '{"site":"spotify"}', False)])
        lessons.watched([("click", '{"target":"play"}', True)])
        assert lessons.block() == ""

    def test_a_new_request_starts_a_new_recipe(self, lessons) -> None:
        lessons.heard("open netflix")
        lessons.watched([("open_url", '{"site":"netflix"}', False)])
        lessons.heard("turn it up")
        lessons.watched([("set_volume", '{"direction":"up"}', False)])

        by_trigger = {r["trigger"]: r["steps"] for r in lessons.db.lessons()}
        assert "open_url" in by_trigger["open netflix"]
        assert "open_url" not in by_trigger["turn it up"], "recipes bled together"


class TestItAttributesTheRecipeToTheRightSentence:
    """Also found by stress-testing.

    Preemptive generation means the next thing you say can arrive before the
    tools for the last thing have reported. The recipe for "open my Netflix"
    was landing on "actually make it louder", which then teaches it that
    turning the volume up means opening Netflix.
    """

    def test_speaking_again_mid_job_does_not_steal_the_recipe(self, lessons) -> None:
        lessons.heard("open my netflix", at=100.0)
        lessons.heard("actually make it louder", at=105.0)
        lessons.watched([("open_url", '{"site":"netflix"}', False)], started_at=101.0)

        rows = {r["trigger"]: r["steps"] for r in lessons.db.lessons()}
        assert "open netflix" in rows, f"attributed to the wrong sentence: {rows}"
        assert "actually make it louder" not in rows

    def test_without_a_timestamp_it_still_uses_the_latest(self, lessons) -> None:
        """Callers that can't supply one must still work."""
        lessons.heard("open my netflix")
        lessons.watched([("open_url", '{"site":"netflix"}', False)])
        assert "open_url" in lessons.block()

    def test_a_tool_call_older_than_anything_said_is_dropped(self, lessons) -> None:
        """Rather than guessing, which is how you learn nonsense."""
        lessons.heard("open my netflix", at=100.0)
        assert lessons.watched([("open_url", "{}", False)], started_at=50.0) == ""


class TestItKeepsWorkingAsYouTeachItMore:
    """The one that matters for "it can do anything once I teach it".

    The block the model sees is capped, and it used to be ranked by how often
    a lesson had been used. A lesson taught today has been used zero times, so
    past about twenty-five lessons THE NEXT THING YOU TEACH IT IS INVISIBLE -
    stored, findable, and never shown. Teaching quietly stopped working.
    """

    def fill(self, lessons, count: int) -> None:
        for i in range(count):
            lessons.db.learn(f"do job number {i}", f"the recipe for job {i}")
            lessons.db.lesson_used(f"do job number {i}")

    def test_the_newest_lesson_always_reaches_the_model(self, lessons) -> None:
        self.fill(lessons, 60)
        lessons.db.learn("launch the new product page",
                         "open_url with site shopify, then click New Product")
        assert "launch the new product page" in lessons.block()

    def test_what_you_use_most_still_reaches_the_model(self, lessons) -> None:
        self.fill(lessons, 60)
        for _ in range(50):
            lessons.db.lesson_used("do job number 7")
        assert "the recipe for job 7" in lessons.block()

    def test_what_you_taught_beats_what_it_guessed(self, lessons) -> None:
        for i in range(40):
            lessons.db.learn(f"watched thing {i}", f"watched recipe {i}",
                             source="watched")
        lessons.db.learn("the one I taught", "the recipe I gave it")
        assert "the recipe I gave it" in lessons.block()

    def test_the_block_stays_within_its_budget(self, lessons) -> None:
        self.fill(lessons, 200)
        assert lessons.block().count("\n- ") <= learning.BLOCK_LIMIT

    def buried(self, lessons) -> None:
        """One old lesson, then enough newer ones to push it out of sight."""
        lessons.db.learn("ship the october newsletter",
                         "open_url with site mailchimp, then click Campaigns")
        self.fill(lessons, 200)

    def test_the_long_tail_is_reachable_by_asking(self, lessons) -> None:
        """Beyond what fits in the prompt, it has to be able to go and look -
        otherwise "teach it anything" has a hard ceiling at one screenful."""
        self.buried(lessons)
        assert "ship the october newsletter" not in lessons.block()

        found = lessons.db.lesson_for("ship the october newsletter")
        assert found and "mailchimp" in found["steps"]

    async def test_it_can_look_up_a_lesson_it_was_not_shown(self, lessons) -> None:
        self.buried(lessons)
        answer = await lessons.how_do_i._func(lessons, None, "ship the october newsletter")
        assert "mailchimp" in str(answer)

    async def test_the_lookup_is_forgiving_about_wording(self, lessons) -> None:
        """You will not say it the same way you said it in June."""
        self.buried(lessons)
        answer = await lessons.how_do_i._func(lessons, None, "ship october newsletter")
        assert "mailchimp" in str(answer)

    async def test_using_a_looked_up_lesson_brings_it_back_into_the_prompt(
            self, lessons) -> None:
        """So the second time you ask this week it is already in front of it,
        with no lookup at all. That is the improving-over-time part."""
        self.buried(lessons)
        await lessons.how_do_i._func(lessons, None, "ship the october newsletter")
        assert "mailchimp" in lessons.block()

    async def test_looking_up_something_never_taught_says_so(self, lessons) -> None:
        answer = str(await lessons.how_do_i._func(lessons, None, "pilot a submarine"))
        assert "never shown me" in answer.lower()
        assert "remember_how" in answer, "it should say how to fix that"

    def test_the_model_is_told_the_lookup_exists(self, lessons) -> None:
        lessons.db.learn("something", "some steps")
        assert "how_do_i" in lessons.block() + lessons.guidance()


class TestNothingYouTeachItIsEverLost:
    """The ceiling on "teach it anything", found by pushing past it.

    Matching used to look at only the two hundred most recent lessons, so once
    you had taught it more than that, the oldest ones could only be reached by
    saying the exact words again. You will not remember the exact words you
    used in June. Matching now filters in the database, over everything.
    """

    def test_an_old_lesson_is_found_by_different_wording(self, lessons) -> None:
        lessons.db.learn("send out the october newsletter to subscribers",
                         "open_url with site mailchimp")
        for i in range(250):
            lessons.db.learn(f"run business process number {i} properly",
                             f"recipe {i}")

        found = lessons.db.lesson_for("send the october newsletter to subscribers")
        assert found and "mailchimp" in found["steps"]

    def test_it_still_refuses_a_job_it_was_never_taught(self, lessons) -> None:
        """Reaching further must not mean matching more loosely - a wrong
        recipe applied confidently is worse than none."""
        lessons.db.learn("send out the october newsletter to subscribers",
                         "open_url with site mailchimp")
        for phrase in ("book a flight to berlin", "what is the weather",
                       "send a text to my brother"):
            assert lessons.db.lesson_for(phrase) is None, phrase

    def test_looking_up_stays_fast_with_thousands_of_lessons(self, lessons) -> None:
        """It runs while they are waiting for an answer."""
        import time

        for i in range(3000):
            lessons.db.learn(f"some job number {i} with a few words",
                             f"recipe {i}", source="watched")
        start = time.perf_counter()
        for _ in range(10):
            lessons.db.lesson_for("open my spotify and play something loud")
        elapsed = (time.perf_counter() - start) / 10
        assert elapsed < 0.15, f"{elapsed * 1000:.0f} ms per lookup"


class TestItAsksTheModelWhenTheWordsDontLineUp:
    """The limit of matching on words, and what to do about it.

    "Put my Spotify on" and "open my Spotify" are one request to a person and
    share one word. No amount of overlap scoring fixes that - it needs to know
    the phrases mean the same thing.

    Rather than buying an embedding service for it, the near misses are handed
    back and the model picks. It already knows what was said and what the
    words mean; that is the one thing in this system that does.
    """

    def setup_store(self, lessons) -> None:
        lessons.db.learn("open my spotify",
                         "open_url with site spotify, then click Liked Songs")
        lessons.db.learn("book the studio", "open_url with site calendly")

    async def test_a_near_miss_comes_back_as_a_candidate(self, lessons) -> None:
        self.setup_store(lessons)
        answer = str(await lessons.how_do_i._func(lessons, None, "put my spotify on"))
        assert "Liked Songs" in answer, answer

    async def test_it_is_told_these_are_guesses_not_the_answer(self, lessons) -> None:
        """Following a near miss as though it were exact is how it does the
        wrong job confidently."""
        self.setup_store(lessons)
        answer = str(await lessons.how_do_i._func(lessons, None, "put my spotify on"))
        assert "closest" in answer.lower() or "might" in answer.lower()

    async def test_an_exact_match_is_still_stated_as_certain(self, lessons) -> None:
        self.setup_store(lessons)
        answer = str(await lessons.how_do_i._func(lessons, None, "open my spotify"))
        assert "They taught you" in answer
        assert "closest" not in answer.lower()

    async def test_something_wholly_unrelated_offers_nothing(self, lessons) -> None:
        self.setup_store(lessons)
        answer = str(await lessons.how_do_i._func(lessons, None, "pilot a submarine"))
        assert "Liked Songs" not in answer
        assert "never shown me" in answer.lower()

    async def test_it_does_not_dump_the_whole_store(self, lessons) -> None:
        for i in range(60):
            lessons.db.learn(f"open my thing number {i}", f"recipe {i}")
        answer = str(await lessons.how_do_i._func(lessons, None, "open my thing somehow"))
        assert answer.count("\n- ") <= 5, "too many guesses is no answer at all"
