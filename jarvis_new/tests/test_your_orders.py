"""The instructions you write in settings, and whether they are actually obeyed.

The bug this file exists to stop, in the user's words: "I write the commands,
and he's not really... he doesn't really care about them. He's just ignoring
it."

He was right, and it was not the model's fault. The Commands tab creates rules
of kind "reply". The voice agent rendered only kind "instruct" into its prompt,
and unlike the original terminal JARVIS it never matches rules locally either.
So every command written in the settings panel was invisible to the thing that
was supposed to follow it - not weighted lightly, not overruled, simply never
mentioned.

Three things have to hold for "100% do everything that is there" to be true:

1. **Every rule reaches the prompt.** All three kinds, whatever the panel
   happens to create.
2. **They outrank the template.** They go first, and they say plainly that they
   win, because "priority over your general style guidance" is a narrow grant
   that a rule about behaviour does not obviously fall under.
3. **Editing them takes effect.** Without this you cannot tell a rule being
   ignored from a rule the running agent has never seen.
"""

import pytest
from jarvis.core.rules import KIND_INSTRUCT, KIND_REPLY, KIND_RUN, RuleBook

import personalise


class FakeSettings:
    def __init__(self, **values):
        self._values = values

    def get(self, dotted, default=None):
        return self._values.get(dotted, default)


@pytest.fixture
def book(tmp_path, monkeypatch) -> RuleBook:
    from jarvis import paths

    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    paths.refresh()
    paths.ensure_dirs()
    return RuleBook(FakeSettings())


class TestEveryCommandReachesThePrompt:
    """The bug itself: a command the agent is never told about."""

    def test_a_command_from_the_settings_panel_is_in_the_prompt(self, book) -> None:
        """What the Commands tab creates - kind "reply" - used to be dropped
        on the floor, which is the whole complaint."""
        book.create("status report", "All systems nominal, sir.")

        orders = personalise.your_orders(FakeSettings(), book)
        assert "status report" in orders
        assert "All systems nominal, sir." in orders

    def test_a_reply_command_says_to_use_those_exact_words(self, book) -> None:
        book.create("status report", "All systems nominal, sir.")
        orders = personalise.your_orders(FakeSettings(), book)
        assert "exactly" in orders.lower()

    def test_a_standing_instruction_is_in_the_prompt(self, book) -> None:
        book.create("", "Always answer in Hebrew unless I ask otherwise.",
                    kind=KIND_INSTRUCT)
        orders = personalise.your_orders(FakeSettings(), book)
        assert "Hebrew" in orders

    def test_a_shorthand_is_in_the_prompt_and_says_to_act(self, book) -> None:
        book.create("movie night",
                    "open Netflix and put my phone on silent", kind=KIND_RUN)
        orders = personalise.your_orders(FakeSettings(), book)
        assert "movie night" in orders
        assert "open Netflix and put my phone on silent" in orders

    def test_all_three_kinds_survive_together(self, book) -> None:
        book.create("status report", "All nominal.", kind=KIND_REPLY)
        book.create("", "Call me sir.", kind=KIND_INSTRUCT)
        book.create("movie night", "open Netflix", kind=KIND_RUN)

        orders = personalise.your_orders(FakeSettings(), book)
        for fragment in ("All nominal.", "Call me sir.", "open Netflix"):
            assert fragment in orders, fragment

    def test_a_disabled_command_is_left_out(self, book) -> None:
        rule = book.create("status report", "All nominal.")
        book.set_enabled(rule.id, False)
        assert "All nominal." not in personalise.your_orders(FakeSettings(), book)

    def test_the_rules_you_typed_in_the_rules_tab_are_there_too(self, book) -> None:
        settings = FakeSettings(**{
            "persona.instructions": "My business is video production.",
            "persona.never": "Never spend money.",
        })
        orders = personalise.your_orders(settings, book)
        assert "video production" in orders
        assert "Never spend money." in orders

    def test_nothing_written_means_no_block_at_all(self, book) -> None:
        """An empty heading full of nothing wastes prompt space and teaches
        the model that this section can be ignored."""
        assert personalise.your_orders(FakeSettings(), book) == ""


class TestTheyOutrankTheTemplate:
    def test_his_orders_come_before_the_template(self, book) -> None:
        book.create("status report", "All nominal.")
        assembled = personalise.assemble("TEMPLATE INSTRUCTIONS", None,
                                         FakeSettings(), book)
        assert assembled.index("All nominal.") < assembled.index("TEMPLATE")

    def test_the_wording_is_absolute_rather_than_about_style(self, book) -> None:
        """"Priority over your general style guidance" does not obviously cover
        "always check my calendar first", which is behaviour, not style."""
        book.create("status report", "All nominal.")
        orders = personalise.your_orders(FakeSettings(), book).lower()
        assert "override" in orders or "overrides" in orders
        assert "every" in orders

    def test_it_is_told_not_to_need_reminding(self, book) -> None:
        book.create("status report", "All nominal.")
        assert "without being reminded" in personalise.your_orders(
            FakeSettings(), book)

    def test_the_end_of_the_prompt_says_so_again(self, book) -> None:
        """The last thing in a long prompt carries weight. The template's own
        jokes used to be the last word on what is mandatory."""
        book.create("status report", "All nominal.")
        assembled = personalise.assemble("TEMPLATE", None, FakeSettings(), book)
        tail = assembled[-400:].lower()
        assert "standing orders" in tail

    def test_no_reminder_when_there_is_nothing_to_remind_about(self, book) -> None:
        assembled = personalise.assemble("TEMPLATE", None, FakeSettings(), book)
        assert "standing orders" not in assembled.lower()


class TestEditingThemTakesEffect:
    """Otherwise "he ignores it" and "he never saw it" look identical."""

    def test_the_fingerprint_changes_when_a_command_is_added(self, book) -> None:
        before = personalise.fingerprint(FakeSettings(), book)
        book.create("status report", "All nominal.")
        assert personalise.fingerprint(FakeSettings(), book) != before

    def test_the_fingerprint_changes_when_the_rules_tab_changes(self, book) -> None:
        before = personalise.fingerprint(FakeSettings(), book)
        after = personalise.fingerprint(
            FakeSettings(**{"persona.instructions": "Call me sir."}), book)
        assert after != before

    def test_the_fingerprint_is_stable_when_nothing_changed(self, book) -> None:
        """A fingerprint that churns would rewrite the agent's instructions
        mid-sentence for no reason."""
        book.create("status report", "All nominal.")
        settings = FakeSettings()
        assert personalise.fingerprint(settings, book) == \
            personalise.fingerprint(settings, book)


class TestItSurvivesAMess:
    def test_a_missing_rule_book_does_not_stop_the_call(self) -> None:
        assert personalise.your_orders(FakeSettings(), None) == ""
        assert personalise.assemble("TEMPLATE", None, FakeSettings(), None)

    def test_a_rule_book_that_throws_does_not_stop_the_call(self) -> None:
        class Explodes:
            def all(self, *_args, **_kwargs):
                raise RuntimeError("disk on fire")

        assert personalise.your_orders(FakeSettings(), Explodes()) == ""
        assert personalise.fingerprint(FakeSettings(), Explodes())

    def test_unreadable_settings_do_not_stop_the_call(self, book) -> None:
        class Broken:
            def get(self, *_args, **_kwargs):
                raise RuntimeError("corrupt")

        assert personalise.your_orders(Broken(), book) == ""
        assert personalise.assemble("TEMPLATE", None, Broken(), book)


class TestTheRunningCallPicksUpAnEdit:
    """Typing a command mid-conversation has to do something.

    Instructions are fixed when a realtime session starts, so before this an
    edit did nothing until the next call - which from the outside is exactly
    what "he ignores it" looks like. These call the real method on a stand-in,
    because building a whole Assistant needs live LiveKit credentials.
    """

    class Stub:
        def __init__(self, fingerprint_value: str) -> None:
            self._prompt_fingerprint = fingerprint_value
            self._settings = None
            self._rules = None
            self.pushed: list[str] = []
            self.memory = None
            self.lessons = None

        def _instructions(self) -> str:
            return "rebuilt"

        async def update_instructions(self, text: str) -> None:
            self.pushed.append(text)

    async def test_an_edit_is_pushed_into_the_live_session(self, monkeypatch) -> None:
        import agent

        monkeypatch.setattr(agent, "load_settings", lambda: FakeSettings())
        monkeypatch.setattr(agent, "load_rules", lambda _s: None)
        monkeypatch.setattr(agent, "fingerprint", lambda _s, _r: "changed")

        stub = self.Stub("was-something-else")
        assert await agent.Assistant.reload_rules(stub) is True
        assert stub.pushed == ["rebuilt"]
        assert stub._prompt_fingerprint == "changed"

    async def test_nothing_changing_does_not_interrupt(self, monkeypatch) -> None:
        """Rewriting the instructions for no reason mid-sentence is worse than
        not noticing an edit."""
        import agent

        monkeypatch.setattr(agent, "load_settings", lambda: FakeSettings())
        monkeypatch.setattr(agent, "load_rules", lambda _s: None)
        monkeypatch.setattr(agent, "fingerprint", lambda _s, _r: "same")

        stub = self.Stub("same")
        assert await agent.Assistant.reload_rules(stub) is False
        assert stub.pushed == []

    async def test_a_half_written_settings_file_is_survivable(self, monkeypatch) -> None:
        """The panel saves by writing the file. Catching it mid-write must not
        end the conversation."""
        import agent

        def explode():
            raise RuntimeError("half a json file")

        monkeypatch.setattr(agent, "load_settings", explode)
        stub = self.Stub("whatever")
        assert await agent.Assistant.reload_rules(stub) is False

    def test_the_watcher_is_actually_started(self) -> None:
        """A reload nobody calls is a reload that does not happen."""
        import inspect

        import agent

        source = inspect.getsource(agent.my_agent)
        assert "_watch_for_edits(assistant)" in source
        assert "reload_rules" in inspect.getsource(agent._watch_for_edits)

    def test_it_checks_often_enough_to_feel_immediate(self) -> None:
        import agent

        assert agent.RELOAD_SECONDS <= 10
