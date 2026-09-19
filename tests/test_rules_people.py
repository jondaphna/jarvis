"""Custom commands, and who JARVIS takes them from."""

from __future__ import annotations

import pytest

from jarvis.core.people import Authority, PeopleRegistry
from jarvis.core.permissions import Outcome, Request
from jarvis.core.rules import (
    ANYONE, KIND_INSTRUCT, KIND_REPLY, KIND_RUN, Rule, RuleBook,
    parse_rule_command,
)
from jarvis.core.grants import (
    CAP_APP_LAUNCH, CAP_FS_READ, CAP_FS_WRITE, CAP_LLM_CALL, CAP_WEB_SEARCH,
)


@pytest.fixture
def book(settings, tmp_path):
    return RuleBook(settings, path=tmp_path / "commands.json")


@pytest.fixture
def people(settings, tmp_path):
    return PeopleRegistry(settings, path=tmp_path / "people.json")


# --------------------------------------------------------------------------- #
# Custom commands
# --------------------------------------------------------------------------- #

class TestSpokenRuleCreation:
    def test_the_exact_example_from_the_brief(self):
        rule = parse_rule_command(
            "From now on, when I say hey jarvis, say Yes sir, how can I help you")
        assert rule is not None
        assert rule.kind == KIND_REPLY
        assert rule.trigger == "hey jarvis"
        assert rule.response == "Yes sir, how can I help you"

    @pytest.mark.parametrize("said,trigger", [
        ("every time I say good morning reply Morning sir", "good morning"),
        ('when I say "movie night" do open Netflix', "movie night"),
        ("whenever I say status, say All systems nominal", "status"),
    ])
    def test_other_phrasings(self, said, trigger):
        rule = parse_rule_command(said)
        assert rule is not None and rule.trigger == trigger

    def test_do_creates_a_shorthand_not_a_reply(self):
        rule = parse_rule_command("when I say movie night, do dim the lights")
        assert rule.kind == KIND_RUN

    @pytest.mark.parametrize("said", [
        "Always call me sir",
        "Never use markdown in your replies",
        "from now on answer me in Hebrew",
    ])
    def test_standing_instructions(self, said):
        rule = parse_rule_command(said)
        assert rule is not None and rule.kind == KIND_INSTRUCT

    @pytest.mark.parametrize("said", [
        "open chrome please",
        "what time is it",
        "say hello to my brother",
        "",
    ])
    def test_ordinary_requests_are_not_rules(self, said):
        assert parse_rule_command(said) is None


class TestMatching:
    def test_a_reply_rule_fires(self, book):
        book.create("hey jarvis", "Yes sir, how can I help you")
        rule = book.find("Hey, Jarvis!")
        assert rule is not None and rule.response == "Yes sir, how can I help you"

    def test_punctuation_and_case_do_not_matter(self, book):
        book.create("good morning", "Morning, sir.")
        assert book.find("  GOOD MORNING!! ") is not None

    def test_unrelated_speech_matches_nothing(self, book):
        book.create("hey jarvis", "Yes sir")
        assert book.find("what's the weather") is None

    def test_exact_match_can_be_required(self, book):
        book.create("status", "All nominal", match="exact")
        assert book.find("status") is not None
        assert book.find("what is the status of my missions") is None

    def test_the_more_specific_rule_wins(self, book):
        book.create("morning", "Generic morning")
        book.create("good morning", "Specific morning")
        assert book.find("good morning").response == "Specific morning"

    def test_priority_overrides_specificity(self, book):
        book.create("morning", "Wins", priority=10)
        book.create("good morning", "Loses")
        assert book.find("good morning").response == "Wins"

    def test_a_disabled_rule_does_not_fire(self, book):
        rule = book.create("hey jarvis", "Yes sir")
        book.set_enabled(rule.id, False)
        assert book.find("hey jarvis") is None

    def test_shorthand_keeps_what_follows_it(self, book):
        rule = book.create("movie night", "dim the lights and open Netflix",
                           kind=KIND_RUN)
        assert rule.remainder("movie night in the lounge") == "in the lounge"


class TestScoping:
    def test_a_personal_rule_only_fires_for_that_person(self, book):
        book.create("hey jarvis", "Yes sir", scope="Jon")
        assert book.find("hey jarvis", person="Jon") is not None
        assert book.find("hey jarvis", person="Dani") is None

    def test_a_shared_rule_fires_for_everyone(self, book):
        book.create("hey jarvis", "Hello", scope=ANYONE)
        assert book.find("hey jarvis", person="Dani") is not None

    def test_standing_instructions_are_scoped_too(self, book):
        book.create("", "always call me sir", kind=KIND_INSTRUCT, scope="Jon")
        book.create("", "keep replies short", kind=KIND_INSTRUCT)
        assert len(book.instructions("Jon")) == 2
        assert book.instructions("Dani") == ["keep replies short"]


class TestPersistence:
    def test_rules_survive_a_restart(self, settings, tmp_path):
        first = RuleBook(settings, path=tmp_path / "commands.json")
        first.create("hey jarvis", "Yes sir")
        second = RuleBook(settings, path=tmp_path / "commands.json")
        assert second.find("hey jarvis") is not None

    def test_a_corrupt_file_does_not_crash_startup(self, settings, tmp_path):
        path = tmp_path / "commands.json"
        path.write_text("{ not json", encoding="utf-8")
        assert RuleBook(settings, path=path).all() == []

    def test_a_malformed_rule_is_skipped_not_fatal(self, settings, tmp_path):
        import json
        path = tmp_path / "commands.json"
        path.write_text(json.dumps({"rules": [
            {"trigger": "ok", "response": "fine", "kind": "reply"},
            {"trigger": "bad", "response": "", "kind": "reply"},
        ]}), encoding="utf-8")
        assert len(RuleBook(settings, path=path).all()) == 1

    def test_removing_and_counting(self, book):
        rule = book.create("a", "b")
        assert book.remove(rule.id)
        assert not book.remove(rule.id)


class TestValidation:
    def test_a_rule_needs_a_response(self):
        with pytest.raises(ValueError):
            Rule(trigger="hi", response="")

    def test_a_trigger_rule_needs_a_trigger(self):
        with pytest.raises(ValueError):
            Rule(trigger="", response="hello", kind=KIND_REPLY)

    def test_an_unknown_kind_is_refused(self):
        with pytest.raises(ValueError):
            Rule(trigger="hi", response="there", kind="explode")


# --------------------------------------------------------------------------- #
# People and authority
# --------------------------------------------------------------------------- #

class TestPeople:
    def test_identifying_the_right_person(self, people):
        people.add_voice("Jon", [[1.0, 0.0, 0.0]], authority="owner")
        people.add_voice("Dani", [[0.0, 1.0, 0.0]], authority="trusted")
        assert people.identify_voice([0.99, 0.02, 0.0]).name == "Jon"
        assert people.identify_voice([0.01, 0.98, 0.0]).name == "Dani"

    def test_a_stranger_is_not_recognised(self, people):
        people.add_voice("Jon", [[1.0, 0.0, 0.0]], authority="owner")
        found = people.identify_voice([0.0, 0.0, 1.0])
        assert not found.known
        assert people.authority_for(found) is Authority.GUEST

    def test_faces_work_the_same_way(self, people):
        people.add_face("Jon", [[1.0, 0.0, 0.0]], authority="owner")
        assert people.identify_face([0.98, 0.05, 0.0]).name == "Jon"
        assert not people.identify_face([0.0, 1.0, 0.0]).known

    def test_authority_can_be_changed_later(self, people):
        people.add_voice("Dani", [[0.0, 1.0, 0.0]], authority="guest")
        people.set_authority("Dani", "trusted")
        assert people.get("Dani").authority is Authority.TRUSTED

    def test_people_survive_a_restart(self, settings, tmp_path):
        first = PeopleRegistry(settings, path=tmp_path / "people.json")
        first.add_voice("Jon", [[1.0, 0.0, 0.0]], authority="owner")
        second = PeopleRegistry(settings, path=tmp_path / "people.json")
        assert second.owner().name == "Jon"

    def test_unknown_authority_is_configurable(self, people, settings):
        settings.set("people.unknown_authority", "blocked")
        found = people.identify_voice([0.0, 0.0, 1.0])
        assert people.authority_for(found) is Authority.BLOCKED

    def test_authority_parsing_is_forgiving(self):
        assert Authority.parse("OWNER") is Authority.OWNER
        assert Authority.parse(2) is Authority.TRUSTED
        with pytest.raises(ValueError):
            Authority.parse("emperor")


class TestAuthorityEnforcement:
    """The actual ask: he can talk to it, but he can't touch my computer."""

    async def test_the_owner_is_unrestricted(self, broker, workspace):
        broker.acting_as(Authority.OWNER, "Jon")
        assert (await broker.authorize(
            Request(CAP_FS_WRITE, str(workspace / "a.txt")))).allowed

    async def test_trusted_can_ask_but_not_change(self, broker, workspace):
        broker.acting_as(Authority.TRUSTED, "Dani")
        assert (await broker.authorize(Request(CAP_WEB_SEARCH, "news"))).allowed
        assert (await broker.authorize(
            Request(CAP_FS_READ, str(workspace / "a.txt")))).allowed

        blocked = await broker.authorize(Request(CAP_FS_WRITE, str(workspace / "a.txt")))
        assert blocked.outcome is Outcome.DENY
        assert (await broker.authorize(
            Request(CAP_APP_LAUNCH, "chrome"))).outcome is Outcome.DENY

    async def test_a_guest_gets_conversation_only(self, broker, workspace):
        broker.acting_as(Authority.GUEST, "a visitor")
        assert (await broker.authorize(Request(CAP_LLM_CALL, "model"))).allowed
        assert (await broker.authorize(
            Request(CAP_WEB_SEARCH, "news"))).outcome is Outcome.DENY

    async def test_blocked_gets_nothing(self, broker):
        broker.acting_as(Authority.BLOCKED, "nuisance")
        assert (await broker.authorize(
            Request(CAP_LLM_CALL, "model"))).outcome is Outcome.DENY

    async def test_identity_can_never_grant_extra_power(self, broker):
        """A guest must not be able to do what the OWNER would be refused."""
        broker.acting_as(Authority.OWNER, "Jon")
        owner_result = await broker.authorize(Request(CAP_FS_READ, "/etc/shadow"))
        broker.acting_as(Authority.GUEST, "visitor")
        guest_result = await broker.authorize(Request(CAP_FS_READ, "/etc/shadow"))
        assert not owner_result.allowed and not guest_result.allowed

    async def test_resetting_returns_full_authority(self, broker, workspace):
        broker.acting_as(Authority.GUEST, "visitor")
        broker.reset_actor()
        assert (await broker.authorize(
            Request(CAP_FS_WRITE, str(workspace / "a.txt")))).allowed
