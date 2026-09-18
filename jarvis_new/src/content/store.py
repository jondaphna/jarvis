"""Where content jobs and their output live.

The same SQLite file as everything else - `%APPDATA%\\JARVIS\\jarvis.db` on
Windows - so one backup covers the assistant and the business, and the panel
that reads missions can read content jobs without learning a second place to
look. The tables are new and additive: nothing here touches a table the older
generation owns, so an old build reading this database sees exactly what it
saw before.

Two rules shape the design.

**A job outlives the process that started it.** A voice call ends; a script
written during it does not stop existing. So the job row is written before the
work begins, updated as it moves, and readable by the next call, the settings
panel, or a scheduled run tomorrow.

**A connection belongs to one thread.** Background work runs on the worker
thread while the voice runs on its own, and sharing one sqlite3 connection
across them is the kind of bug that appears once a fortnight under load.
Each thread gets its own.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[3]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

SCHEMA = """
CREATE TABLE IF NOT EXISTS content_jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ref         TEXT NOT NULL UNIQUE,
    kind        TEXT NOT NULL DEFAULT 'script',
    topic       TEXT NOT NULL DEFAULT '',
    style       TEXT NOT NULL DEFAULT '',
    account     TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'queued',
    stage       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    started_at  TEXT,
    finished_at TEXT,
    error       TEXT NOT NULL DEFAULT '',
    result      TEXT
);
CREATE INDEX IF NOT EXISTS idx_content_jobs_created
    ON content_jobs(created_at DESC);

CREATE TABLE IF NOT EXISTS content_assets (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    job_ref  TEXT NOT NULL,
    kind     TEXT NOT NULL,
    path     TEXT NOT NULL DEFAULT '',
    body     TEXT,
    meta     TEXT,
    at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_content_assets_job
    ON content_assets(job_ref, id);
"""

#: Job kinds. `script` is the only one that runs today; the rest are the
#: stages the pipeline grows into, named here so a stored row from a later
#: build still means something to this one.
KIND_SCRIPT = "script"
KIND_REEL = "reel"

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


#: How many hex characters a job reference gets. Eight was nicer to say and
#: too few to be safe: thirty-two bits collide about half the time by seventy
#: thousand jobs, and the collision surfaces as a UNIQUE constraint error
#: raised at whoever created the job - which, before the database came off the
#: voice path, meant raised at the person talking. Forty-eight bits pushes the
#: same coin flip out to sixteen million.
REF_CHARS = 12


def new_ref() -> str:
    """A short, unambiguous handle for a job, said out loud without pain."""
    return uuid.uuid4().hex[:REF_CHARS]


class ContentStore:
    """Jobs and assets, one connection per thread, created on first use."""

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
        # WAL so a long write in the worker thread never blocks a read from
        # the voice thread, which is the one that has somebody waiting on it.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=15000")
        # Cheap and idempotent, so every thread's first connection runs it
        # rather than depending on a flag that says somebody else already did.
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
    # Jobs
    # ------------------------------------------------------------------ #

    def create_job(self, topic: str, kind: str = KIND_SCRIPT, style: str = "",
                   account: str = "", ref: str = "") -> str:
        """Write the job row. Returns the reference it was stored under.

        A reference this method invents may be retried on the astronomically
        unlikely collision. One handed in may not: the caller has already told
        somebody that number, so quietly storing the job under a different one
        would be worse than failing.
        """
        supplied = bool(ref)
        conn = self.connection()
        for attempt in range(3):
            candidate = ref or new_ref()
            try:
                conn.execute(
                    "INSERT INTO content_jobs(ref, kind, topic, style, account, "
                    "status, created_at) VALUES(?,?,?,?,?,?,?)",
                    (candidate, kind, topic.strip(), style.strip(),
                     account.strip(), STATUS_QUEUED, _now()))
            except sqlite3.IntegrityError:
                if supplied or attempt == 2:
                    raise
                continue
            conn.commit()
            return candidate
        raise sqlite3.IntegrityError("couldn't find a free job reference")

    def start_job(self, ref: str, stage: str = "") -> None:
        conn = self.connection()
        conn.execute(
            "UPDATE content_jobs SET status=?, stage=?, started_at=? WHERE ref=?",
            (STATUS_RUNNING, stage, _now(), ref))
        conn.commit()

    def set_stage(self, ref: str, stage: str) -> None:
        conn = self.connection()
        conn.execute("UPDATE content_jobs SET stage=? WHERE ref=?", (stage, ref))
        conn.commit()

    def finish_job(self, ref: str, result: Any = None, stage: str = "") -> None:
        conn = self.connection()
        conn.execute(
            "UPDATE content_jobs SET status=?, stage=?, finished_at=?, result=? "
            "WHERE ref=?",
            (STATUS_DONE, stage, _now(), _dump(result), ref))
        conn.commit()

    def fail_job(self, ref: str, error: str, stage: str = "") -> None:
        conn = self.connection()
        conn.execute(
            "UPDATE content_jobs SET status=?, stage=?, finished_at=?, error=? "
            "WHERE ref=?",
            (STATUS_FAILED, stage, _now(), str(error)[:2000], ref))
        conn.commit()

    def job(self, ref: str) -> dict[str, Any] | None:
        row = self.connection().execute(
            "SELECT * FROM content_jobs WHERE ref=?", (ref,)).fetchone()
        return _job_row(row) if row else None

    def recent_jobs(self, limit: int = 10, status: str = "") -> list[dict[str, Any]]:
        sql = "SELECT * FROM content_jobs"
        args: list[Any] = []
        if status:
            sql += " WHERE status=?"
            args.append(status)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(max(1, int(limit)))
        rows = self.connection().execute(sql, args).fetchall()
        return [_job_row(row) for row in rows]

    # ------------------------------------------------------------------ #
    # Assets
    # ------------------------------------------------------------------ #

    def add_asset(self, job_ref: str, kind: str, body: Any = None,
                  path: str = "", meta: dict[str, Any] | None = None) -> int:
        conn = self.connection()
        cursor = conn.execute(
            "INSERT INTO content_assets(job_ref, kind, path, body, meta, at) "
            "VALUES(?,?,?,?,?,?)",
            (job_ref, kind, path, _dump(body), _dump(meta), _now()))
        conn.commit()
        return int(cursor.lastrowid or 0)

    def assets(self, job_ref: str = "", kind: str = "",
               limit: int = 50) -> list[dict[str, Any]]:
        sql = "SELECT * FROM content_assets"
        clauses, args = [], []
        if job_ref:
            clauses.append("job_ref=?")
            args.append(job_ref)
        if kind:
            clauses.append("kind=?")
            args.append(kind)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(max(1, int(limit)))
        rows = self.connection().execute(sql, args).fetchall()
        return [_asset_row(row) for row in rows]


def _dump(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def _load(value: Any) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def _job_row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["result"] = _load(data.get("result"))
    return data


def _asset_row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["body"] = _load(data.get("body"))
    data["meta"] = _load(data.get("meta"))
    return data


#: One store for the process. Opening a second would be harmless but pointless:
#: the per-thread connections inside it are what actually matter.
_STORE: ContentStore | None = None
_STORE_LOCK = threading.Lock()


def store() -> ContentStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = ContentStore()
        return _STORE
