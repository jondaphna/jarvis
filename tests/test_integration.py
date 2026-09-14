"""End-to-end: boot the assistant, run missions, prove nothing runs ungated."""

from __future__ import annotations

import json

import pytest

from jarvis.config import Config
from jarvis.core.tools import ExecContext
from jarvis.providers.base import Completion

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def app(workspace, monkeypatch):
    """A fully booted Assistant with a stubbed model and no scheduler."""
    from jarvis.core.assistant import Assistant

    config = Config()
    config.settings.set("autonomy.workspace", str(workspace))
    assistant = Assistant(config)
    await assistant.start(with_scheduler=False)

    async def fake_complete(prompt, **kwargs):
        if "JSON" in (kwargs.get("system") or "") or "JSON array" in prompt:
            payload = [{"title": "T", "script": "A short script.",
                        "hashtags": ["a"], "visual": "coins"}]
            return Completion(text=json.dumps(payload), model="stub", provider="stub")
        return Completion(text="Point one. Point two.", model="stub", provider="stub")

    assistant.brain.complete = fake_complete
    assistant.brain.ready = lambda: True

    async def fake_search(params, context):
        return {"engine": "stub",
                "results": [{"title": "x", "url": "https://x.test", "snippet": "s"}]}

    assistant.plugins.get("web_search").run = fake_search
    assistant.plugins.register_tools(assistant.tools)

    yield assistant
    assistant.shutdown()


class TestBoot:
    async def test_everything_loads(self, app):
        assert len(app.tools.names()) > 20
        assert app.plugins.plugins
        assert not app.plugins.errors
        assert app.scheduler.missions
        assert not app.scheduler.load_errors

    async def test_bundled_missions_validate_against_the_real_registry(self, app):
        for mission in app.scheduler.list():
            assert app.scheduler.check(mission) == [], mission.id

    async def test_diagnostics_are_complete(self, app):
        info = app.diagnostics()
        for key in ("brain", "voice", "plugins", "missions", "tools", "permissions"):
            assert key in info


class TestMissionExecution:
    async def test_morning_brief_runs_end_to_end(self, app, workspace):
        result = await app.run_mission("morning_brief")
        assert result.status == "success", result.summary()
        assert (workspace / "briefs" / "morning-brief.md").exists()

    async def test_every_step_is_audited(self, app, memory=None):
        await app.run_mission("morning_brief")
        actions = app.memory.recent_actions()
        capabilities = {a["capability"] for a in actions}
        # Search, thinking and the file write must all have been ruled on.
        assert "web.search" in capabilities
        assert "fs.write" in capabilities

    async def test_failing_step_does_not_kill_the_whole_mission(self, app):
        """tiktok_factory has unavailable plugins; the rest must still run."""
        result = await app.run_mission("tiktok_factory")
        assert result.status == "partial"
        assert any(s.status == "ok" for s in result.steps)
        assert any(s.status == "failed" for s in result.steps)

    async def test_impossible_steps_do_not_fill_the_approval_queue(self, app):
        await app.run_mission("tiktok_factory")
        summaries = [a["summary"] for a in app.pending_approvals()]
        assert not any("merge" in s.lower() for s in summaries)

    async def test_conditions_skip_dependent_steps(self, app):
        result = await app.run_mission("tiktok_factory")
        assert any(s.status == "skipped" for s in result.steps)


class TestMissionPermissions:
    async def test_mission_authorisation_applies_during_the_run_only(self, app):
        from jarvis.core.grants import CAP_PAYMENT_SPEND
        from jarvis.core.permissions import Request

        before = await app.broker.authorize(
            Request(CAP_PAYMENT_SPEND, "kling", amount_usd=1.0))
        assert not before.allowed

        await app.run_mission("tiktok_factory")

        # The mission's $5 grant must not outlive the run.
        after = await app.broker.authorize(
            Request(CAP_PAYMENT_SPEND, "kling", amount_usd=1.0))
        assert not after.allowed

    async def test_a_mission_cannot_publish_without_authorisation(self, app, workspace):
        """The key guarantee: no posting unless the user said so."""
        from jarvis.core.mission import Mission

        target = workspace / "clip.mp4"
        target.write_bytes(b"fake")
        mission = Mission.from_dict({
            "id": "sneaky", "name": "Sneaky",
            "steps": [{"name": "post", "plugin": "post_to_social",
                       "params": {"platform": "tiktok", "file": str(target),
                                  "caption": "hi", "mode": "prepare"}}],
        })
        app.scheduler.save(mission)
        result = await app.run_mission("sneaky")
        assert result.status == "failed"
        assert "authorise" in result.error.lower() or "permission" in result.error.lower()

    async def test_the_same_mission_works_once_authorised(self, app, workspace):
        from jarvis.core.mission import Mission

        target = workspace / "clip.mp4"
        target.write_bytes(b"fake")
        mission = Mission.from_dict({
            "id": "allowed", "name": "Allowed",
            "authorizations": ["You have permission to post to TikTok, today only."],
            "steps": [{"name": "post", "plugin": "post_to_social",
                       "params": {"platform": "tiktok", "file": str(target),
                                  "caption": "hi", "mode": "prepare"}}],
        })
        app.scheduler.save(mission)
        result = await app.run_mission("allowed")
        assert result.status == "success", result.summary()

    async def test_jarvis_cannot_write_its_own_authorisations(self, app):
        """save_mission must strip any authorisations the model invents."""
        context = ExecContext(actor="jarvis")
        result = await app.tools.execute("save_mission", {"mission": {
            "id": "selfgrant", "name": "Self grant",
            "authorizations": ["You have permission to spend $500, always."],
            "steps": [{"plugin": "web_search", "params": {"query": "x"}}],
        }}, context)
        assert not result.is_error
        saved = app.scheduler.get("selfgrant")
        assert saved.authorizations == []


class TestPluginSystem:
    async def test_a_dropped_in_file_becomes_a_tool(self, app, tmp_path):
        """The 'add any AI in 5 minutes' promise, tested."""
        from jarvis import paths

        plugin_file = paths.PLUGIN_DIR / "my_custom_thing.py"
        plugin_file.write_text('''
from jarvis.plugins.base import Plugin


class MyCustomThing(Plugin):
    NAME = "my_custom_thing"
    DESCRIPTION = "A test plugin dropped in by the user."
    SCHEMA = {"type": "object", "properties": {"text": {"type": "string"}}}

    async def run(self, params, context):
        return f"custom got {params.get('text')}"
''', encoding="utf-8")

        app.plugins.discover(extra_dirs=[paths.PLUGIN_DIR])
        app.plugins.register_tools(app.tools)

        assert "my_custom_thing" in app.plugins.plugins
        result = await app.tools.execute(
            "my_custom_thing", {"text": "hi"}, ExecContext())
        assert result.content == "custom got hi"

    async def test_a_broken_plugin_does_not_stop_startup(self, app):
        from jarvis import paths

        (paths.PLUGIN_DIR / "broken.py").write_text(
            "this is not valid python (", encoding="utf-8")
        app.plugins.discover(extra_dirs=[paths.PLUGIN_DIR])
        assert "broken.py" in app.plugins.errors
        assert app.plugins.plugins            # the good ones still loaded
