"""Standing routines: the things Jarvis does without being asked.

A routine is a name, a schedule, and one of a small set of actions. It lives
in SQLite beside everything else, it is edited from the settings panel, and it
runs on the background worker host - never on the voice loop.

    from routines import get_engine, start_routines

    start_routines()                      # arm and begin ticking
    get_engine().save({"name": "Good morning briefing",
                   "action": "briefing", "schedule": "07:30"})

The pieces, if you need them directly:

* `cron` - the schedule parser. Five-field cron, plus "07:30" and "@daily".
* `store` - the two tables and the claim that stops a double fire.
* `actions` - what a routine can actually do.
* `engine` - the ticker, and everything the settings panel calls.
"""

from .actions import ACTIONS, catalogue
from .cron import Schedule, ScheduleError, parse, valid
from .engine import (
    DEFAULTS,
    RoutineEngine,
    get_engine,
    overdue,
    pending_block,
    reset_engine,
    routines_block,
    start_routines,
    stop_routines,
)
from .store import RoutineStore

__all__ = [
    "ACTIONS", "DEFAULTS", "RoutineEngine", "RoutineStore", "Schedule",
    "ScheduleError", "catalogue", "get_engine", "overdue", "parse",
    "pending_block", "reset_engine", "routines_block", "start_routines",
    "stop_routines",
    "valid",
]
