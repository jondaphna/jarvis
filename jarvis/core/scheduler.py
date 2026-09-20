"""The empire engine: loads missions and runs them on schedule, unattended.

Missions come from two folders - the examples shipped with JARVIS, and the
user's own. The user's copy wins on an id clash, so editing an example never
means fighting the packaged version.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from . import events
from .events import bus, log
from .mission import Mission, MissionError, MissionRunner, RunResult, validate
from .. import paths


class MissionScheduler:
    """Owns the mission catalogue and the cron jobs that fire them."""

    def __init__(self, app) -> None:
        self.app = app
        self.runner = MissionRunner(app)
        self.missions: dict[str, Mission] = {}
        self.load_errors: dict[str, str] = {}
        self._scheduler: Any = None
        self._running: dict[str, asyncio.Task] = {}

    # ------------------------------------------------------------------ #
    # Catalogue
    # ------------------------------------------------------------------ #

    def mission_dirs(self) -> list[Path]:
        dirs = []
        if paths.BUILTIN_MISSION_DIR.is_dir():
            dirs.append(paths.BUILTIN_MISSION_DIR)
        dirs.append(paths.MISSION_DIR)
        return dirs

    def load_all(self) -> dict[str, Mission]:
        """Read every mission file. A bad file is reported, never fatal."""
        self.missions.clear()
        self.load_errors.clear()

        for directory in self.mission_dirs():
            if not directory.is_dir():
                continue
            for path in sorted(directory.glob("*.json")):
                if path.stem.upper().startswith("TEMPLATE"):
                    continue
                try:
                    mission = Mission.load(path)
                except MissionError as exc:
                    self.load_errors[path.name] = str(exc)
                    log.warning("mission %s skipped: %s", path.name, exc)
                    continue
                self.missions[mission.id] = mission

        log.info("loaded %d mission(s)", len(self.missions))
        return self.missions

    def get(self, mission_id: str) -> Mission | None:
        return self.missions.get(mission_id)

    def list(self) -> list[Mission]:
        return sorted(self.missions.values(), key=lambda m: m.name.lower())

    def check(self, mission: Mission) -> list[str]:
        """Validate against the actions that actually exist right now."""
        known: set[str] = set(self.app.plugins.plugins) | set(self.app.tools.names())
        return validate(mission, known)

    def save(self, mission: Mission) -> Path:
        path = mission.save(paths.MISSION_DIR)
        self.missions[mission.id] = mission
        if self._scheduler is not None:
            self._schedule(mission)
        return path

    def delete(self, mission_id: str) -> bool:
        mission = self.missions.pop(mission_id, None)
        if mission is None:
            return False
        if self._scheduler is not None:
            try:
                self._scheduler.remove_job(mission_id)
            except Exception:
                pass
        if mission.path and mission.path.is_relative_to(paths.MISSION_DIR):
            mission.path.unlink(missing_ok=True)
        return True

    # ------------------------------------------------------------------ #
    # Scheduling
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        """Begin firing scheduled missions. Needs a running event loop."""
        if not self.app.settings.get("scheduler.enabled", True):
            log.info("scheduler disabled in settings")
            return
        try:
            from apscheduler.schedulers.asyncio import AsyncIOScheduler
        except ImportError:
            log.warning("APScheduler isn't installed - scheduled missions are off. "
                        "Run: pip install apscheduler")
            return

        timezone = self.app.settings.get("timezone") or None
        self._scheduler = AsyncIOScheduler(
            timezone=timezone,
            job_defaults={
                "coalesce": True,          # one catch-up run, not twelve
                "max_instances": 1,        # never overlap a mission with itself
                "misfire_grace_time": int(
                    self.app.settings.get("scheduler.misfire_grace_seconds", 3600)),
            },
        )

        scheduled = 0
        for mission in self.missions.values():
            if self._schedule(mission):
                scheduled += 1

        self._scheduler.start()
        log.info("scheduler running with %d scheduled mission(s)", scheduled)
        bus.publish(events.INFO, f"Scheduler running - {scheduled} mission(s) armed")

    def _unschedule(self, mission_id: str) -> None:
        """Drop any trigger this mission already has. Quiet if it has none."""
        if self._scheduler is None:
            return
        try:
            self._scheduler.remove_job(mission_id)
        except Exception:
            pass

    def _schedule(self, mission: Mission) -> bool:
        if self._scheduler is None:
            return False
        if not mission.schedule or not mission.enabled:
            # Saving a mission re-schedules it, and this is the path an edit
            # to "disabled" or "manual" takes. Returning without removing the
            # old trigger left it armed: the mission still fired on the old
            # schedule until the next restart, which is the opposite of what
            # the edit said.
            self._unschedule(mission.id)
            return False
        try:
            from apscheduler.triggers.cron import CronTrigger
            self._scheduler.add_job(
                self._fire,
                trigger=CronTrigger.from_crontab(mission.schedule),
                args=[mission.id],
                id=mission.id,
                name=mission.name,
                replace_existing=True,
            )
            return True
        except Exception as exc:
            self.load_errors[mission.id] = f"bad schedule: {exc}"
            log.warning("couldn't schedule %s: %s", mission.id, exc)
            return False

    def shutdown(self) -> None:
        if self._scheduler is not None:
            try:
                self._scheduler.shutdown(wait=False)
            except Exception:
                pass
            self._scheduler = None
        for task in list(self._running.values()):
            task.cancel()

    # ------------------------------------------------------------------ #
    # Running
    # ------------------------------------------------------------------ #

    async def _fire(self, mission_id: str) -> None:
        mission = self.missions.get(mission_id)
        if mission is None:
            log.warning("scheduled mission %s no longer exists", mission_id)
            self._unschedule(mission_id)
            return
        if not mission.enabled:
            # Checked again here, not only when the trigger was added. A
            # trigger can outlive the state it was created under, and a
            # disabled mission running unattended is the one outcome the
            # switch exists to prevent.
            log.info("scheduled mission %s is disabled; not running", mission_id)
            self._unschedule(mission_id)
            return
        await self.run(mission, trigger="schedule")

    async def run(self, mission: Mission | str, trigger: str = "manual",
                  unattended: bool = True) -> RunResult:
        if isinstance(mission, str):
            found = self.missions.get(mission)
            if found is None:
                raise MissionError(f"There's no mission called {mission!r}.")
            mission = found

        if mission.id in self._running and not self._running[mission.id].done():
            raise MissionError(f"{mission.name} is already running.")

        task = asyncio.create_task(
            self.runner.run(mission, trigger=trigger, unattended=unattended))
        self._running[mission.id] = task
        try:
            return await task
        finally:
            self._running.pop(mission.id, None)

    def running(self) -> list[str]:
        return [key for key, task in self._running.items() if not task.done()]

    def cancel(self, mission_id: str) -> bool:
        task = self._running.get(mission_id)
        if task is None or task.done():
            return False
        task.cancel()
        return True

    # ------------------------------------------------------------------ #
    # Status
    # ------------------------------------------------------------------ #

    def next_runs(self) -> list[dict[str, Any]]:
        """What's armed and when it fires next - drives the dashboard."""
        rows: list[dict[str, Any]] = []
        jobs = {}
        if self._scheduler is not None:
            jobs = {job.id: job for job in self._scheduler.get_jobs()}

        for mission in self.list():
            job = jobs.get(mission.id)
            next_run = getattr(job, "next_run_time", None) if job else None
            history = self.app.memory.runs(mission.id, limit=1)
            rows.append({
                "id": mission.id,
                "name": mission.name,
                "schedule": mission.schedule or "manual",
                "enabled": mission.enabled,
                "steps": len(mission.steps),
                "running": mission.id in self.running(),
                "next_run": next_run.isoformat(timespec="minutes") if next_run else None,
                "last_run": history[0]["started_at"] if history else None,
                "last_status": history[0]["status"] if history else None,
                "authorizations": mission.authorizations,
            })
        return rows

    def set_enabled(self, mission_id: str, enabled: bool) -> bool:
        mission = self.missions.get(mission_id)
        if mission is None:
            return False
        mission.enabled = enabled
        if mission.path and mission.path.is_relative_to(paths.MISSION_DIR):
            mission.save()
        if self._scheduler is not None:
            if enabled:
                self._schedule(mission)
            else:
                self._unschedule(mission_id)
        return True
