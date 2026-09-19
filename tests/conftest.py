"""Test fixtures. Every test runs against a throwaway JARVIS home and workspace,
so nothing touches the developer's real config, keys or files.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_home(monkeypatch, tmp_path):
    """Point JARVIS at a temp directory for the duration of each test."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("JARVIS_HOME", str(home))
    # Keep real API keys out of tests even when the developer has them exported.
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY",
                 "DEEPGRAM_API_KEY", "CARTESIA_API_KEY", "EXA_API_KEY",
                 "TAVILY_API_KEY", "REPLICATE_API_TOKEN"):
        monkeypatch.delenv(name, raising=False)

    import jarvis.paths as paths
    paths.refresh()
    paths.ensure_dirs()
    yield home
    paths.refresh()


@pytest.fixture
def workspace(tmp_path) -> Path:
    path = tmp_path / "workspace"
    path.mkdir()
    return path


@pytest.fixture
def settings(workspace):
    from jarvis.config import Settings

    settings = Settings.load()
    settings.set("autonomy.workspace", str(workspace))
    return settings


@pytest.fixture
def memory():
    from jarvis.core.memory import Memory

    store = Memory()
    yield store
    store.close()


@pytest.fixture
def broker(settings, memory):
    from jarvis.core.permissions import PermissionBroker

    return PermissionBroker(settings, memory)


@pytest.fixture
def registry(broker, settings, memory):
    from jarvis.core.actions import register_core_tools
    from jarvis.core.computer import Computer
    from jarvis.core.tools import ToolRegistry

    computer = Computer(settings)
    reg = ToolRegistry(broker, path_resolver=computer.resolve)
    register_core_tools(reg, computer, memory, broker)
    return reg
