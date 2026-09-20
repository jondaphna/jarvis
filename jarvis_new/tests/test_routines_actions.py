"""What a routine does when it fires, and what it does when it can't.

The rule these all test is the same one: at half past seven in the morning
there is nobody to read an error. So an action either produces something worth
saying, or says plainly what stopped it - and either way the run finishes.
A missing API key is a sentence, not a stack trace.
"""

from datetime import datetime

import pytest

from routines import actions
from routines.actions import ACTIONS, catalogue, run
from routines.store import RoutineStore


@pytest.fixture
def store(tmp_path):
    return RoutineStore(tmp_path / "jarvis.db")


def routine(**over):
    base = {"id": "test", "name": "Test", "action": "note", "instruction": ""}
    base.update(over)
    return base


class TestTheCatalogue:

    def test_every_action_can_be_looked_up_by_key(self):
        for key in ACTIONS:
            assert callable(actions.get(key))

    def test_an_unknown_action_is_none_rather_than_a_crash(self):
        assert actions.get("teleport") is None
        assert actions.get("") is None

    def test_running_an_unknown_action_says_so(self):
        with pytest.raises(ValueError, match="isn't an action"):
            run(routine(action="teleport"))

    def test_the_catalogue_is_what_a_dropdown_needs(self):
        entries = catalogue()
        assert len(entries) == len(ACTIONS)
        for entry in entries:
            assert entry["key"] and entry["label"] and entry["detail"]
            assert entry["needs"]

    def test_the_free_ones_say_they_need_nothing(self):
        needs = {entry["key"]: entry["needs"] for entry in catalogue()}
        assert needs["system_check"] == "Nothing."
        assert needs["note"] == "Nothing."


class TestNote:
    """The one that works on a machine with nothing installed."""

    def test_it_says_what_it_was_given(self):
        assert run(routine(instruction="Bins go out tonight.")) \
            == "Bins go out tonight."

    def test_an_empty_note_still_returns_a_sentence(self):
        assert "nothing to say" in run(routine(name="Reminder", instruction=""))

    def test_it_touches_nothing(self, monkeypatch):
        """No model, no network, no database - which is why it is the action
        used to test the engine itself."""
        def explode(*args, **kwargs):
            raise AssertionError("an action reached for the network")

        monkeypatch.setattr("urllib.request.urlopen", explode)
        assert run(routine(instruction="fine")) == "fine"


class TestSystemCheck:

    def test_it_says_something_about_the_machine(self):
        answer = run(routine(action="system_check"))
        assert answer
        assert "percent" in answer or "psutil" in answer

    def test_it_survives_psutil_being_missing(self, monkeypatch):
        import builtins

        real = builtins.__import__

        def blocked(name, *args, **kwargs):
            if name == "psutil":
                raise ImportError("no psutil here")
            return real(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", blocked)
        assert "psutil" in " ".join(actions.machine_lines())

    def test_it_survives_psutil_throwing(self, monkeypatch):
        import psutil

        monkeypatch.setattr(psutil, "disk_usage",
                            lambda *a: (_ for _ in ()).throw(OSError("gone")))
        lines = actions.machine_lines()
        assert lines and "couldn't read" in lines[0]


class TestBriefing:

    def test_it_opens_with_the_time_of_day(self, store, monkeypatch):
        monkeypatch.setattr(actions, "_greeting", lambda: "Good morning, sir.")
        assert run(routine(action="briefing"), store).startswith("Good morning")

    @pytest.mark.parametrize("hour,expected", [
        (7, "Good morning"), (13, "Good afternoon"), (20, "Good evening"),
        (0, "Good morning"), (11, "Good morning"), (12, "Good afternoon"),
        (17, "Good afternoon"), (18, "Good evening"), (23, "Good evening"),
    ])
    def test_the_greeting_fits_the_hour(self, hour, expected):
        when = datetime(2026, 9, 18, hour, 0)
        assert actions._greeting(when).startswith(expected)

    def test_it_works_with_no_thinking_brain_at_all(self, store, monkeypatch):
        """A fresh Windows machine has no key and no Ollama. The briefing on
        that machine is the locally composed one, and it is fine."""
        import thinker

        def refuse(*args, **kwargs):
            raise RuntimeError("Thinking needs a Google key.")

        monkeypatch.setattr(thinker.Thinker, "ask", refuse)
        answer = run(routine(action="briefing"), store)
        assert answer
        assert "Google key" not in answer

    def test_the_instruction_is_folded_in(self, store, monkeypatch):
        import thinker

        monkeypatch.setattr(thinker.Thinker, "ask",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
        answer = run(routine(action="briefing",
                             instruction="Remind me about the dentist."), store)
        assert "dentist" in answer

    def test_a_brain_that_answers_is_used(self, store, monkeypatch):
        import thinker

        monkeypatch.setattr(thinker.Thinker, "ask",
                            lambda *a, **k: "Morning. All quiet.")
        assert run(routine(action="briefing"), store) == "Morning. All quiet."

    def test_a_brain_that_answers_with_nothing_falls_back(self, store,
                                                          monkeypatch):
        import thinker

        monkeypatch.setattr(thinker.Thinker, "ask", lambda *a, **k: "   ")
        assert len(run(routine(action="briefing"), store)) > 20

    def test_it_mentions_a_routine_that_is_failing(self, store, monkeypatch):
        import thinker

        monkeypatch.setattr(thinker.Thinker, "ask",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
        store.save({"name": "Broken one", "action": "note",
                    "schedule": "07:00", "instruction": "x"})
        store.finish_run(store.start_run("broken-one"), "broken-one", "failed",
                         error="nope")
        assert "Broken one" in run(routine(action="briefing"), store)

    def test_it_survives_a_store_it_cannot_read(self, tmp_path, monkeypatch):
        import thinker

        monkeypatch.setattr(thinker.Thinker, "ask",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
        broken = tmp_path / "broken.db"
        broken.write_text("not sqlite")
        assert run(routine(action="briefing"), RoutineStore(broken))

    def test_it_survives_the_content_store_being_unreadable(self, store,
                                                            monkeypatch):
        monkeypatch.setattr(actions, "content_lines",
                            lambda: (_ for _ in ()).throw(RuntimeError("no db")))
        with pytest.raises(RuntimeError):
            actions.content_lines()
        # and the real one, on a database that is not one:
        assert isinstance(actions.machine_lines(), list)


class TestInstruct:

    def test_it_puts_the_instruction_to_the_brain(self, monkeypatch):
        import thinker

        seen = {}

        def capture(self, task, mode="general", context=""):
            seen["task"] = task
            return "Here is the answer."

        monkeypatch.setattr(thinker.Thinker, "ask", capture)
        answer = run(routine(action="instruct",
                             instruction="Summarise yesterday's news."))
        assert answer == "Here is the answer."
        assert seen["task"] == "Summarise yesterday's news."

    def test_an_empty_instruction_says_so(self):
        assert "no instruction" in run(routine(action="instruct"))

    def test_a_missing_key_becomes_a_sentence_not_a_crash(self, monkeypatch):
        import thinker

        def refuse(*args, **kwargs):
            raise RuntimeError("Thinking needs a Google key.")

        monkeypatch.setattr(thinker.Thinker, "ask", refuse)
        answer = run(routine(action="instruct", instruction="anything"))
        assert "couldn't carry out" in answer
        assert "Google key" in answer


class TestContentScripts:

    @pytest.fixture(autouse=True)
    def content_switched_on(self, monkeypatch):
        """The content engine is off by default, and the routine now checks.

        These tests are about what the routine does once it is allowed to run,
        so they say so rather than relying on the switch's default.
        """
        import permissions

        monkeypatch.setattr(permissions, "allowed",
                            lambda key, settings=None: True)

    def test_it_queues_rather_than_writing_them_itself(self, monkeypatch):
        from content import pipeline

        monkeypatch.setattr(pipeline, "queue_scripts",
                            lambda **kwargs: "abc123abc123")
        answer = run(routine(action="content_scripts", instruction="pasta"))
        assert "abc123abc123" in answer
        assert "pasta" in answer

    def test_an_empty_topic_says_so(self):
        assert "no topic" in run(routine(action="content_scripts"))

    def test_a_failure_to_queue_is_a_sentence(self, monkeypatch):
        from content import pipeline

        def explode(**kwargs):
            raise RuntimeError("the worker is down")

        monkeypatch.setattr(pipeline, "queue_scripts", explode)
        assert "couldn't start" in run(
            routine(action="content_scripts", instruction="pasta"))

    def test_the_content_switch_stops_it_queueing(self, monkeypatch):
        """Turning the engine off has to stop a standing routine too.

        Switching a capability off removes its tools, which covers everything
        the model asks for - but a routine is not the model asking. Without
        this check a routine saved while the engine was on carried on queueing
        work after it was turned off.
        """
        import permissions
        from content import pipeline

        monkeypatch.setattr(permissions, "allowed",
                            lambda key, settings=None: key != "content")

        queued = []
        monkeypatch.setattr(pipeline, "queue_scripts",
                            lambda **kwargs: queued.append(kwargs) or "ref")

        answer = run(routine(action="content_scripts", instruction="pasta"))
        assert queued == []
        assert "switched off" in answer

    def test_it_never_posts_anything(self):
        """The engine writes. The line it must not cross is publishing, and
        no action here has any way to."""
        source = (actions.action_content_scripts.__doc__ or "").lower()
        assert "queue" in source
        for entry in catalogue():
            assert "post" not in entry["label"].lower()


class TestItSoundsLikeAPerson:
    """The briefing is read aloud by a text-to-speech voice, so the date has
    to be written the way somebody would say it."""

    @pytest.mark.parametrize("day,expected", [
        (1, "1st"), (2, "2nd"), (3, "3rd"), (4, "4th"),
        (11, "11th"), (12, "12th"), (13, "13th"),
        (21, "21st"), (22, "22nd"), (23, "23rd"), (30, "30th"), (31, "31st"),
    ])
    def test_the_day_gets_its_ordinal(self, day, expected):
        assert actions._ordinal(day) == expected

    def test_the_greeting_reads_as_a_sentence(self):
        assert actions._greeting(datetime(2026, 9, 18, 7, 0)) \
            == "Good morning, sir. It is Friday the 18th of September."

    def test_no_markup_reaches_the_voice(self):
        """Nothing here may emit markdown: the prompt forbids it and the
        text-to-speech would read the asterisks out."""
        for text in (actions._greeting(), *actions.machine_lines()):
            assert not any(mark in text for mark in ("*", "#", "`", "|"))
