"""Routines and their run history, in the same SQLite file as everything else.

Two tables, both additive: `routines` is the definition the settings panel
edits, `routine_runs` is what happened each time one fired. Nothing here
touches a table the older generation owns.

The reason this is a database rather than the JSON file the old missions use
is not tidiness. Two processes look at these rows - the voice agent, which
fires them, and the settings API, which edits them and can fire one on demand -
and a routine that runs twice because both of them thought it was due is worse
than one that does not run at all. `claim` is how that is prevented: the due
time a process saw is part of the UPDATE's WHERE clause, so exactly one
process can move it forward, and the loser gets False and does nothing.

Timestamps are stored as UTC ISO-8601 strings, so they sort lexicographically
and mean the same thing after the clocks change. What a person sees in the
panel is converted back to their own time on the way out.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[3]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

SCHEMA = """
CREATE TABLE IF NOT EXISTS routines (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    action        TEXT NOT NULL DEFAULT 'briefing',
    instruction   TEXT NOT NULL DEFAULT '',
    schedule      TEXT NOT NULL DEFAULT '',
    timezone      TEXT NOT NULL DEFAULT '',
    enabled       INTEGER NOT NULL DEFAULT 1,
    catch_up      INTEGER NOT NULL DEFAULT 1,
    grace_seconds INTEGER NOT NULL DEFAULT 3600,
    speak         INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    next_run_at   TEXT,
    last_run_at   TEXT,
    last_status   TEXT NOT NULL DEFAULT '',
    last_output   TEXT NOT NULL DEFAULT '',
    last_error    TEXT NOT NULL DEFAULT '',
    runs          INTEGER NOT NULL DEFAULT 0,
    failures      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_routines_due
    ON routines(enabled, next_run_at);

CREATE TABLE IF NOT EXISTS routine_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    routine_id  TEXT NOT NULL,
    job_ref     TEXT NOT NULL DEFAULT '',
    trigger     TEXT NOT NULL DEFAULT 'schedule',
    due_at      TEXT NOT NULL DEFAULT '',
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL DEFAULT 'running',
    output      TEXT NOT NULL DEFAULT '',
    error       TEXT NOT NULL DEFAULT '',
    delivered   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_routine_runs_routine
    ON routine_runs(routine_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_routine_runs_undelivered
    ON routine_runs(delivered, id DESC);
"""

STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"

#: Columns a caller may set through `save`. Anything else in the payload is
#: ignored rather than rejected, so a panel that sends back the row it was
#: given - next_run_at, runs and all - does not get an error for its trouble.
WRITABLE = ("name", "action", "instruction", "schedule", "timezone",
            "enabled", "catch_up", "grace_seconds", "speak")

#: How many runs to keep per routine. A daily briefing writes 365 rows a year
#: and nobody reads the one from March, but a routine that has started failing
#: needs enough history to see when it started.
KEEP_RUNS = 60


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def to_dt(value: str | None) -> datetime | None:
    """Parse one of our own timestamps back. Never raises on rubbish."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def from_dt(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def slug(text: str) -> str:
    """A stable id from a name. Ids are how the panel addresses a routine.

    ASCII only, because the id is a path segment in the settings API - and a
    routine called "Ünïcode" whose id needs percent-encoding to delete is a
    routine that cannot be deleted from a page that forgot to encode it.
    """
    cleaned = "".join(
        c if (c.isascii() and c.isalnum()) else "-" for c in (text or "").lower())
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned.strip("-")[:48] or "routine"


class RoutineStore:
    """The routines table, one connection per thread."""

    def __init__(self, db_path: Path | str | None = None) -> None:
        self._explicit = Path(db_path) if db_path else None
        self._local = threading.local()

    # ------------------------------------------------------------------ #
    # Connection
    # ------------------------------------------------------------------ #

    def path(self) -> Path:
        if self._explicit is not None:
            return self._explicit
        from jarvis import paths

        paths.ensure_dirs()
        return paths.DB_FILE

    def connection(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            return conn
        target = self.path()
        target.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(target), timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=15000")
        conn.executescript(SCHEMA)
        conn.commit()
        self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            finally:
                self._local.conn = None

    # ------------------------------------------------------------------ #
    # Definitions
    # ------------------------------------------------------------------ #

    def all(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM routines"
        if enabled_only:
            sql += " WHERE enabled=1"
        sql += " ORDER BY name COLLATE NOCASE"
        return [dict(row) for row in self.connection().execute(sql)]

    def get(self, routine_id: str) -> dict[str, Any] | None:
        row = self.connection().execute(
            "SELECT * FROM routines WHERE id=?", (routine_id,)).fetchone()
        return dict(row) if row else None

    def save(self, routine: dict[str, Any]) -> dict[str, Any]:
        """Create or update one routine. The schedule is validated here.

        Validation lives at the door rather than at fire time on purpose: a
        routine with a schedule nobody can parse should fail while the person
        who typed it is still looking at the screen, not silently at 4am.
        """
        from .cron import parse

        # The name is checked before it is slugged. `slug` has a fallback so
        # that a name of nothing but punctuation still yields a usable id, and
        # without this check that fallback would quietly accept a routine with
        # no name at all and call it "routine".
        name = str(routine.get("name") or routine.get("id") or "").strip()
        if not name:
            raise ValueError("A routine needs a name.")
        routine_id = slug(str(routine.get("id") or name))

        schedule = parse(str(routine.get("schedule") or "")).spec
        existing = self.get(routine_id)

        values: dict[str, Any] = {
            "name": name,
            "action": str(routine.get("action") or "briefing").strip(),
            "instruction": str(routine.get("instruction") or "").strip(),
            "schedule": schedule,
            "timezone": str(routine.get("timezone") or "").strip(),
            "enabled": 1 if routine.get("enabled", True) else 0,
            "catch_up": 1 if routine.get("catch_up", True) else 0,
            "grace_seconds": max(0, int(routine.get("grace_seconds", 3600) or 0)),
            "speak": 1 if routine.get("speak", True) else 0,
        }

        conn = self.connection()
        stamp = now_iso()
        if existing is None:
            columns = ["id", *WRITABLE, "created_at", "updated_at"]
            conn.execute(
                f"INSERT INTO routines({','.join(columns)}) "
                f"VALUES({','.join('?' * len(columns))})",
                [routine_id, *(values[key] for key in WRITABLE), stamp, stamp])
        else:
            assignments = ", ".join(f"{key}=?" for key in WRITABLE)
            conn.execute(
                f"UPDATE routines SET {assignments}, updated_at=? WHERE id=?",
                [*(values[key] for key in WRITABLE), stamp, routine_id])
        conn.commit()
        saved = self.get(routine_id)
        assert saved is not None
        return saved

    def delete(self, routine_id: str) -> bool:
        conn = self.connection()
        cursor = conn.execute("DELETE FROM routines WHERE id=?", (routine_id,))
        conn.execute("DELETE FROM routine_runs WHERE routine_id=?", (routine_id,))
        conn.commit()
        return cursor.rowcount > 0

    def set_enabled(self, routine_id: str, enabled: bool) -> bool:
        conn = self.connection()
        cursor = conn.execute(
            "UPDATE routines SET enabled=?, updated_at=? WHERE id=?",
            (1 if enabled else 0, now_iso(), routine_id))
        conn.commit()
        return cursor.rowcount > 0

    # ------------------------------------------------------------------ #
    # Due times
    # ------------------------------------------------------------------ #

    def set_next_run(self, routine_id: str, when: datetime | None) -> None:
        conn = self.connection()
        conn.execute("UPDATE routines SET next_run_at=? WHERE id=?",
                     (from_dt(when), routine_id))
        conn.commit()

    def claim(self, routine_id: str, due_at: str,
              next_at: datetime | None) -> bool:
        """Take ownership of one firing. True for the process that won it.

        The due time we read is in the WHERE clause, so if another process
        moved it on between our read and our write, nothing is updated and we
        know not to run the job. This is the whole of the double-fire
        protection, and it works across processes because SQLite's write lock
        does.
        """
        conn = self.connection()
        cursor = conn.execute(
            "UPDATE routines SET next_run_at=? WHERE id=? AND next_run_at=? "
            "AND enabled=1",
            (from_dt(next_at), routine_id, due_at))
        conn.commit()
        return cursor.rowcount > 0

    def due(self, moment: datetime | None = None) -> list[dict[str, Any]]:
        """Enabled routines whose next run is in the past."""
        cutoff = from_dt(moment or datetime.now(timezone.utc))
        rows = self.connection().execute(
            "SELECT * FROM routines WHERE enabled=1 AND next_run_at IS NOT NULL "
            "AND next_run_at <= ? ORDER BY next_run_at", (cutoff,))
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------ #
    # Runs
    # ------------------------------------------------------------------ #

    def start_run(self, routine_id: str, trigger: str = "schedule",
                  due_at: str = "", job_ref: str = "") -> int:
        conn = self.connection()
        cursor = conn.execute(
            "INSERT INTO routine_runs(routine_id, job_ref, trigger, due_at, "
            "started_at, status) VALUES(?,?,?,?,?,?)",
            (routine_id, job_ref, trigger, due_at, now_iso(), STATUS_RUNNING))
        conn.commit()
        return int(cursor.lastrowid or 0)

    def finish_run(self, run_id: int, routine_id: str, status: str,
                   output: str = "", error: str = "") -> None:
        """Close a run and roll the summary onto the routine in one go.

        Both writes happen in one transaction because the panel reads the
        routine row and the run row together, and a half-applied pair shows a
        routine that succeeded above a run that is still going.
        """
        conn = self.connection()
        stamp = now_iso()
        failed = 1 if status == STATUS_FAILED else 0
        with conn:
            conn.execute(
                "UPDATE routine_runs SET status=?, finished_at=?, output=?, "
                "error=? WHERE id=?",
                (status, stamp, output, error, run_id))
            conn.execute(
                "UPDATE routines SET last_run_at=?, last_status=?, last_output=?, "
                "last_error=?, runs=runs+1, failures=failures+? WHERE id=?",
                (stamp, status, output, error, failed, routine_id))
        self.trim_runs(routine_id)

    def runs(self, routine_id: str = "", limit: int = 20) -> list[dict[str, Any]]:
        if routine_id:
            rows = self.connection().execute(
                "SELECT * FROM routine_runs WHERE routine_id=? "
                "ORDER BY id DESC LIMIT ?", (routine_id, int(limit)))
        else:
            rows = self.connection().execute(
                "SELECT * FROM routine_runs ORDER BY id DESC LIMIT ?",
                (int(limit),))
        return [dict(row) for row in rows]

    def trim_runs(self, routine_id: str) -> int:
        conn = self.connection()
        cursor = conn.execute(
            "DELETE FROM routine_runs WHERE routine_id=? AND id NOT IN ("
            "SELECT id FROM routine_runs WHERE routine_id=? "
            "ORDER BY id DESC LIMIT ?)",
            (routine_id, routine_id, KEEP_RUNS))
        conn.commit()
        return cursor.rowcount

    # ------------------------------------------------------------------ #
    # Delivery
    # ------------------------------------------------------------------ #

    def undelivered(self, limit: int = 5) -> list[dict[str, Any]]:
        """Finished runs with something to say that nobody has heard yet.

        A briefing written at half past seven has no one to say it to: there
        is no call in progress. So it waits here, and the next conversation
        starts knowing it exists.
        """
        rows = self.connection().execute(
            "SELECT * FROM routine_runs WHERE delivered=0 AND status=? "
            "AND output<>'' ORDER BY id DESC LIMIT ?", (STATUS_DONE, int(limit)))
        return [dict(row) for row in rows]

    def mark_delivered(self, run_ids: list[int]) -> int:
        if not run_ids:
            return 0
        conn = self.connection()
        marks = ",".join("?" * len(run_ids))
        cursor = conn.execute(
            f"UPDATE routine_runs SET delivered=1 WHERE id IN ({marks})",
            [int(value) for value in run_ids])
        conn.commit()
        return cursor.rowcount


def dumps(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)
