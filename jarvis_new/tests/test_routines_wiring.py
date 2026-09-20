"""Is the routines engine actually joined up to the things that use it?

Every failure this project has had in this area was a joint rather than a
part: the old generation's mission scheduler is correct, well tested, and has
never once run in `jarvis_new/`, because nothing constructs the object it
hangs off. A module that works perfectly and is never called is the exact
shape of the bug these tests exist to catch.
"""

import inspect
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"


class TestTheAgentStartsIt:
    """These used to assert the opposite, and they were right to at the time.

    The ticker was started in the call entrypoint and stopped by a shutdown
    callback, and these tests held that wiring in place. That wiring was the
    bug: a ticker that starts with a call and stops with it is a ticker that
    does not exist while nobody is talking, which is precisely when a standing
    routine is supposed to fire.

    So the contract they protect has moved rather than gone. What the ticker
    must not be tied to is a job; what it must be tied to is the process. The
    tests below are that, the other way round.
    """

    def test_the_agent_imports_routines(self):
        assert "import routines" in (SRC / "agent.py").read_text(encoding="utf-8")

    def test_the_ticker_does_not_start_inside_the_call(self):
        """The `while I'm away` case: nobody is on a call at 07:30."""
        source = (SRC / "agent.py").read_text(encoding="utf-8")
        entrypoint = source[source.index("async def my_agent("):
                            source.index("\nif __name__ ==")]
        assert "routines.start_routines()" not in entrypoint

    def test_the_ticker_is_not_stopped_when_the_call_ends(self):
        source = (SRC / "agent.py").read_text(encoding="utf-8")
        assert "ctx.add_shutdown_callback(routines.stop_routines)" not in source

    def test_the_services_come_up_with_the_process(self):
        """`serve_in_background` binds the control port, and `control_api.serve`
        starts the services behind that bind - so whichever process owns the
        port owns the ticker, and it is up before the first job arrives."""
        source = (SRC / "agent.py").read_text(encoding="utf-8")
        main = source[source.index("if __name__ =="):]
        assert "serve_in_background()" in main
        assert main.index("serve_in_background()") < main.index("cli.run_app")

    def test_they_start_after_the_workers_they_live_on(self):
        """The ticker lives on the worker host's loop, so the host comes first.
        That ordering moved into `services.start` with everything else."""
        source = (SRC / "services.py").read_text(encoding="utf-8")
        body = source[source.index("def start("):source.index("def stop(")]
        assert body.index("workers.host().start()") < body.index("start_routines()")

    def test_what_ran_overnight_reaches_the_prompt(self):
        source = (SRC / "agent.py").read_text(encoding="utf-8")
        assert "routines.pending_block()" in source
        assert "routines.routines_block()" in source

    def test_the_prompt_blocks_are_inside_the_instructions_builder(self):
        """Not at import time - they have to be re-read on every call."""
        source = (SRC / "agent.py").read_text(encoding="utf-8")
        builder = source[source.index("def _instructions"):]
        assert "routines.pending_block()" in builder[:1200]


class TestItCostsTheConversationNothing:

    def test_no_new_voice_tools_were_added(self):
        """The tool surface is capped, and a routines tool would have cost a
        slot on every turn for a question asked twice a year. The standing
        list goes in the prompt as three lines of text instead."""
        import permissions

        governed = permissions.GOVERNED
        assert not [name for name in governed if "routine" in name]

    def test_the_prompt_block_is_small(self, tmp_path):
        from routines.engine import RoutineEngine
        from routines.store import RoutineStore

        engine = RoutineEngine(store=RoutineStore(tmp_path / "jarvis.db"))
        engine.seed_defaults()
        assert len(engine.routines_block()) < 600

    def test_the_ticker_is_spawned_not_queued(self):
        """At the shipped concurrency of one, a ticker in the job queue would
        be the entire worker pool and no content job would ever run."""
        from routines.engine import RoutineEngine

        source = inspect.getsource(RoutineEngine.start)
        assert "spawn" in source
        assert "submit" not in source


class TestTheWorkerHostCanHoldADaemon:

    def test_spawn_returns_a_task(self):
        import asyncio

        import workers

        host = workers.WorkerHost(name="test-spawn")
        try:
            async def forever():
                await asyncio.sleep(3600)

            task = host.spawn(forever, name="test")
            assert task is not None
            assert host.cancel_spawned(task) is True
        finally:
            host.stop(timeout=3.0)

    def test_spawn_on_a_host_that_will_not_start(self, monkeypatch):
        import workers

        host = workers.WorkerHost(name="test-dead")
        monkeypatch.setattr(host, "start", lambda: False)
        assert host.spawn(lambda: None, name="test") is None

    def test_a_daemon_that_dies_says_so_rather_than_vanishing(self, capsys):
        import workers

        host = workers.WorkerHost(name="test-noisy")
        try:
            async def explode():
                raise RuntimeError("the ticker fell over")

            host.spawn(explode, name="ticker")
            import time

            deadline = time.time() + 5
            while time.time() < deadline:
                if "the ticker fell over" in capsys.readouterr().out:
                    return
                time.sleep(0.05)
            pytest.fail("a dying daemon printed nothing")
        finally:
            host.stop(timeout=3.0)

    def test_stopping_the_host_cancels_its_daemons(self):
        import asyncio

        import workers

        host = workers.WorkerHost(name="test-shutdown")
        stopped = []

        async def forever():
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                stopped.append(True)
                raise

        host.spawn(forever, name="test")
        host.stop(timeout=5.0)
        assert stopped == [True]

    def test_cancelling_nothing(self):
        import workers

        host = workers.WorkerHost(name="test-nothing")
        assert host.cancel_spawned(None) is False


class TestTheSettingsPanelCanReachThem:

    def test_the_api_has_a_route_for_every_thing_the_panel_does(self):
        source = (SRC / "control_api.py").read_text(encoding="utf-8")
        for needed in ("def routines", "def save_routine", "def delete_routine",
                       "def set_routine_enabled", "def run_routine",
                       "def routine_runs"):
            assert needed in source

    def test_the_routes_are_dispatched(self):
        source = (SRC / "control_api.py").read_text(encoding="utf-8")
        dispatch = source[source.index("def _dispatch"):]
        assert 'head == "routines"' in dispatch
        assert '"run"' in dispatch
        assert '"enabled"' in dispatch

    def test_the_whole_page_load_includes_them(self):
        source = (SRC / "control_api.py").read_text(encoding="utf-8")
        state = source[source.index("def state(self)"):]
        assert '"routines"' in state[:800]

    def test_a_broken_routines_table_does_not_break_the_settings_page(
            self, monkeypatch):
        from control_api import Control

        control = Control.__new__(Control)
        monkeypatch.setattr(
            Control, "routines",
            lambda self: (_ for _ in ()).throw(RuntimeError("no database")))
        payload = control._routines_for_state()
        assert payload["routines"] == []
        assert "no database" in payload["error"]

    def test_saving_through_the_api_arms_it(self, tmp_path, monkeypatch):
        from control_api import Control
        from routines import engine as engine_module
        from routines.engine import RoutineEngine
        from routines.store import RoutineStore

        mine = RoutineEngine(store=RoutineStore(tmp_path / "jarvis.db"))
        monkeypatch.setattr(engine_module, "get_engine", lambda: mine)

        control = Control.__new__(Control)
        saved = control.save_routine({"name": "From the panel",
                                      "action": "note", "schedule": "08:00",
                                      "instruction": "hello"})
        assert saved["next_run_at"]
        assert control.routines()["summary"]["enabled"] == 1
        assert control.set_routine_enabled(saved["id"], False)["changed"] is True
        assert control.delete_routine(saved["id"])["deleted"] is True


class TestTheDoctorLooksAtThem:

    def test_the_doctor_has_a_routines_check(self):
        import doctor

        assert hasattr(doctor, "check_routines")

    def test_it_runs_as_part_of_the_full_check(self):
        source = (SRC / "doctor.py").read_text(encoding="utf-8")
        main = source[source.index("def main()"):]
        assert "check_routines" in main

    def test_it_reports_rather_than_crashing_on_an_empty_database(
            self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
        from jarvis import paths

        paths.refresh()
        import doctor
        from routines import engine as engine_module

        engine_module.reset_engine()
        doctor.check_routines()
        assert "Standing routines" in capsys.readouterr().out


class TestTheSettingsFileKnowsAboutThem:

    def test_the_permissions_matrix_is_unchanged_by_this_work(self):
        """Routines are not a capability: they are not something the model
        can invoke, so there is nothing to take away from it."""
        import permissions

        keys = {capability.key for capability in permissions.CAPABILITIES}
        assert "routines" not in keys
        assert "content" in keys


class TestThePackageDoesNotShadowItself:
    """A name exported from a package can hide the module it came from.

    `from .engine import engine` in `__init__.py` binds `routines.engine` to a
    function, so `import routines.engine` hands back the function rather than
    the module and every attribute on it fails. It was doing exactly that
    until a test tried to import it that way.
    """

    def test_every_submodule_is_still_importable_as_a_module(self):
        import importlib
        import types

        for name in ("routines.cron", "routines.store", "routines.actions",
                     "routines.engine"):
            module = importlib.import_module(name)
            assert isinstance(module, types.ModuleType), name

    def test_the_submodules_are_reachable_as_attributes_too(self):
        import routines

        assert routines.engine.RoutineEngine is not None
        assert routines.store.RoutineStore is not None
        assert routines.cron.parse is not None

    def test_no_package_here_shadows_one_of_its_own_submodules(self):
        """The general form of the same bug, across both sub-packages."""
        import importlib
        import pkgutil
        import types

        for package_name in ("routines", "content"):
            package = importlib.import_module(package_name)
            for info in pkgutil.iter_modules(package.__path__):
                shadow = getattr(package, info.name, None)
                assert shadow is None or isinstance(shadow, types.ModuleType), (
                    f"{package_name}.{info.name} is shadowed by "
                    f"{type(shadow).__name__}")
