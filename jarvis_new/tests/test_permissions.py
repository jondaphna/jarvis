"""Switches that actually switch things off.

A permission implemented as a line in the prompt is theatre: the tool is still
there, the model can still call it, and one persuasive sentence later it does.
These tests exist to keep the enforcement real - the tool is removed from the
list the model is handed, so there is nothing to argue with.
"""

import permissions
import pytest


class FakeSettings:
    def __init__(self, values: dict | None = None) -> None:
        self.values = values or {}

    def get(self, key, default=None):
        return self.values.get(key, default)


class TestTheSwitches:
    def test_everything_has_a_home(self) -> None:
        """A tool governed by no switch cannot be turned off, which is worth
        knowing about deliberately rather than by accident."""
        assert len(permissions.GOVERNED) >= 20

    def test_no_tool_is_claimed_by_two_switches(self) -> None:
        """Otherwise turning one off would half-disable another."""
        seen: set[str] = set()
        for capability in permissions.CAPABILITIES:
            clash = seen & set(capability.tools)
            assert not clash, f"{capability.key} also claims {clash}"
            seen.update(capability.tools)

    def test_power_is_off_until_you_ask(self) -> None:
        assert permissions.BY_KEY["power"].default is False

    def test_an_unknown_key_is_permitted(self) -> None:
        """A capability this build doesn't know about must not silently
        disable a tool."""
        assert permissions.allowed("not-a-capability", FakeSettings())


class TestEnforcement:
    class Tool:
        def __init__(self, name: str) -> None:
            self.info = type("Info", (), {"name": name})()

    def tools(self) -> list:
        return [self.Tool(n) for n in
                ("open_url", "click", "type_text", "open_app", "end_call")]

    def test_switching_something_off_removes_its_tools(self) -> None:
        settings = FakeSettings({"permissions.control_web": False})
        kept = {t.info.name for t in
                permissions.filter_tools(self.tools(), settings)}
        assert "click" not in kept
        assert "type_text" not in kept
        assert "open_url" in kept

    def test_ungoverned_tools_always_survive(self) -> None:
        """Ending the call is not something to be locked out of."""
        settings = FakeSettings({f"permissions.{c.key}": False
                                 for c in permissions.CAPABILITIES})
        kept = {t.info.name for t in
                permissions.filter_tools(self.tools(), settings)}
        assert kept == {"end_call"}

    def test_all_on_changes_nothing(self) -> None:
        settings = FakeSettings({f"permissions.{c.key}": True
                                 for c in permissions.CAPABILITIES})
        tools = self.tools()
        assert permissions.filter_tools(tools, settings) == tools

    def test_broken_settings_fall_back_to_the_defaults(self) -> None:
        """A settings file that cannot be read must not hand out every
        capability, nor take them all away."""

        class Exploding:
            def get(self, key, default=None):
                raise RuntimeError("nope")

        assert permissions.allowed("browse", Exploding()) is True
        assert permissions.allowed("power", Exploding()) is False


class TestWhatTheModelIsTold:
    def test_nothing_is_said_when_all_is_allowed(self) -> None:
        settings = FakeSettings({f"permissions.{c.key}": True
                                 for c in permissions.CAPABILITIES})
        assert permissions.summary(settings) == ""

    def test_it_is_told_what_it_cannot_do(self) -> None:
        """So it can say so plainly instead of being confused by a missing
        tool and casting about for another way."""
        settings = FakeSettings({"permissions.apps": False})
        text = permissions.summary(settings)
        assert "Switched off" in text
        assert "open apps" in text.lower()

    def test_the_interface_gets_the_full_picture(self) -> None:
        entries = permissions.current(FakeSettings())
        assert len(entries) == len(permissions.CAPABILITIES)
        assert all({"key", "label", "detail", "tools", "enabled", "risk"}
                   <= set(entry) for entry in entries)


@pytest.mark.parametrize("key", [c.key for c in permissions.CAPABILITIES])
def test_every_switch_governs_at_least_one_tool(key: str) -> None:
    assert permissions.BY_KEY[key].tools
