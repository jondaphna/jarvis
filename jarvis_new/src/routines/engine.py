"""The loop that fires routines, living on the background worker host.

This is the piece that was missing. The older generation has a perfectly good
mission scheduler, but it hangs off `jarvis.core.assistant.Assistant`, which
the voice agent never builds - so a routine saved in the settings panel sat on
disk being correct and never ran. Everything here runs inside the same daemon
thread the content engine uses, which means a routine firing at seven in the
morning costs the conversation nothing, and a routine that throws cannot end a
call.

How a firing works, end to end:

1. The ticker wakes every few seconds on the worker loop and hands the actual
   database work to a thread, so even the check is off the loop.
2. Any enabled routine whose `next_run_at` is in the past is a candidate.
3. Its next time is computed and written with the old one in the WHERE clause.
   Exactly one process can win that write, so the voice agent and the settings
   API cannot both fire the same routine.
4. The winner submits the work to the worker queue, where it serialises behind
   whatever else is running.
5. The result is written to `routine_runs`, and waits there until the next
   conversation, when the agent's prompt picks it up and Jarvis says it.

Step 5 is the honest answer to "what does a briefing do at half past seven
when nobody is talking to it". It does not speak to an empty room. It writes
what it would have said, and says it when you next say hello.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import actions
from .cron import ScheduleError, parse
from .store import (
    SEEDED_KEY,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_SKIPPED,
    RoutineStore,
    from_dt,
    to_dt,
)

#: The repo root, so the flat modules in `src/` and the `jarvis` package are
#: both importable from inside this sub-package however the agent was started.
REPO = Path(__file__).resolve().parents[3]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

#: How often to look for something due. Cron's resolution is a minute, so this
#: only has to be comfortably under that; it costs one indexed SELECT.
TICK_SECONDS = 20.0

#: How late a firing may be and still run, when the routine does not say.
#: An hour covers a laptop that was shut when the briefing was due.
DEFAULT_GRACE = 3600

#: How much of a routine's output to keep on the routine row for the panel.
OUTPUT_LIMIT = 4000

#: The routines a fresh install gets. They are what he asked for by name, they
#: cost nothing to run, and none of them touches the outside world: the first
#: two read the local machine, and the third only speaks if you put something
#: in its instruction box. Seeding happens once - deleting one keeps it
#: deleted, because seeding checks whether the table has ever had rows rather
#: than whether these particular ids are present.
DEFAULTS: tuple[dict[str, Any], ...] = (
    {
        "id": "good-morning",
        "name": "Good morning briefing",
        "action": "briefing",
        "schedule": "07:30",
        "instruction": "",
        "enabled": True,
        "catch_up": True,
        "grace_seconds": 6 * 3600,
        "speak": True,
    },
    {
        "id": "morning-system-check",
        "name": "Morning system check",
        "action": "system_check",
        "schedule": "07:25",
        "instruction": "",
        "enabled": True,
        "catch_up": True,
        "grace_seconds": 6 * 3600,
        "speak": True,
    },
    {
        "id": "evening-wrap-up",
        "name": "Evening wrap-up",
        "action": "note",
        "schedule": "21:00",
        "instruction": "",
        "enabled": False,
        "catch_up": False,
        "grace_seconds": 1800,
        "speak": True,
    },
)


def _zone(name: str = "") -> Any:
    """The timezone a routine's clock times are in.

    A person writing "07:30" means half past seven where they are, not UTC,
    and the machine already knows where it is. A named zone is only needed
    when the two differ - a laptop travelling, or a routine deliberately set
    to somebody else's morning.
    """
    if name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            pass
    return datetime.now().astimezone().tzinfo or timezone.utc


class RoutineEngine:
    """Owns the ticker, the claiming, and the firing."""

    def __init__(self, store: RoutineStore | None = None, host: Any = None,
                 tick_seconds: float = TICK_SECONDS) -> None:
        self.store = store or RoutineStore()
        self._host = host
        self.tick_seconds = max(1.0, float(tick_seconds))
        self._task: Any = None
        self._lock = threading.RLock()
        self._seed = True
        self.started = False

    # ------------------------------------------------------------------ #
    # Wiring
    # ------------------------------------------------------------------ #

    def host(self) -> Any:
        if self._host is not None:
            return self._host
        import workers

        return workers.host()

    # ------------------------------------------------------------------ #
    # Schedules
    # ------------------------------------------------------------------ #

    def next_time(self, routine: dict[str, Any],
                  after: datetime | None = None) -> datetime | None:
        """When this routine fires next, in UTC. None if it never does."""
        try:
            schedule = parse(str(routine.get("schedule") or ""))
        except ScheduleError:
            return None
        zone = _zone(str(routine.get("timezone") or ""))
        moment = (after or datetime.now(timezone.utc)).astimezone(zone)
        upcoming = schedule.next_after(moment)
        if upcoming is None:
            return None
        return upcoming.astimezone(timezone.utc)

    def arm(self) -> int:
        """Give every enabled routine a next run time, if it hasn't one.

        A time already on the row is left exactly as it is, including one in
        the past. That is what makes a missed briefing survive a restart: the
        row still says half past seven, the process comes up at eight, and the
        catch-up rule - not the arming - decides whether it still runs.
        """
        armed = 0
        for routine in self.store.all(enabled_only=True):
            if routine.get("next_run_at"):
                continue
            when = self.next_time(routine)
            if when is None:
                continue
            self.store.set_next_run(str(routine["id"]), when)
            armed += 1
        return armed

    def save(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Create or edit a routine and re-arm it against its new schedule.

        Editing goes through here rather than through the store directly
        because a changed schedule with the old due time still on the row is a
        routine that fires at the time you just changed away from - once,
        confusingly, and then correctly ever after.
        """
        saved = self.store.save(payload)
        when = self.next_time(saved) if saved.get("enabled") else None
        self.store.set_next_run(str(saved["id"]), when)
        refreshed = self.store.get(str(saved["id"]))
        return refreshed or saved

    def set_enabled(self, routine_id: str, enabled: bool) -> bool:
        """Turn one on or off. Turning it on arms it from now, not from then."""
        if not self.store.set_enabled(routine_id, enabled):
            return False
        routine = self.store.get(routine_id)
        if routine is None:
            return False
        self.store.set_next_run(
            routine_id, self.next_time(routine) if enabled else None)
        return True

    def seed_defaults(self, force: bool = False) -> int:
        """Put the starting routines in, once, on a fresh database.

        "Once" is recorded in the database rather than inferred from the table
        being empty. An empty table is not the same as a new one: somebody who
        deletes every routine means it, and the old check brought the defaults
        straight back on the next start.
        """
        if not force:
            try:
                if self.store.flag(SEEDED_KEY) or self.store.all():
                    # Marked here too, so an install that already has routines
                    # from before this flag existed does not seed on the day
                    # its last routine is deleted.
                    with contextlib.suppress(Exception):
                        self.store.set_flag(SEEDED_KEY)
                    return 0
            except Exception:
                return 0
        added = 0
        for payload in DEFAULTS:
            with contextlib.suppress(Exception):
                self.save(dict(payload))
                added += 1
        with contextlib.suppress(Exception):
            self.store.set_flag(SEEDED_KEY)
        return added

    # ------------------------------------------------------------------ #
    # Firing
    # ------------------------------------------------------------------ #

    def tick(self, moment: datetime | None = None) -> list[str]:
        """Fire everything that is due. Returns the job references started.

        Synchronous on purpose: it is called from a thread, and being an
        ordinary function is what lets a test call it directly and assert on
        what happened rather than racing a loop.
        """
        now = moment or datetime.now(timezone.utc)
        started: list[str] = []
        for routine in self.store.due(now):
            ref = self._fire(routine, now)
            if ref:
                started.append(ref)
        return started

    def _fire(self, routine: dict[str, Any], now: datetime) -> str:
        routine_id = str(routine.get("id") or "")
        due_at = str(routine.get("next_run_at") or "")
        upcoming = self.next_time(routine, now)

        # Whoever wins this write owns this firing. Everyone else moves on.
        if not self.store.claim(routine_id, due_at, upcoming):
            return ""

        due = to_dt(due_at)
        late = (now - due).total_seconds() if due else 0.0
        # `is None`, not truthiness: a grace of 0 means "if you missed it,
        # skip it", and `or DEFAULT_GRACE` turned that into an hour - the
        # opposite instruction.
        raw_grace = routine.get("grace_seconds")
        grace = DEFAULT_GRACE if raw_grace is None else max(0, int(raw_grace))
        if late > grace and not routine.get("catch_up"):
            run_id = self.store.start_run(routine_id, trigger="schedule",
                                          due_at=due_at)
            self.store.finish_run(
                run_id, routine_id, STATUS_SKIPPED,
                output="",
                error=(f"missed by {int(late // 60)} minutes, and this routine "
                       "is set not to catch up"))
            return ""

        return self.submit(routine, trigger="schedule", due_at=due_at)

    def submit(self, routine: dict[str, Any], trigger: str = "manual",
               due_at: str = "") -> str:
        """Hand one routine to the worker queue. Returns the job reference."""
        ref = self.host().submit(
            self._execute, dict(routine), trigger, due_at,
            name=f"routine: {routine.get('name', routine.get('id'))}")
        if ref is None:
            # The host would not start. Record the failure rather than losing
            # it: a routine that silently did not run is the exact complaint
            # this whole module exists to answer.
            run_id = self.store.start_run(str(routine.get("id") or ""),
                                          trigger=trigger, due_at=due_at)
            self.store.finish_run(run_id, str(routine.get("id") or ""),
                                  STATUS_FAILED, error="the background worker "
                                  "wouldn't start")
            return ""
        return ref

    def run_now(self, routine_id: str) -> str:
        """Fire one on demand, from the panel or a test. Never waits for it."""
        routine = self.store.get(routine_id)
        if routine is None:
            raise ValueError(f"There's no routine called {routine_id!r}.")
        return self.submit(routine, trigger="manual")

    def _execute(self, routine: dict[str, Any], trigger: str,
                 due_at: str) -> str:
        """Run one routine's action on the worker thread and record it."""
        routine_id = str(routine.get("id") or "")
        run_id = self.store.start_run(routine_id, trigger=trigger, due_at=due_at)
        try:
            output = (actions.run(routine, self.store) or "").strip()
        except Exception as exc:
            self.store.finish_run(run_id, routine_id, STATUS_FAILED,
                                  error=f"{type(exc).__name__}: {exc}")
            raise
        self.store.finish_run(run_id, routine_id, STATUS_DONE,
                              output=output[:OUTPUT_LIMIT])
        return output

    # ------------------------------------------------------------------ #
    # The ticker
    # ------------------------------------------------------------------ #

    def prepare(self) -> dict[str, Any]:
        """Everything that has to happen once, before the first tick.

        Deliberately not done in `start()`. `start()` is called from the voice
        thread as a call opens, and this recovers rows, seeds a fresh database
        and arms every routine - three lots of SQLite on the thread that has
        somebody talking to it. Out here it runs on the worker instead, and
        the call opens without waiting for any of it.
        """
        closed = self.store.recover_interrupted()
        seeded = self.seed_defaults() if self._seed else 0
        armed = self.arm()
        if closed:
            print(f"  Routines: closed {len(closed)} run(s) interrupted by a "
                  f"previous shutdown.")
        state = self.summary()
        following = state["next"]
        when = ""
        if following:
            moment = to_dt(str(following["at"]))
            if moment:
                when = (f", next is {following['name']} at "
                        f"{moment.astimezone().strftime('%H:%M')}")
        print(f"  Routines armed: {state['armed']} scheduled{when}.")
        return {"recovered": len(closed), "seeded": seeded, "armed": armed}

    async def _tick_forever(self) -> None:
        try:
            await asyncio.to_thread(self.prepare)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"  [routines] could not be armed: {type(exc).__name__}: {exc}")
        while True:
            await asyncio.sleep(self.tick_seconds)
            try:
                # Even the check goes to a thread. It is one indexed SELECT,
                # but it is a SELECT against a file another thread is writing,
                # and the worker loop has background jobs waiting on it.
                await asyncio.to_thread(self.tick)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # A ticker that dies is a scheduler that silently stops. It
                # says so and keeps going.
                print(f"  [routines] tick failed: {type(exc).__name__}: {exc}")

    def start(self, seed: bool = True) -> bool:
        """Start ticking. Safe to call twice.

        Returns as soon as the ticker is on the loop. Recovery, seeding and
        arming happen in `prepare()`, on the worker, because this is called
        while somebody is waiting for a call to open.
        """
        with self._lock:
            if self.started:
                return True
            self._seed = seed
            task = self.host().spawn(self._tick_forever, name="routine-ticker")
            if task is None:
                return False
            self._task = task
            self.started = True
            return True

    def stop(self) -> None:
        task, self._task = self._task, None
        self.started = False
        if task is not None:
            with contextlib.suppress(Exception):
                self.host().cancel_spawned(task)

    # ------------------------------------------------------------------ #
    # Reading it back
    # ------------------------------------------------------------------ #

    def listing(self) -> list[dict[str, Any]]:
        """Every routine with its schedule in words, for the panel."""
        rows = []
        for routine in self.store.all():
            entry = dict(routine)
            entry["enabled"] = bool(routine.get("enabled"))
            entry["catch_up"] = bool(routine.get("catch_up"))
            entry["speak"] = bool(routine.get("speak"))
            try:
                entry["schedule_in_words"] = parse(
                    str(routine.get("schedule") or "")).describe()
            except ScheduleError as exc:
                entry["schedule_in_words"] = f"broken schedule: {exc}"
            entry["action_label"] = (
                actions.ACTIONS.get(str(routine.get("action")), {})
                .get("label", routine.get("action")))
            rows.append(entry)
        return rows

    def summary(self) -> dict[str, Any]:
        """One glance: how many are armed, what is next, what is broken."""
        rows = self.store.all()
        enabled = [row for row in rows if row.get("enabled")]
        upcoming = sorted(
            (row for row in enabled if row.get("next_run_at")),
            key=lambda row: str(row["next_run_at"]))
        failing = [row["name"] for row in enabled
                   if row.get("last_status") == STATUS_FAILED]
        return {
            "total": len(rows),
            "enabled": len(enabled),
            "armed": len(upcoming),
            "next": ({"name": upcoming[0]["name"],
                      "at": upcoming[0]["next_run_at"]} if upcoming else None),
            "failing": failing,
            "ticking": self.started,
        }

    # ------------------------------------------------------------------ #
    # Delivery
    # ------------------------------------------------------------------ #

    def routines_block(self) -> str:
        """What is standing, for the prompt, so he can be asked about it.

        Three lines of text rather than a tool: "what have you got scheduled"
        is a question about a handful of rows that change once a month, and a
        tool for it would cost every turn of every conversation a little
        attention for the sake of a question asked twice a year.
        """
        try:
            rows = [row for row in self.store.all() if row.get("enabled")]
        except Exception:
            return ""
        if not rows:
            return ""
        lines = ["# Your standing routines",
                 "These run on their own, in the background, whether or not "
                 "you are in a call. If asked what is scheduled, this is the "
                 "list. They are changed in the settings panel, not by you."]
        for row in rows:
            try:
                when = parse(str(row.get("schedule") or "")).describe()
            except ScheduleError:
                when = str(row.get("schedule") or "")
            lines.append(f"- {row.get('name')}: {when}")
        return "\n".join(lines)

    def pending_block(self, mark: bool = True) -> str:
        """Anything a routine produced that you have not been told yet.

        Folded into the system prompt when a call starts, which is what turns
        "it ran at half past seven" into "he told me about it when I said good
        morning". Marking them delivered here rather than after he speaks is
        deliberate: the alternative is a briefing repeated in every call until
        it happens to be mentioned.
        """
        try:
            rows = [row for row in self.store.undelivered()
                    if str(row.get("output") or "").strip()]
        except Exception:
            return ""
        if not rows:
            return ""

        lines = ["# While you were away",
                 "These ran on their own schedule and you have not heard them "
                 "yet. Lead with them when the call opens, in your own words, "
                 "briefly. Do not read them out twice.",
                 ""]
        for row in reversed(rows):
            when = to_dt(str(row.get("finished_at") or row.get("started_at")))
            stamp = when.astimezone().strftime("%H:%M") if when else "earlier"
            lines.append(f"- ({stamp}) {str(row.get('output') or '').strip()}")
        if mark:
            with contextlib.suppress(Exception):
                self.store.mark_delivered([int(row["id"]) for row in rows])
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# The one the agent uses
# --------------------------------------------------------------------------- #

_ENGINE: RoutineEngine | None = None
_ENGINE_LOCK = threading.Lock()


def get_engine() -> RoutineEngine:
    """The process-wide routine engine, created on first use.

    Named `get_engine` rather than `engine` because this module is
    `routines.engine`, and a function of that name exported from the package
    shadows the module itself: `import routines.engine` would then hand back
    the function, and every attribute lookup on it would fail with something
    unhelpful about a function having no attributes.
    """
    global _ENGINE
    with _ENGINE_LOCK:
        if _ENGINE is None:
            _ENGINE = RoutineEngine()
        return _ENGINE


def reset_engine() -> None:
    """Drop the process-wide engine. For tests, which build their own."""
    global _ENGINE
    with _ENGINE_LOCK:
        if _ENGINE is not None:
            _ENGINE.stop()
        _ENGINE = None


def start_routines() -> bool:
    """Start the routine ticker. Called once when a call begins.

    Touches no database: what it costs the call is putting one task on the
    worker loop. Everything else - recovering interrupted runs, seeding a
    fresh install, arming the schedules - happens on the worker a moment
    later, and prints what it found.
    """
    try:
        started = get_engine().start()
    except Exception as exc:
        print(f"  Routines are not running: {exc}")
        return False
    if not started:
        print("  Routines could not start; voice is unaffected.")
    return started


async def stop_routines() -> None:
    """Shutdown callback shape: awaited by the agent when the call ends."""
    get_engine().stop()


def pending_block() -> str:
    """The prompt block of anything not yet said. Never raises."""
    try:
        return get_engine().pending_block()
    except Exception:
        return ""


def routines_block() -> str:
    """The prompt block listing what is standing. Never raises."""
    try:
        return get_engine().routines_block()
    except Exception:
        return ""


def overdue(moment: datetime | None = None,
            within: timedelta = timedelta(days=1)) -> list[str]:
    """Names of enabled routines whose time passed and which never ran.

    Used by the doctor, which is the one place where "it looks armed but
    nothing has happened for a day" needs saying out loud.
    """
    now = moment or datetime.now(timezone.utc)
    late: list[str] = []
    for routine in get_engine().store.all(enabled_only=True):
        due = to_dt(str(routine.get("next_run_at") or ""))
        if due is not None and now - due > within:
            late.append(str(routine.get("name")))
    return late


__all__ = ["RoutineEngine", "from_dt", "get_engine", "overdue",
           "pending_block",
           "reset_engine", "routines_block", "start_routines",
           "stop_routines", "to_dt"]
