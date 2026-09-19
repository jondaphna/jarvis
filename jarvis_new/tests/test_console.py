"""The console's own endpoints, and what they promise the page.

The dashboard polls three things while it is open - the content engine, the
worker host and the routines - and draws a button for each one it can change.
A panel is only as honest as those payloads, so this file holds them to two
things:

* **Every key the panel draws is present**, on a machine where nothing has
  run yet. The first time anyone opens this, there is no database, no job and
  no key in the vault, and "nothing has happened yet" must render as a quiet
  panel rather than as a broken one.
* **Nothing here changes what a fresh install does.** The matrix can toggle a
  capability, but the defaults it toggles from - and the number of tools a new
  machine hands the model - are the same as before the dashboard existed.
"""

import pytest

import control_api
import permissions


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A throwaway JARVIS folder, so tests never touch the real one."""
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    from jarvis import paths

    paths.refresh()
    paths.ensure_dirs()
    yield tmp_path / "home"
    monkeypatch.delenv("JARVIS_HOME", raising=False)
    paths.refresh()


# --------------------------------------------------------------------------- #
# What the panels are given
# --------------------------------------------------------------------------- #

def test_the_content_panel_gets_every_key_it_draws(home) -> None:
    payload = control_api.Control().content()

    for key in ("enabled", "workers", "jobs", "scripts", "counts", "stages",
                "style"):
        assert key in payload, f"the content panel draws {key}, which isn't sent"
    for status in ("queued", "running", "done", "failed"):
        assert status in payload["counts"]
    assert isinstance(payload["jobs"], list)
    assert isinstance(payload["stages"], list)


def test_a_machine_with_nothing_on_it_reads_as_empty_not_broken(home) -> None:
    """Before the first job there is no content table. That is the normal
    state on day one, and it must not surface as an error banner."""
    payload = control_api.Control().content()

    assert payload["jobs"] == []
    assert payload["counts"]["done"] == 0
    assert payload["error"] == ""


def test_the_worker_panel_gets_every_key_it_draws(home) -> None:
    status = control_api.Control().workers()

    for key in ("running", "paused", "queued", "in_progress", "concurrency",
                "jobs"):
        assert key in status, f"the live panel draws {key}, which isn't sent"


def test_pausing_and_resuming_go_through_the_service(home) -> None:
    control = control_api.Control()
    try:
        assert control.set_workers_paused(True) == {"paused": True}
        assert control.workers()["paused"] is True
        assert control.set_workers_paused(False) == {"paused": False}
        assert control.workers()["paused"] is False
    finally:
        import workers

        workers.host().set_paused(False)


def test_readiness_is_not_recalculated_on_every_poll(home, monkeypatch) -> None:
    """It decrypts the vault and searches PATH. Once a minute is plenty; once
    every three seconds for as long as the dashboard is open is not."""
    from content import pipeline

    calls = []
    monkeypatch.setattr(pipeline, "readiness",
                        lambda: calls.append(1) or {"stages": [], "style": {}})

    control = control_api.Control()
    control.content()
    control.content()
    control.content()

    assert len(calls) == 1, f"readiness was worked out {len(calls)} times"


# --------------------------------------------------------------------------- #
# The switch the panel offers, and the one it must not move
# --------------------------------------------------------------------------- #

def test_the_content_engine_is_still_off_on_a_fresh_install(home) -> None:
    """The matrix shows this switch prominently. Showing it must not arm it."""
    entries = {entry["key"]: entry for entry in control_api.Control().permissions()}

    assert entries["content"]["enabled"] is False
    assert entries["content"]["default"] is False


def test_queueing_scripts_is_refused_while_the_engine_is_off(home) -> None:
    """A button that appears to work while the capability is off is how you
    wait all evening for a job nobody was going to run."""
    with pytest.raises(ValueError, match="switched off"):
        control_api.Control().queue_content("anything at all")


def test_an_empty_topic_is_refused_before_a_worker_is_woken(home) -> None:
    with pytest.raises(ValueError):
        control_api.Control().queue_content("   ")


def test_the_matrix_shows_the_default_for_every_switch(home) -> None:
    """The panel marks a machine that has drifted from its defaults, which it
    can only do if each switch says what its default was."""
    for entry in control_api.Control().permissions():
        assert "default" in entry, f"{entry['key']} doesn't say what it defaults to"
        assert isinstance(entry["default"], bool)


def test_the_console_adds_no_tools_to_the_model(home) -> None:
    """The dashboard is a page, not a capability. Nothing here may widen what
    a fresh install hands the model."""
    governed = {tool for capability in permissions.CAPABILITIES
                for tool in capability.tools}

    assert "content_engine_status" in governed
    assert not any(tool.startswith("dashboard") for tool in governed)
