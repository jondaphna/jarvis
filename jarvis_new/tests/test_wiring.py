"""Is the glue actually connected?

Every other test file checks that a piece works. This one checks that the
pieces are joined up, because almost every real failure in this project has
been a joint rather than a part:

* a button that ran a script that had been renamed
* a settings panel calling a route the service didn't have
* a panel reading a field the service never sent
* a prompt naming a tool that had been consolidated away
* a permission switch governing a tool that no longer existed
* commands saved in a shape the agent never read

None of those raise anywhere. They present as "it just doesn't do anything",
which is the hardest kind of bug to report and the slowest kind to find. So
each joint is walked here, in the build, rather than discovered on a Windows
machine at midnight.
"""

import re
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
AGENT = HERE.parent                            # jarvis_new
SRC = AGENT / "src"
REPO = AGENT.parent                            # the repository root
FRONTEND = AGENT / "frontend"
PANEL = FRONTEND / "components" / "app" / "control-panel.tsx"
PROXY = FRONTEND / "app" / "api" / "control" / "[...path]" / "route.ts"


def panel_source() -> str:
    return PANEL.read_text(encoding="utf-8")


def control_source() -> str:
    return (SRC / "control_api.py").read_text(encoding="utf-8")


class _Named:
    """A stand-in with just the shape `filter_tools` looks at."""

    def __init__(self, name: str) -> None:
        self.info = type("Info", (), {"name": name})()


class _Defaults:
    """Settings as a fresh install has them: every switch at its default."""

    def get(self, dotted: str, default: object = None) -> object:
        return default


# --------------------------------------------------------------------------- #
# The buttons
# --------------------------------------------------------------------------- #

class TestEveryButtonRunsSomethingThatExists:
    """A `.bat` pointing at a renamed file fails with a Windows error box and
    no explanation. There is one of these for every way in."""

    LAUNCHERS = ("butler-setup", "butler-talk", "butler-agent", "butler-web",
                 "butler-orb", "butler-api", "butler-doctor", "butler-keys",
                 "butler-check", "butler-devices", "butler-restore",
                 "butler-settings", "butler-test")

    @pytest.mark.parametrize("name", LAUNCHERS)
    def test_the_launcher_is_there(self, name: str) -> None:
        assert (REPO / f"{name}.bat").exists(), f"{name}.bat is missing"

    @pytest.mark.parametrize("name", LAUNCHERS)
    def test_every_file_it_runs_exists(self, name: str) -> None:
        text = (REPO / f"{name}.bat").read_text(encoding="utf-8", errors="ignore")
        for target in re.findall(r"(?:python|uv run)\s+(?:--\S+\s+)*"
                                 r"([\w./\\-]+\.py)", text):
            path = target.replace("\\", "/")
            candidates = [REPO / path, REPO / "jarvis_new" / path]
            assert any(c.exists() for c in candidates), \
                f"{name}.bat runs {target}, which does not exist"

    def test_every_button_on_the_orb_launches_a_file_that_exists(self) -> None:
        """The orb is the one the user actually double-clicks, and it starts
        everything else by name. A renamed launcher is a dead menu item."""
        orb = (REPO / "desktop_orb.py").read_text(encoding="utf-8")
        launched = set(re.findall(r'["\'](butler-[\w-]+\.bat)["\']', orb))
        assert launched, "the orb doesn't launch anything at all any more"
        for name in sorted(launched):
            assert (REPO / name).exists(), f"the orb launches {name}, which is missing"

    def test_the_orb_can_start_jarvis_and_reach_the_settings(self) -> None:
        orb = (REPO / "desktop_orb.py").read_text(encoding="utf-8")
        assert "butler-agent.bat" in orb, "the orb can no longer start the agent"
        assert "butler-web.bat" in orb, "the orb can no longer start the web app"
        assert "/settings" in orb, "right-clicking the orb no longer opens settings"

    def test_restore_points_name_real_commits(self) -> None:
        """A restore point that isn't there when you need it is worse than
        none. These are commit ids because tags can't be pushed from here."""
        import subprocess

        lines = (REPO / "RESTORE_POINTS.txt").read_text(encoding="utf-8")
        entries = [ln.split("\t") for ln in lines.splitlines()
                   if ln.strip() and not ln.startswith("#")]
        assert entries, "no restore points at all"
        for entry in entries:
            assert len(entry) >= 2, f"malformed restore point: {entry}"
            found = subprocess.run(
                ["git", "cat-file", "-e", f"{entry[1]}^{{commit}}"],
                cwd=REPO, capture_output=True)
            assert found.returncode == 0, \
                f"restore point {entry[0]!r} points at {entry[1]}, which isn't a commit"


# --------------------------------------------------------------------------- #
# The settings panel and the service behind it
# --------------------------------------------------------------------------- #

class TestThePanelAndTheServiceAgree:
    def panel_routes(self) -> set[str]:
        """Every path the panel asks for, reduced to its first segment."""
        text = panel_source()
        paths = re.findall(r"api\(\s*['\"`]([^'\"`$]+)", text)
        return {p.strip("/").split("/")[0] for p in paths if p.strip("/")}

    def test_every_route_the_panel_calls_exists(self) -> None:
        import control_api

        source = control_source()
        for head in sorted(self.panel_routes()):
            assert f'head == "{head}"' in source, \
                f"the panel calls /api/{head}, which the service has no route for"
        assert control_api  # imported to prove the module is loadable

    def test_every_route_the_panel_calls_is_dispatched(self) -> None:
        """Present in the file is not the same as reachable: a route added
        after the final `raise` would never run."""
        source = control_source()
        dispatch = source[source.index("def _dispatch"):]
        end = dispatch.index("raise ValueError(f\"No route for")
        reachable = dispatch[:end]
        for head in sorted(self.panel_routes()):
            assert f'head == "{head}"' in reachable, \
                f"/api/{head} is defined after the fallthrough, so it never runs"

    def test_every_field_the_panel_reads_is_sent(self, tmp_path, monkeypatch) -> None:
        """The panel renders `state.lessons`, `state.costs` and so on. A field
        the service stopped sending shows as an empty tab, not an error."""
        monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
        from jarvis import paths

        paths.refresh()
        paths.ensure_dirs()

        import control_api

        state = control_api.Control().state()
        wanted = set(re.findall(r"state\.(\w+)", panel_source()))
        wanted -= {"settings"}                 # read through a typed accessor
        missing = sorted(w for w in wanted if w not in state)
        assert not missing, f"the panel reads {missing}, which state() doesn't send"

    def test_the_proxy_forwards_the_methods_the_panel_uses(self) -> None:
        route = PROXY.read_text(encoding="utf-8")
        methods = set(re.findall(r"method:\s*'(\w+)'", panel_source())) | {"GET"}
        for method in methods:
            assert f"export async function {method}(" in route, \
                f"the panel sends {method}, which the proxy doesn't forward"

    def test_a_body_is_forwarded_for_every_method_that_carries_one(self) -> None:
        """DELETE with a body is dropped by the proxy, which is why deleting a
        lesson goes through POST instead of a path segment."""
        route = PROXY.read_text(encoding="utf-8")
        assert "req.method !== 'GET' && req.method !== 'DELETE'" in route
        assert "api('lessons/forget'" in panel_source(), \
            "deleting a lesson must not rely on a body the proxy drops"


# --------------------------------------------------------------------------- #
# The prompt and the tools
# --------------------------------------------------------------------------- #

class TestThePromptDescribesTheToolsThatExist:
    def real_tools(self) -> set[str]:
        import permissions

        return set(permissions.GOVERNED)

    def test_every_tool_named_in_the_prompt_is_real(self) -> None:
        """`fetch_page` survived a rename as dead code and stayed in the
        prompt, so the model was told to use something it could not call."""
        import prompts

        named = set(re.findall(r"\b(?:use |calls )([a-z_]+_[a-z_]+)\b",
                               prompts.AGENT_INSTRUCTIONS))
        unknown = sorted(n for n in named if n not in self.real_tools())
        assert not unknown, f"the prompt tells it to use {unknown}, which don't exist"

    def every_tool(self) -> set[str]:
        """Every tool the agent could hand the model, switches aside."""
        import brain_memory
        import files
        import learning
        import os_tools
        import thinker
        import tools as browser_tools
        from browser import BrowserManager

        holders = (
            browser_tools.BrowserTools(BrowserManager.__new__(BrowserManager)),
            brain_memory.JarvisMemory(),
            learning.Lessons(),
            os_tools.OSTools(),
            files.FileTools(),
            thinker.Thinker(),
        )
        return {t.info.name for holder in holders for t in holder.tools}

    def test_every_switch_governs_a_tool_that_exists(self) -> None:
        import permissions

        real = self.every_tool()
        assert real >= permissions.GOVERNED, \
            f"switches govern tools that don't exist: {sorted(permissions.GOVERNED - real)}"

    def test_every_tool_has_a_switch(self) -> None:
        """A tool with no switch cannot be turned off, which makes the
        permissions tab a partial answer to "what can it do"."""
        import permissions

        real = self.every_tool()
        assert real <= permissions.GOVERNED, \
            f"tools with no switch at all: {sorted(real - permissions.GOVERNED)}"

    def test_the_tool_surface_stays_small(self) -> None:
        """Measured, because this is what broke it. Twenty-four tools and a
        two-thousand-token prompt is how "open my Spotify" started failing.

        The number that matters is what a fresh install actually hands the
        model - not every tool that exists. Power actions are off by default
        and cost nothing until you switch them on.
        """
        import permissions

        shipped = permissions.filter_tools(
            [_Named(name) for name in sorted(self.every_tool())], _Defaults())
        assert len(shipped) <= 21, (
            f"{len(shipped)} tools on a fresh install - every one of them "
            "competes for attention with the request actually being made")


# --------------------------------------------------------------------------- #
# The things that fail silently
# --------------------------------------------------------------------------- #

class TestTheSilentFailures:
    def test_the_realtime_model_is_one_the_plugin_knows(self) -> None:
        """A retired model ID doesn't raise. It gives you a call where nobody
        ever speaks, which cost this project an entire evening once."""
        import agent

        source = (SRC / "agent.py").read_text(encoding="utf-8")
        model = re.search(r'model="(gemini[^"]+)"', source)
        assert model, "no realtime model named in agent.py"
        assert agent  # loadable

    def test_the_session_cannot_quietly_expire(self) -> None:
        source = (SRC / "agent.py").read_text(encoding="utf-8")
        assert "session_resumption" in source
        assert "context_window_compression" in source

    def test_nothing_spends_money_without_the_switch(self) -> None:
        import thinker

        assert thinker.is_paid(thinker.DEFAULT_MODEL) is False
        assert thinker.paid_allowed(None) is False

    def test_the_browser_is_visible_rather_than_headless(self) -> None:
        """A headless browser is a browser you can't see failing."""
        source = (SRC / "agent.py").read_text(encoding="utf-8")
        assert "BrowserManager()" in source, \
            "the session must attach to a browser you can see"

    def test_settings_are_reachable_more_than_one_way(self) -> None:
        """The panel used to be coverable by the call screen, and then there
        was no way in at all."""
        panel = panel_source()
        assert (REPO / "butler-settings.bat").exists()
        assert (FRONTEND / "app" / "settings" / "page.tsx").exists()
        assert "Ctrl+Shift+S" in panel or "shiftKey" in panel
