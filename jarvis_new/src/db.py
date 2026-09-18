"""One SQLite connection per thread, shared by everything that needs it.

Both stores in this build - content jobs and standing routines - live in the
same `jarvis.db`, and until this module existed each opened its own connection
on each thread. So the worker thread held two handles to one file, the voice
thread held two more, and every one of them was a separate WAL reader holding
its own snapshot and its own share of the lock traffic. Nothing about that is
catastrophic; it is simply twice as much of a thing this codebase has already
been bitten by once.

What this gives instead:

* **One connection per (file, thread).** Asking twice on one thread returns the
  same handle. Two threads never share one, because sharing a sqlite3
  connection across threads is the bug that surfaces once a fortnight under
  load and never in a test.
* **Each caller's schema applied once per connection**, tracked on the
  connection itself rather than by a module flag - a flag says "somebody
  already did this" about a connection that may not exist any more.
* **A way to let go.** `close_thread()` closes what this thread opened, which
  the worker host calls on the way down, and tests call between cases.

The pragmas are the ones the content engine's latency work settled on: WAL, so
a long write on the worker never blocks a read on the voice thread, and a busy
timeout, so contention is a wait rather than an exception raised at whoever is
talking.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import threading
from pathlib import Path

#: How long a statement waits for the write lock before giving up. Long enough
#: that a render committing its assets is a pause, short enough that a genuine
#: deadlock is still an error rather than a hang.
BUSY_TIMEOUT_MS = 15000

#: The connect() timeout, in seconds, for acquiring the database itself.
CONNECT_TIMEOUT = 15.0

_local = threading.local()

_registry_lock = threading.Lock()


class _Pool:
    """One thread's connections, and which schemas each has already run.

    The two belong together. Keeping the applied-schema marks in a second
    thread-local was a quiet bug: closing the connections without clearing the
    marks left a reopened file believing its tables had already been created,
    and the next query failed on a table that was never there.
    """

    __slots__ = ("applied", "conns")

    def __init__(self) -> None:
        self.conns: dict[str, sqlite3.Connection] = {}
        self.applied: dict[str, set[int]] = {}

    def clear(self) -> int:
        """Let go of every connection here, and say how many there were.

        `close()` on a connection belonging to another thread raises - the
        same guard that stops one being *used* across threads applies to
        closing it. Dropping the reference is the release that works from
        anywhere: the handle closes when the last reference to it goes, and
        the owning thread finds an empty pool and opens afresh.
        """
        count = len(self.conns)
        for conn in list(self.conns.values()):
            with contextlib.suppress(sqlite3.Error):
                conn.close()          # another thread's raises; dropped below
        self.conns.clear()
        self.applied.clear()
        return count


#: Every live pool, so `close_all()` can reach the threads this one does not
#: own. Guarded, because threads are created and retired while it is read.
_registry: dict[int, _Pool] = {}


def _pool() -> _Pool:
    """This thread's pool, registered so `close_all()` can reach it.

    The registration is checked on every call rather than only on creation: a
    `close_all()` from another thread drops this thread's entry, and a pool
    that is not in the registry is a pool nothing can close.
    """
    pool = getattr(_local, "pool", None)
    if pool is None:
        pool = _Pool()
        _local.pool = pool
    ident = threading.get_ident()
    with _registry_lock:
        if _registry.get(ident) is not pool:
            _registry[ident] = pool
    return pool


def _key(path: Path | str) -> str:
    """One spelling per file, so two ways of writing it share a connection.

    `resolve()` normalises `..` and follows symlinks whether or not the file
    exists yet, which matters because the first caller is usually the one
    creating it.
    """
    return str(Path(path).resolve())


def connect(path: Path | str, schema: str = "") -> sqlite3.Connection:
    """This thread's connection to `path`, opening it if it is the first ask.

    `schema` is executed once per connection. It must be idempotent - every
    statement in it a CREATE ... IF NOT EXISTS - because a second caller with
    a different schema for the same file will run its own against the same
    handle.
    """
    key = _key(path)
    pool = _pool()

    conn = pool.conns.get(key)
    if conn is None:
        target = Path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(target), timeout=CONNECT_TIMEOUT)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        pool.conns[key] = conn

    if schema:
        # Tracked beside the connection rather than on it: sqlite3.Connection
        # is a C type with no instance dictionary, so setting an attribute on
        # one raises - which is exactly the sort of thing that works in a
        # sketch and fails on the first real call.
        applied = pool.applied.setdefault(key, set())
        mark = hash(schema)
        if mark not in applied:
            conn.executescript(schema)
            conn.commit()
            applied.add(mark)
    return conn


def close_thread() -> int:
    """Close every connection this thread opened. Returns how many."""
    pool = getattr(_local, "pool", None)
    if pool is None:
        return 0
    count = pool.clear()
    with _registry_lock:
        if _registry.get(threading.get_ident()) is pool:
            _registry.pop(threading.get_ident(), None)
    _local.pool = None
    return count


def close_path(path: Path | str) -> int:
    """Close this thread's connection to one file, if it has one."""
    pool = getattr(_local, "pool", None)
    if pool is None:
        return 0
    key = _key(path)
    conn = pool.conns.pop(key, None)
    pool.applied.pop(key, None)
    if conn is None:
        return 0
    try:
        conn.close()
    except sqlite3.Error:
        return 0
    return 1


def close_all() -> int:
    """Close every connection on every thread. For shutdown, and for tests.

    A connection this thread does not own cannot be closed from here - sqlite3
    guards that as it guards using one - so it is released instead and closes
    when the last reference to it goes. Either way the pool is emptied rather
    than discarded, so a thread still holding one finds it empty and opens
    afresh, schemas included: those are cleared with the connections they were
    applied to.
    """
    with _registry_lock:
        pools = list(_registry.values())
        _registry.clear()
    return sum(pool.clear() for pool in pools)


# --------------------------------------------------------------------------- #
# Additive migrations
# --------------------------------------------------------------------------- #

def ensure_columns(conn: sqlite3.Connection, table: str,
                   columns: dict[str, str]) -> list[str]:
    """Add columns a later build needs to a table an earlier one created.

    `CREATE TABLE IF NOT EXISTS` is a no-op against a table that already
    exists, so a column added to the schema string never reaches anybody who
    has run the previous version - which is everybody. Returns the columns it
    actually added.
    """
    have = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if not have:
        return []                                # no such table yet
    added = []
    for name, declaration in columns.items():
        if name in have:
            continue
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
        added.append(name)
    if added:
        conn.commit()
    return added


# --------------------------------------------------------------------------- #
# Which process owns a row
# --------------------------------------------------------------------------- #

def process_tag() -> str:
    """This process, in a form that stays wrong after it dies.

    A bare process id is not enough: ids are reused, so a job stamped with one
    can be read back after a reboot as belonging to something entirely
    different that happens to be running. Pairing it with the process start
    time makes the tag unforgeable by coincidence.
    """
    pid = os.getpid()
    try:
        import psutil

        return f"{pid}:{int(psutil.Process(pid).create_time())}"
    except Exception:
        return f"{pid}:0"


def is_alive(tag: str) -> bool:
    """Is the process that wrote this tag still running?

    Unknown or unreadable tags are reported as *not* alive. That is the safe
    direction here: the caller uses this to decide whether a job is an orphan,
    and the cost of being wrong is a job marked interrupted that could have
    been left alone - against the cost of the other error, which is a job
    stuck on "running" until somebody notices, which is the bug this exists
    to fix.
    """
    text = (tag or "").strip()
    if not text:
        return False
    pid_text, _, started = text.partition(":")
    try:
        pid = int(pid_text)
    except ValueError:
        return False

    dated = bool(started) and started != "0"
    try:
        import psutil
    except Exception:
        # Without psutil the only thing that can be established is whether the
        # id is this process's own, and a tag carrying a start time cannot have
        # come from a process that could not read one. Unverifiable, so no.
        return pid == os.getpid() and not dated

    try:
        if not psutil.pid_exists(pid):
            return False
        if dated:
            # Checked for this process too. Ids are reused, so "it is my own
            # id" is not the same claim as "I wrote this", and a tag left by a
            # previous life of this id would otherwise read as live forever.
            return int(psutil.Process(pid).create_time()) == int(started)
        return True
    except Exception:
        return False


def open_count() -> int:
    """How many connections are open across all threads. For tests."""
    with _registry_lock:
        return sum(len(pool.conns) for pool in _registry.values())
