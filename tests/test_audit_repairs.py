"""Regressions for the September 2026 re-audit's findings.

Each test here is a defect the re-audit reproduced against commit `4528362`,
inverted: where the audit's fixture passed by demonstrating the bug, these
pass by demonstrating it is gone. They are written as behaviour rather than
as structure - "the handler was never called" rather than "the registration
has this keyword" - because the structural version goes green the moment
somebody moves the code and says nothing about whether the file was read.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from jarvis.core.grants import (
    CAP_COMMS_SEND, CAP_FS_READ, CAP_FS_WRITE, Grant,
)


# --------------------------------------------------------------------------- #
# F34 - copying a file reads the source, and must say so
# --------------------------------------------------------------------------- #

class TestCopyDeclaresItsSourceRead:
    """A destination-write permission is not permission to read any file the
    account can open, one copy at a time."""

    def registration(self):
        from jarvis.core.actions import register_core_tools
        from jarvis.core.tools import ToolRegistry

        registry = ToolRegistry(broker=None)
        calls: list[dict[str, Any]] = []

        class Recorder:
            """Stands in for the real Computer. Records, touches nothing."""

            def __getattr__(self, name):
                def record(*args, **kwargs):
                    calls.append({"handler": name, "args": args})
                    return "copied"
                return record

        register_core_tools(registry, Recorder(), memory=None, broker=None)
        return registry, calls

    def test_copy_requires_read_on_its_source(self):
        registry, _ = self.registration()
        tool = registry.get("copy_file")
        assert tool is not None
        required = {(capability, key) for capability, key in tool.also_requires}
        assert (CAP_FS_READ, "source") in required, (
            "copy_file must declare that it reads its source; without it, "
            "permission to write one folder authorises reading every file "
            "the account can open")

    def test_the_destination_write_alone_is_declared_on_the_tool(self):
        """The original capability is unchanged - this adds, it does not move."""
        registry, _ = self.registration()
        tool = registry.get("copy_file")
        assert tool.capability == CAP_FS_WRITE
        assert tool.resource_key == "destination"

    def test_a_denied_source_copies_nothing(self):
        """The acceptance the audit asked for: permit the destination, deny
        the source, and count zero calls into the copy handler."""
        from jarvis.core.permissions import (
            Decision, Outcome, PermissionDenied, Risk,
        )

        registry, calls = self.registration()

        def decide(request, outcome: Outcome, reason: str) -> Decision:
            return Decision(outcome=outcome, risk=Risk.LOW, reason=reason,
                            request=request)

        class SourceDenyingBroker:
            """Allows everything except reading, which it refuses."""

            def __init__(self):
                self.seen: list[tuple[str, str | None]] = []

            async def require(self, request):
                self.seen.append((request.capability, request.resource))
                if request.capability == CAP_FS_READ:
                    raise PermissionDenied(
                        decide(request, Outcome.DENY,
                               "not allowed to read that"))
                return decide(request, Outcome.ALLOW, "fine")

        broker = SourceDenyingBroker()
        registry.broker = broker

        from jarvis.core.tools import ExecContext

        result = asyncio.run(registry.execute(
            "copy_file",
            {"source": "/somewhere/private.txt", "destination": "/tmp/copy.txt"},
            ExecContext(actor="test")))

        assert result.is_error
        assert calls == [], "the copy handler ran despite the source being denied"
        assert CAP_FS_READ in {capability for capability, _ in broker.seen}


# --------------------------------------------------------------------------- #
# F05 - a grant scoped to a target needs a target
# --------------------------------------------------------------------------- #

class TestAScopedGrantNeedsATarget:
    """`covers()` used to skip the scope check entirely when the caller named
    no resource, so a grant limited to one recipient covered a request with no
    recipient at all - the one request it could not have been meant for."""

    def grant(self) -> Grant:
        return Grant(capabilities=(CAP_COMMS_SEND,),
                     resources=("alice@example.test",), uses=None)

    def test_the_named_target_is_still_covered(self):
        assert self.grant().covers(CAP_COMMS_SEND, "alice@example.test") is True

    def test_a_different_target_is_not(self):
        assert self.grant().covers(CAP_COMMS_SEND, "mallory@example.test") is False

    @pytest.mark.parametrize("missing", [None, "", "   "])
    def test_a_missing_target_is_not(self, missing):
        assert self.grant().covers(CAP_COMMS_SEND, missing) is False

    def test_an_unscoped_grant_is_unaffected(self):
        """A grant that says "anything" still means anything. The fix is about
        scoped grants; widening this one would be a different bug."""
        anything = Grant(capabilities=(CAP_COMMS_SEND,), resources=("*",),
                         uses=None)
        assert anything.covers(CAP_COMMS_SEND, None) is True
        assert anything.covers(CAP_COMMS_SEND, "anyone@example.test") is True


# --------------------------------------------------------------------------- #
# F04 / F10 - settings that could not be read are not a policy
# --------------------------------------------------------------------------- #

class TestADamagedSettingsFileIsNotAPolicy:
    def test_load_marks_a_file_it_could_not_read(self, isolated_home):
        from jarvis import paths
        from jarvis.config import Settings

        paths.ensure_dirs()
        paths.CONFIG_FILE.write_text('{"permissions":', encoding="utf-8")

        loaded = Settings.load()
        assert loaded.unreadable is True, (
            "a settings file that could not be parsed must be flagged, not "
            "silently replaced by permissive defaults")

    def test_a_readable_file_is_not_marked(self, isolated_home):
        from jarvis import paths
        from jarvis.config import Settings

        paths.ensure_dirs()
        paths.CONFIG_FILE.write_text('{"permissions": {"apps": false}}',
                                     encoding="utf-8")
        assert Settings.load().unreadable is False

    def test_a_missing_file_is_not_marked(self, isolated_home):
        """No settings yet is the ordinary first run, not a damaged file."""
        from jarvis.config import Settings

        assert Settings.load().unreadable is False
