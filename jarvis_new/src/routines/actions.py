"""What a routine actually does when its time comes.

Each action is an ordinary function that takes the routine row and returns the
text it produced. That text is what gets stored, what the panel shows, and
what Jarvis reads out the next time you speak to him - so it is written to be
said aloud, not to be looked at.

The rules every action here follows:

* **It runs on a background worker**, never on the voice loop, so it may take
  as long as it takes.
* **It returns a sentence rather than raising**, wherever a failure is a fact
  about the world rather than a bug. "The machine has four gigabytes left" and
  "nothing to report" are both results. A missing API key is a result too - a
  briefing that says the thinking brain is not configured is more use at seven
  in the morning than a stack trace in a log file.
* **It costs nothing by default.** The three that ship enabled use only the
  local machine and the free brain. Nothing here spends money, and nothing
  here posts anything anywhere.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

REPO = Path(__file__).resolve().parents[3]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

#: Disk below this is worth mentioning unprompted in a morning briefing.
LOW_DISK_GB = 20.0
#: And a battery below this, if there is one.
LOW_BATTERY = 25


def _ordinal(day: int) -> str:
    """1st, 2nd, 3rd, 21st. This is read aloud, so "the 18 of September" -
    which is what a bare %d gives - sounds like a machine reading a form."""
    if 11 <= day % 100 <= 13:
        return f"{day}th"
    return f"{day}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(day % 10, 'th') }"


def _greeting(when: datetime | None = None) -> str:
    moment = when or datetime.now()
    hour = moment.hour
    if hour < 12:
        part = "Good morning"
    elif hour < 18:
        part = "Good afternoon"
    else:
        part = "Good evening"
    return (f"{part}, sir. It is {moment.strftime('%A')} the "
            f"{_ordinal(moment.day)} of {moment.strftime('%B')}.")


def machine_lines() -> list[str]:
    """How the computer is, in sentences, with the worrying parts first."""
    try:
        import psutil
    except ImportError:
        return ["I can't read the machine's vitals; psutil isn't installed."]

    lines: list[str] = []
    try:
        disk = psutil.disk_usage(str(Path.home().anchor or "/"))
        free_gb = disk.free / 1e9
        if free_gb < LOW_DISK_GB:
            lines.append(f"Disk is getting tight: {free_gb:.0f} gigabytes left.")
        memory = psutil.virtual_memory()
        lines.append(
            f"The machine is at {psutil.cpu_percent(interval=0.3):.0f} percent "
            f"processor and {memory.percent:.0f} percent memory, with "
            f"{free_gb:.0f} gigabytes of disk free.")
    except Exception as exc:
        lines.append(f"I couldn't read the machine's vitals: {exc}.")

    try:
        battery = psutil.sensors_battery()
        if battery is not None and not battery.power_plugged \
                and battery.percent < LOW_BATTERY:
            lines.append(f"Battery is down to {battery.percent:.0f} percent "
                         "and not charging.")
    except Exception:
        pass
    return lines


def content_lines() -> list[str]:
    """What the business side did while nobody was watching."""
    try:
        from content.store import ContentStore

        jobs = ContentStore().recent_jobs(limit=25)
    except Exception:
        return []

    if not jobs:
        return []
    done = [job for job in jobs if job.get("status") == "done"]
    failed = [job for job in jobs if job.get("status") == "failed"]
    busy = [job for job in jobs if job.get("status") in ("queued", "running")]

    lines: list[str] = []
    if done:
        lines.append(f"The content engine finished {len(done)} job"
                     f"{'' if len(done) == 1 else 's'}.")
    if busy:
        lines.append(f"{len(busy)} still in progress.")
    if failed:
        lines.append(f"{len(failed)} failed - worth a look.")
    return lines


def routine_lines(store: Any) -> list[str]:
    """Anything that has started going wrong on its own schedule."""
    try:
        rows = store.all()
    except Exception:
        return []
    broken = [row for row in rows
              if row.get("enabled") and row.get("last_status") == "failed"]
    if not broken:
        return []
    names = ", ".join(str(row.get("name")) for row in broken[:3])
    return [f"One of your routines is failing: {names}."]


# --------------------------------------------------------------------------- #
# The actions
# --------------------------------------------------------------------------- #

def action_note(routine: dict[str, Any], store: Any = None) -> str:
    """Say a fixed thing. No model, no network, no key - it always works."""
    text = str(routine.get("instruction") or "").strip()
    return text or f"{routine.get('name', 'This routine')} had nothing to say."


def action_system_check(routine: dict[str, Any], store: Any = None) -> str:
    """How the computer is doing, and nothing else."""
    return " ".join(machine_lines())


def action_briefing(routine: dict[str, Any], store: Any = None) -> str:
    """The morning briefing: the day, the machine, the business, your notes.

    Composed locally first, then handed to the free brain only to be tidied
    into something worth hearing. If there is no brain configured - which on a
    fresh machine there is not - the locally composed version is what you get,
    and it is perfectly serviceable.
    """
    parts: list[str] = [_greeting()]
    parts.extend(machine_lines())
    parts.extend(content_lines())
    if store is not None:
        parts.extend(routine_lines(store))

    extra = str(routine.get("instruction") or "").strip()
    if extra:
        parts.append(extra)

    plain = " ".join(part for part in parts if part)

    try:
        from thinker import Thinker

        tidy = Thinker().ask(
            "Rewrite this morning briefing as three or four short spoken "
            "sentences for a butler to read aloud. Keep every fact and every "
            "number. Do not add anything that is not here. No lists, no "
            "markdown, no greeting other than the one already present.",
            mode="general", context=plain)
    except Exception:
        return plain
    return (tidy or "").strip() or plain


def action_instruct(routine: dict[str, Any], store: Any = None) -> str:
    """Put the routine's own instruction to the thinking brain."""
    task = str(routine.get("instruction") or "").strip()
    if not task:
        return "This routine has no instruction to carry out."
    try:
        from thinker import Thinker

        return Thinker().ask(task, mode="general").strip()
    except Exception as exc:
        return (f"I couldn't carry out {routine.get('name', 'that routine')}: "
                f"{exc}")


def action_content_scripts(routine: dict[str, Any], store: Any = None) -> str:
    """Queue Reel scripts on a topic, on the same background host.

    The routine does not wait for them. It hands the work to the content
    engine's own queue and says so; the scripts land in the database in their
    own time, and the status tool reports them like any other job.
    """
    topic = str(routine.get("instruction") or "").strip()
    if not topic:
        return "This routine has no topic to write about."
    try:
        from content.pipeline import queue_scripts

        ref = queue_scripts(topic=topic, count=3)
    except Exception as exc:
        return f"I couldn't start the script writing: {exc}"
    return (f"I've started three Reel scripts on {topic}. "
            f"The job reference is {ref}.")


#: Every action, with what the settings panel needs to draw the dropdown.
ACTIONS: dict[str, dict[str, Any]] = {
    "briefing": {
        "run": action_briefing,
        "label": "Daily briefing",
        "detail": ("The date, how the machine is, what the content engine did "
                   "overnight, and anything of yours in the instruction box."),
        "needs": "Nothing. Better with a thinking brain configured.",
    },
    "system_check": {
        "run": action_system_check,
        "label": "Check the machine",
        "detail": "Processor, memory, disk and battery, with warnings first.",
        "needs": "Nothing.",
    },
    "note": {
        "run": action_note,
        "label": "Remind me of something",
        "detail": "Say exactly what is in the instruction box, at that time.",
        "needs": "Nothing.",
    },
    "instruct": {
        "run": action_instruct,
        "label": "Carry out an instruction",
        "detail": ("Put the instruction to the thinking brain and keep the "
                   "answer for you."),
        "needs": "A thinking brain: Ollama locally, or a Google key.",
    },
    "content_scripts": {
        "run": action_content_scripts,
        "label": "Write Reel scripts",
        "detail": "Queue three Reel scripts on the topic in the instruction box.",
        "needs": "The content engine, and a thinking brain to write with.",
    },
}


def get(name: str) -> Callable[..., str] | None:
    entry = ACTIONS.get((name or "").strip())
    return entry["run"] if entry else None


def catalogue() -> list[dict[str, Any]]:
    """The actions, for the dropdown in the settings panel."""
    return [{"key": key, "label": entry["label"], "detail": entry["detail"],
             "needs": entry["needs"]}
            for key, entry in ACTIONS.items()]


def run(routine: dict[str, Any], store: Any = None) -> str:
    """Run one routine's action. Raises only if the action itself does."""
    handler = get(str(routine.get("action") or ""))
    if handler is None:
        raise ValueError(
            f"{routine.get('action')!r} isn't an action this build knows about.")
    return handler(routine, store)
