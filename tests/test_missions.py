"""Mission parsing, placeholder resolution, validation and execution."""

from __future__ import annotations

import pytest

from jarvis.core.mission import (
    Mission, MissionError, placeholders_in, resolve, validate,
)

CONTEXT = {
    "scripts": [
        {"title": "A", "script": "say A", "tags": ["x", "y"]},
        {"title": "B", "script": "say B", "tags": ["z"]},
    ],
    "brief": "Ship it.",
    "files": ["/a.mp4", "/b.mp4"],
    "result": {"file": "/final.mp4", "count": 2},
}


class TestPlaceholders:
    def test_whole_placeholder_keeps_the_object(self):
        assert resolve("{files}", CONTEXT) == ["/a.mp4", "/b.mp4"]
        assert isinstance(resolve("{scripts}", CONTEXT), list)

    def test_field_mapping_across_a_list(self):
        assert resolve("{scripts[*].script}", CONTEXT) == ["say A", "say B"]

    def test_indexing(self):
        assert resolve("{scripts[0].title}", CONTEXT) == "A"
        assert resolve("{scripts[1].title}", CONTEXT) == "B"

    def test_nested_field(self):
        assert resolve("{result.file}", CONTEXT) == "/final.mp4"

    def test_inline_rendering(self):
        out = resolve("Brief: {brief}", CONTEXT)
        assert out == "Brief: Ship it."

    def test_missing_key_is_empty_not_an_error(self):
        assert resolve("{nope}", CONTEXT) == ""

    def test_nested_structures_are_walked(self):
        params = {"a": ["{brief}", {"b": "{scripts[0].title}"}]}
        assert resolve(params, CONTEXT) == {"a": ["Ship it.", {"b": "A"}]}

    def test_non_strings_pass_through(self):
        assert resolve({"n": 5, "flag": True}, CONTEXT) == {"n": 5, "flag": True}


class TestSchema:
    def test_minimal_mission_parses(self):
        mission = Mission.from_dict({
            "id": "x", "name": "X",
            "steps": [{"plugin": "web_search", "params": {"query": "a"}}],
        })
        assert mission.id == "x"
        assert len(mission.steps) == 1

    def test_missing_steps_is_rejected(self):
        with pytest.raises(MissionError, match="steps"):
            Mission.from_dict({"id": "x", "name": "X"})

    def test_step_without_a_plugin_is_rejected(self):
        with pytest.raises(MissionError, match="which plugin"):
            Mission.from_dict({"id": "x", "name": "X", "steps": [{"params": {}}]})

    def test_bad_cron_is_caught_at_load_time(self):
        with pytest.raises(MissionError, match="cron"):
            Mission.from_dict({
                "id": "x", "name": "X", "schedule": "not a cron",
                "steps": [{"plugin": "llm"}],
            })

    def test_unsafe_id_is_rejected(self):
        with pytest.raises(MissionError, match="letters"):
            Mission.from_dict({
                "id": "../../etc/passwd", "name": "X",
                "steps": [{"plugin": "llm"}]})

    def test_round_trip(self):
        original = Mission.from_dict({
            "id": "x", "name": "X", "schedule": "0 2 * * *",
            "steps": [{"name": "s", "plugin": "llm", "params": {"prompt": "hi"},
                       "output_key": "out"}],
        })
        again = Mission.from_dict(original.to_dict())
        assert again.to_dict() == original.to_dict()


class TestAuthorisations:
    def test_mission_authorisations_become_grants(self):
        mission = Mission.from_dict({
            "id": "x", "name": "X",
            "authorizations": ["You have permission to post to TikTok, today only."],
            "steps": [{"plugin": "llm"}],
        })
        grants = mission.grants()
        assert len(grants) == 1
        assert "web.publish" in grants[0].capabilities

    def test_unreadable_authorisation_is_flagged_by_validation(self):
        mission = Mission.from_dict({
            "id": "x", "name": "X",
            "authorizations": ["do whatever you want"],
            "steps": [{"plugin": "llm"}],
        })
        problems = validate(mission, ["llm"])
        assert any("doesn't read as a permission" in p for p in problems)


class TestValidation:
    def test_unknown_plugin_is_flagged(self):
        mission = Mission.from_dict({
            "id": "x", "name": "X",
            "steps": [{"plugin": "does_not_exist"}]})
        problems = validate(mission, ["llm", "web_search"])
        assert any("no plugin or tool" in p for p in problems)

    def test_placeholder_with_no_producer_is_flagged(self):
        mission = Mission.from_dict({
            "id": "x", "name": "X",
            "steps": [{"plugin": "llm", "params": {"prompt": "{missing}"}}]})
        problems = validate(mission, ["llm"])
        assert any("{missing}" in p for p in problems)

    def test_valid_chain_passes(self):
        mission = Mission.from_dict({
            "id": "x", "name": "X",
            "steps": [
                {"plugin": "web_search", "params": {"query": "a"}, "output_key": "r"},
                {"plugin": "llm", "params": {"prompt": "{r}"}},
            ]})
        assert validate(mission, ["llm", "web_search"]) == []

    def test_placeholders_in_finds_references(self):
        mission = Mission.from_dict({
            "id": "x", "name": "X",
            "steps": [{"plugin": "llm", "params": {"prompt": "{a} and {b[0].c}"}}]})
        assert placeholders_in(mission) == {"a", "b"}


class TestShippedMissions:
    """The examples must actually be valid - they're the first thing anyone runs."""

    def test_every_bundled_mission_parses(self):
        from jarvis import paths
        for path in paths.BUILTIN_MISSION_DIR.glob("*.json"):
            if path.stem.upper().startswith("TEMPLATE"):
                continue
            mission = Mission.load(path)
            assert mission.steps
