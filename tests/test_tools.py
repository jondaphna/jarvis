"""Tool execution, and the gate every call passes through."""

from __future__ import annotations

from pathlib import Path

import pytest

from jarvis.core.tools import ExecContext

pytestmark = pytest.mark.asyncio


@pytest.fixture
def context():
    return ExecContext(actor="jarvis")


class TestPathAgreement:
    """The gate and the handler must judge the same path. This was a real bug."""

    async def test_bare_filename_means_workspace(self, registry, context, workspace):
        result = await registry.execute(
            "write_file", {"path": "notes.txt", "content": "hi"}, context)
        assert not result.is_error
        assert (workspace / "notes.txt").read_text() == "hi"

    async def test_optional_path_defaults_to_workspace(self, registry, context, workspace):
        (workspace / "a.txt").write_text("x")
        result = await registry.execute("list_files", {}, context)
        assert not result.is_error
        assert "a.txt" in result.content

    async def test_move_checks_both_ends(self, registry, context, workspace, tmp_path):
        (workspace / "a.txt").write_text("x")
        outside = tmp_path / "outside" / "a.txt"
        result = await registry.execute(
            "move_file", {"source": "a.txt", "destination": str(outside)}, context)
        # Source is in the workspace, but the destination isn't - must not slip through.
        assert result.is_error
        assert not outside.exists()


class TestGating:
    async def test_protected_path_is_refused(self, registry, context):
        result = await registry.execute(
            "read_file", {"path": str(Path.home() / ".ssh" / "id_rsa")}, context)
        assert result.is_error
        assert "protected" in result.content.lower()

    async def test_dangerous_command_is_refused(self, registry, context):
        result = await registry.execute("run_command", {"command": "rm -rf /"}, context)
        assert result.is_error
        assert "never-run" in result.content.lower()

    async def test_allow_app_cannot_be_self_granted(self, registry, context):
        """JARVIS may ask to widen the allowlist; it may not do it unilaterally."""
        result = await registry.execute("allow_app", {"name": "photoshop"}, context)
        assert result.is_error

    async def test_request_permission_only_queues(self, registry, context, memory):
        result = await registry.execute(
            "request_permission", {"what": "Buy credits", "why": "for video"}, context)
        assert not result.is_error
        assert len(memory.pending_approvals()) == 1


class TestExecution:
    async def test_unknown_tool_is_reported(self, registry, context):
        result = await registry.execute("no_such_tool", {}, context)
        assert result.is_error

    async def test_handler_exception_becomes_an_error_result(self, registry, context):
        result = await registry.execute("read_file", {"path": "missing.txt"}, context)
        assert result.is_error
        assert "doesn't exist" in result.content

    async def test_memory_tools_work(self, registry, context):
        await registry.execute("remember", {"key": "biz", "value": "agency"}, context)
        result = await registry.execute("recall", {"key": "biz"}, context)
        assert "agency" in result.content

    async def test_attended_only_tools_are_hidden_unattended(self, registry):
        unattended = ExecContext(actor="mission", unattended=True)
        result = await registry.execute("type_text", {"text": "hello"}, unattended)
        assert result.is_error
        assert "keyboard" in result.content.lower()

    async def test_unattended_specs_exclude_attended_only_tools(self, registry):
        names = {spec["name"] for spec in registry.specs(unattended=True)}
        assert "type_text" not in names
        assert "write_file" in names


class TestSecretRedaction:
    async def test_secrets_are_not_logged(self, registry, context, memory):
        await registry.execute(
            "write_file",
            {"path": "x.txt", "content": "ok", "api_key": "sk-secret-123"}, context)
        blob = str(memory.recent_actions())
        assert "sk-secret-123" not in blob
