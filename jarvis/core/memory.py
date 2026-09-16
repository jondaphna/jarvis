"""Persistent memory, logs and the audit trail - one SQLite file.

Four jobs:
  1. Conversation history, so JARVIS remembers what you said yesterday.
  2. Durable facts ("my business is X", "post at 9pm") it learns over time.
  3. The audit trail - every permission decision and every action taken.
  4. Mission run history + usage accounting, so you can see what worked.

SQLite in WAL mode handles the mixed reader/writer load (UI reading while a
mission writes) without a server. All access is serialised through one
connection guarded by a reentrant lock; calls are short, so contention is a
non-issue at this scale.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

from .. import paths

SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    channel     TEXT NOT NULL DEFAULT 'chat',
    title       TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role            TEXT NOT NULL,
    content         TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    model           TEXT,
    meta            TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, id);

CREATE TABLE IF NOT EXISTS facts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    key        TEXT NOT NULL UNIQUE,
    value      TEXT NOT NULL,
    category   TEXT NOT NULL DEFAULT 'general',
    source     TEXT NOT NULL DEFAULT 'user',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_facts_category ON facts(category);

CREATE TABLE IF NOT EXISTS actions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    at         TEXT NOT NULL,
    actor      TEXT NOT NULL,
    capability TEXT NOT NULL,
    resource   TEXT,
    risk       TEXT NOT NULL,
    decision   TEXT NOT NULL,
    reason     TEXT,
    run_id     INTEGER,
    details    TEXT
);
CREATE INDEX IF NOT EXISTS idx_actions_at ON actions(at);
CREATE INDEX IF NOT EXISTS idx_actions_run ON actions(run_id);

CREATE TABLE IF NOT EXISTS approvals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT NOT NULL,
    capability  TEXT NOT NULL,
    resource    TEXT,
    risk        TEXT NOT NULL,
    summary     TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    decided_at  TEXT,
    note        TEXT,
    payload     TEXT
);
CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status, at);

CREATE TABLE IF NOT EXISTS mission_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    mission_id  TEXT NOT NULL,
    name        TEXT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL DEFAULT 'running',
    trigger     TEXT NOT NULL DEFAULT 'schedule',
    error       TEXT,
    summary     TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_mission ON mission_runs(mission_id, started_at);

CREATE TABLE IF NOT EXISTS mission_steps (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER NOT NULL REFERENCES mission_runs(id) ON DELETE CASCADE,
    idx         INTEGER NOT NULL,
    name        TEXT,
    plugin      TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'running',
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    output      TEXT,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS idx_steps_run ON mission_steps(run_id, idx);

CREATE TABLE IF NOT EXISTS usage (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    at            TEXT NOT NULL,
    model         TEXT NOT NULL,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read    INTEGER NOT NULL DEFAULT 0,
    cache_write   INTEGER NOT NULL DEFAULT 0,
    cost_usd      REAL NOT NULL DEFAULT 0,
    purpose       TEXT
);
CREATE INDEX IF NOT EXISTS idx_usage_at ON usage(at);

CREATE TABLE IF NOT EXISTS lessons (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger     TEXT NOT NULL UNIQUE,
    said        TEXT NOT NULL DEFAULT '',
    steps       TEXT NOT NULL,
    source      TEXT NOT NULL DEFAULT 'taught',
    uses        INTEGER NOT NULL DEFAULT 0,
    confidence  REAL NOT NULL DEFAULT 1.0,
    created_at  TEXT NOT NULL,
    used_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_lessons_rank
    ON lessons(confidence DESC, uses DESC, used_at DESC);

CREATE TABLE IF NOT EXISTS artifacts (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    at      TEXT NOT NULL,
    path    TEXT NOT NULL,
    kind    TEXT NOT NULL,
    run_id  INTEGER,
    note    TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _now_precise() -> str:
    """Timestamps for lessons, to the millisecond.

    Lessons are ordered by when they were last touched, and whole seconds are
    not fine enough: teach it two things in the same second, or look one up
    immediately after, and the ordering falls back to insertion order - which
    puts the OLDEST first and quietly loses the thing that just happened.

    Sorts correctly against existing second-precision rows: at the same second
    the millisecond form compares greater, which is the right answer, because
    it was written later.
    """
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass
class Message:
    role: str
    content: str
    created_at: str = ""
    model: str | None = None
    meta: dict[str, Any] | None = None


class Memory:
    """Everything JARVIS remembers."""

    def __init__(self, db_path: Path | None = None) -> None:
        paths.ensure_dirs()
        self.path = Path(db_path) if db_path else paths.DB_FILE
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._migrate()

    def _migrate(self) -> None:
        with self._write() as conn:
            conn.executescript(_SCHEMA)
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def _query(self, sql: str, args: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, args))

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ #
    # Conversations
    # ------------------------------------------------------------------ #

    def start_conversation(self, channel: str = "chat", title: str | None = None) -> int:
        with self._write() as conn:
            cur = conn.execute(
                "INSERT INTO conversations(started_at, updated_at, channel, title) "
                "VALUES(?,?,?,?)",
                (_now(), _now(), channel, title),
            )
            return int(cur.lastrowid)

    def add_message(
        self,
        conversation_id: int,
        role: str,
        content: str,
        model: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> int:
        with self._write() as conn:
            cur = conn.execute(
                "INSERT INTO messages(conversation_id, role, content, created_at, model, meta) "
                "VALUES(?,?,?,?,?,?)",
                (conversation_id, role, content, _now(), model,
                 json.dumps(meta) if meta else None),
            )
            conn.execute("UPDATE conversations SET updated_at=? WHERE id=?",
                         (_now(), conversation_id))
            return int(cur.lastrowid)

    def history(self, conversation_id: int, limit: int = 40) -> list[Message]:
        rows = self._query(
            "SELECT role, content, created_at, model, meta FROM messages "
            "WHERE conversation_id=? ORDER BY id DESC LIMIT ?",
            (conversation_id, limit),
        )
        return [
            Message(
                role=r["role"],
                content=r["content"],
                created_at=r["created_at"],
                model=r["model"],
                meta=json.loads(r["meta"]) if r["meta"] else None,
            )
            for r in reversed(rows)
        ]

    def recent_conversations(self, limit: int = 10) -> list[dict[str, Any]]:
        rows = self._query(
            "SELECT id, started_at, updated_at, channel, title FROM conversations "
            "ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in rows]

    def search_messages(self, needle: str, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._query(
            "SELECT conversation_id, role, content, created_at FROM messages "
            "WHERE content LIKE ? ORDER BY id DESC LIMIT ?",
            (f"%{needle}%", limit),
        )
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    # Long-term facts
    # ------------------------------------------------------------------ #

    def remember(self, key: str, value: str, category: str = "general",
                 source: str = "user") -> None:
        with self._write() as conn:
            conn.execute(
                "INSERT INTO facts(key, value, category, source, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "category=excluded.category, source=excluded.source, updated_at=excluded.updated_at",
                (key.strip().lower(), value, category, source, _now(), _now()),
            )

    def recall(self, key: str) -> str | None:
        rows = self._query("SELECT value FROM facts WHERE key=?", (key.strip().lower(),))
        return rows[0]["value"] if rows else None

    def forget(self, key: str) -> bool:
        with self._write() as conn:
            cur = conn.execute("DELETE FROM facts WHERE key=?", (key.strip().lower(),))
            return cur.rowcount > 0

    def all_facts(self, category: str | None = None) -> list[dict[str, Any]]:
        if category:
            rows = self._query(
                "SELECT key, value, category, updated_at FROM facts WHERE category=? "
                "ORDER BY key", (category,))
        else:
            rows = self._query(
                "SELECT key, value, category, updated_at FROM facts ORDER BY category, key")
        return [dict(r) for r in rows]

    def facts_block(self, limit: int = 60) -> str:
        """Facts rendered for the system prompt. Empty string when there are none."""
        facts = self.all_facts()[:limit]
        if not facts:
            return ""
        lines = [f"- {f['key']}: {f['value']}" for f in facts]
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # Lessons - things you taught it, and things it watched you accept
    #
    # A fact is what JARVIS knows. A lesson is what JARVIS can do, and it is
    # the difference between an assistant that remembers your name and one
    # that gets better at your work. Teach it once, out loud, and the next
    # time the same words arrive the recipe is already in front of it - so it
    # acts instead of reasoning its way back to the same answer.
    # ------------------------------------------------------------------ #

    #: Words that change nothing about which job is being asked for. Stripping
    #: them is what makes "Jarvis, could you open my Spotify please" and "open
    #: spotify" the same lesson rather than two near-identical rows that each
    #: get learned separately and neither of which ever fires.
    _FILLER = (
        "hey jarvis", "ok jarvis", "okay jarvis", "jarvis", "please", "thanks",
        "thank you", "could you", "can you", "would you", "will you",
        "i want you to", "i need you to", "i want", "for me", "right now",
        "go ahead", "and", "just", "now", "my", "the", "a", "an", "some",
        "again", "quickly", "then",
    )

    @staticmethod
    def normalise_trigger(text: str) -> str:
        """The lookup key for a spoken phrase: lowercase, bare, filler gone."""
        import re

        cleaned = re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower())
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        for word in Memory._FILLER:
            cleaned = re.sub(rf"\b{re.escape(word)}\b", " ", cleaned)
        return re.sub(r"\s+", " ", cleaned).strip()

    def learn(self, trigger: str, steps: str, said: str = "",
              source: str = "taught", confidence: float | None = None) -> str:
        """Record how to do something. Teaching again replaces the old way.

        Replacing rather than appending is the point: when you correct JARVIS,
        you want the wrong way gone, not kept alongside the right one where it
        can be picked again.
        """
        key = self.normalise_trigger(trigger)
        if not key or not steps.strip():
            return ""
        if confidence is None:
            confidence = 1.0 if source in ("taught", "corrected") else 0.5
        with self._write() as conn:
            existing = conn.execute(
                "SELECT source, uses FROM lessons WHERE trigger=?", (key,)
            ).fetchone()
            # Something you said beats something it inferred from watching, so
            # a watched recipe never overwrites one you taught by hand.
            if existing and existing["source"] in ("taught", "corrected") \
                    and source == "watched":
                return key
            kept_uses = existing["uses"] if existing else 0
            conn.execute(
                "INSERT INTO lessons(trigger, said, steps, source, uses, "
                "confidence, created_at) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(trigger) DO UPDATE SET steps=excluded.steps, "
                "said=excluded.said, source=excluded.source, "
                "confidence=excluded.confidence",
                (key, (said or trigger).strip(), steps.strip(), source,
                 kept_uses, float(confidence), _now_precise()),
            )
        return key

    def lessons(self, limit: int = 40) -> list[dict[str, Any]]:
        """Everything it has been taught, most recently touched first.

        Ordering used to be confidence, then use count. That quietly broke
        teaching: a lesson taught today has been used zero times, so once you
        had more lessons than fit in the prompt, THE NEXT THING YOU TAUGHT IT
        WAS NEVER SHOWN - stored, findable, and invisible. Recency is ranked
        alongside use for exactly that reason, so something you just said out
        loud always gets a hearing.
        """
        rows = self._query(
            "SELECT id, trigger, said, steps, source, uses, confidence, "
            "created_at, used_at FROM lessons "
            # Purely recency, deliberately. How often a lesson gets used is
            # a separate claim on the prompt and `top_lessons` weighs it
            # separately; mixing the two here is what let a well-used old
            # lesson push out the one taught thirty seconds ago.
            "ORDER BY confidence DESC, "
            "         COALESCE(used_at, created_at) DESC, "
            # Timestamps are whole seconds, so lessons learned in the same
            # second tie - and a tie broken by insertion order puts the OLDEST
            # first, which is the bug this ordering exists to fix.
            "         id DESC "
            "LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    def top_lessons(self, limit: int = 30) -> list[dict[str, Any]]:
        """The ones worth spending prompt space on: recent AND well-used.

        Two different claims on a limited space. What you asked for an hour
        ago is likely to come up again; so is the thing you ask for every day
        even if it was last week. Taking the best of both beats either alone,
        and beats a single ORDER BY that has to choose.
        """
        recent = self.lessons(limit=limit)
        frequent = [dict(r) for r in self._query(
            "SELECT id, trigger, said, steps, source, uses, confidence, "
            "created_at, used_at FROM lessons "
            "WHERE uses > 0 ORDER BY confidence DESC, uses DESC, id DESC "
            "LIMIT ?", (limit,))]

        merged: dict[str, dict[str, Any]] = {}
        # Interleaved so neither list can crowd the other out of the budget.
        for pair in zip(recent, frequent + recent):
            for row in pair:
                if row["trigger"] not in merged and len(merged) < limit:
                    merged[row["trigger"]] = row
        for row in recent + frequent:          # fill any remaining space
            if row["trigger"] not in merged and len(merged) < limit:
                merged[row["trigger"]] = row
        # Taught before watched, so your own words are read first.
        return sorted(merged.values(),
                      key=lambda r: (-r["confidence"], -r["uses"]))

    def lesson_for(self, text: str) -> dict[str, Any] | None:
        """The lesson for this phrase: an exact key first, then near enough.

        People do not repeat themselves word for word. "play some music on
        Spotify" and "put music on Spotify" are one request, and a store that
        only answers to an exact match would learn each of them separately and
        fire for neither. So after the exact key fails, the best overlapping
        lesson wins - provided it overlaps a lot, because a wrong recipe
        confidently applied is worse than none.
        """
        key = self.normalise_trigger(text)
        if not key:
            return None
        rows = self._query(
            "SELECT id, trigger, said, steps, source, uses, confidence "
            "FROM lessons WHERE trigger=?", (key,))
        if rows:
            return dict(rows[0])

        words = set(key.split())
        if len(words) < 2:
            return None

        # Narrowed in the database rather than in Python. Scoring the two
        # hundred most recent rows meant that past two hundred lessons, the
        # oldest ones could only be found by saying the exact words again -
        # and nobody remembers the exact words they used in June. This looks
        # at every lesson that shares a word with the request, however old.
        clauses = " OR ".join(["trigger LIKE ?"] * len(words))
        rows = self._query(
            "SELECT id, trigger, said, steps, source, uses, confidence "
            f"FROM lessons WHERE {clauses}",
            tuple(f"%{word}%" for word in sorted(words)))

        best, best_score = None, 0.0
        for row in rows:
            other = set(str(row["trigger"]).split())
            if not other:
                continue
            overlap = len(words & other) / len(words | other)
            # Ties go to the one you taught rather than one it inferred.
            score = overlap + (0.001 if row["confidence"] >= 1.0 else 0.0)
            if score > best_score:
                best, best_score = dict(row), score
        return best if best_score >= 0.6 else None

    def near_lessons(self, text: str, limit: int = 5) -> list[dict[str, Any]]:
        """Lessons that share wording with this, best first.

        For when matching on words isn't enough. "Put my Spotify on" and "open
        my Spotify" are one request to a person and share a single word; no
        overlap threshold fixes that, because it needs to know the phrases
        mean the same thing. Handing the near misses to the model is cheaper
        and better than buying an embedding service to decide it - the model
        already knows what was said and what the words mean.
        """
        key = self.normalise_trigger(text)
        words = set(key.split())
        if not words:
            return []
        clauses = " OR ".join(["trigger LIKE ?"] * len(words))
        rows = self._query(
            "SELECT id, trigger, said, steps, source, uses, confidence "
            f"FROM lessons WHERE {clauses}",
            tuple(f"%{word}%" for word in sorted(words)))

        scored = []
        for row in rows:
            other = set(str(row["trigger"]).split())
            shared = len(words & other)
            if not shared:
                continue
            scored.append((shared / len(words | other),
                           row["confidence"], dict(row)))
        scored.sort(key=lambda row: (row[0], row[1]), reverse=True)
        return [row[2] for row in scored[:limit]]

    def lesson_used(self, trigger: str) -> None:
        """Count a lesson as having been useful. Never raises."""
        key = self.normalise_trigger(trigger)
        if not key:
            return
        try:
            with self._write() as conn:
                conn.execute(
                    "UPDATE lessons SET uses = uses + 1, used_at = ? "
                    "WHERE trigger = ?", (_now_precise(), key))
        except Exception:
            pass

    def unlearn(self, trigger: str) -> bool:
        key = self.normalise_trigger(trigger)
        with self._write() as conn:
            return conn.execute(
                "DELETE FROM lessons WHERE trigger=?", (key,)).rowcount > 0

    def lessons_block(self, limit: int = 25) -> str:
        """Lessons rendered for the system prompt. Empty when there are none.

        Deliberately capped. The value is in the model seeing the recipe
        without having to go looking for it, and a block long enough to bury
        the current request defeats that.
        """
        rows = self.top_lessons(limit=limit)
        if not rows:
            return ""
        lines = []
        for row in rows:
            said = (row["said"] or row["trigger"]).strip()
            steps = " ".join(str(row["steps"]).split())
            marker = "" if row["source"] in ("taught", "corrected") else " (from watching you)"
            lines.append(f'- When they say "{said}"{marker}: {steps}')
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # Audit trail
    # ------------------------------------------------------------------ #

    def log_action(
        self,
        actor: str,
        capability: str,
        resource: str | None,
        risk: str,
        decision: str,
        reason: str | None = None,
        run_id: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> int:
        with self._write() as conn:
            cur = conn.execute(
                "INSERT INTO actions(at, actor, capability, resource, risk, decision, "
                "reason, run_id, details) VALUES(?,?,?,?,?,?,?,?,?)",
                (_now(), actor, capability, resource, risk, decision, reason, run_id,
                 json.dumps(details, default=str) if details else None),
            )
            return int(cur.lastrowid)

    def recent_actions(self, limit: int = 100, run_id: int | None = None) -> list[dict[str, Any]]:
        if run_id is not None:
            rows = self._query(
                "SELECT * FROM actions WHERE run_id=? ORDER BY id DESC LIMIT ?", (run_id, limit))
        else:
            rows = self._query("SELECT * FROM actions ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    # Approval queue
    # ------------------------------------------------------------------ #

    def queue_approval(
        self,
        capability: str,
        resource: str | None,
        risk: str,
        summary: str,
        requested_by: str,
        payload: dict[str, Any] | None = None,
    ) -> int:
        with self._write() as conn:
            cur = conn.execute(
                "INSERT INTO approvals(at, capability, resource, risk, summary, "
                "requested_by, payload) VALUES(?,?,?,?,?,?,?)",
                (_now(), capability, resource, risk, summary, requested_by,
                 json.dumps(payload, default=str) if payload else None),
            )
            return int(cur.lastrowid)

    def pending_approvals(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._query(
            "SELECT * FROM approvals WHERE status='pending' ORDER BY id DESC LIMIT ?", (limit,))
        out = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item["payload"]) if item["payload"] else {}
            out.append(item)
        return out

    def get_approval(self, approval_id: int) -> dict[str, Any] | None:
        rows = self._query("SELECT * FROM approvals WHERE id=?", (approval_id,))
        if not rows:
            return None
        item = dict(rows[0])
        item["payload"] = json.loads(item["payload"]) if item["payload"] else {}
        return item

    def decide_approval(self, approval_id: int, status: str, note: str = "") -> bool:
        if status not in {"approved", "denied", "expired"}:
            raise ValueError("status must be approved, denied or expired")
        with self._write() as conn:
            cur = conn.execute(
                "UPDATE approvals SET status=?, decided_at=?, note=? "
                "WHERE id=? AND status='pending'",
                (status, _now(), note, approval_id),
            )
            return cur.rowcount > 0

    # ------------------------------------------------------------------ #
    # Mission runs
    # ------------------------------------------------------------------ #

    def start_run(self, mission_id: str, name: str = "", trigger: str = "schedule") -> int:
        with self._write() as conn:
            cur = conn.execute(
                "INSERT INTO mission_runs(mission_id, name, started_at, trigger) VALUES(?,?,?,?)",
                (mission_id, name, _now(), trigger),
            )
            return int(cur.lastrowid)

    def finish_run(self, run_id: int, status: str, error: str = "", summary: str = "") -> None:
        with self._write() as conn:
            conn.execute(
                "UPDATE mission_runs SET finished_at=?, status=?, error=?, summary=? WHERE id=?",
                (_now(), status, error or None, summary or None, run_id),
            )

    def start_step(self, run_id: int, idx: int, name: str, plugin: str) -> int:
        with self._write() as conn:
            cur = conn.execute(
                "INSERT INTO mission_steps(run_id, idx, name, plugin, started_at) "
                "VALUES(?,?,?,?,?)",
                (run_id, idx, name, plugin, _now()),
            )
            return int(cur.lastrowid)

    def finish_step(self, step_id: int, status: str, output: Any = None,
                    error: str = "") -> None:
        text = output if isinstance(output, str) else json.dumps(output, default=str)
        if text and len(text) > 20_000:
            text = text[:20_000] + "... (truncated)"
        with self._write() as conn:
            conn.execute(
                "UPDATE mission_steps SET finished_at=?, status=?, output=?, error=? WHERE id=?",
                (_now(), status, text, error or None, step_id),
            )

    def runs(self, mission_id: str | None = None, limit: int = 25) -> list[dict[str, Any]]:
        if mission_id:
            rows = self._query(
                "SELECT * FROM mission_runs WHERE mission_id=? ORDER BY id DESC LIMIT ?",
                (mission_id, limit))
        else:
            rows = self._query("SELECT * FROM mission_runs ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    def run_steps(self, run_id: int) -> list[dict[str, Any]]:
        return [dict(r) for r in self._query(
            "SELECT * FROM mission_steps WHERE run_id=? ORDER BY idx", (run_id,))]

    # ------------------------------------------------------------------ #
    # Usage accounting
    # ------------------------------------------------------------------ #

    def log_usage(self, model: str, input_tokens: int = 0, output_tokens: int = 0,
                  cache_read: int = 0, cache_write: int = 0, cost_usd: float = 0.0,
                  purpose: str = "") -> None:
        with self._write() as conn:
            conn.execute(
                "INSERT INTO usage(at, model, input_tokens, output_tokens, cache_read, "
                "cache_write, cost_usd, purpose) VALUES(?,?,?,?,?,?,?,?)",
                (_now(), model, input_tokens, output_tokens, cache_read, cache_write,
                 cost_usd, purpose),
            )

    def spend_since(self, hours: int = 24) -> float:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
        rows = self._query("SELECT COALESCE(SUM(cost_usd),0) AS total FROM usage WHERE at>=?",
                           (cutoff,))
        return float(rows[0]["total"] or 0.0)

    def usage_summary(self, days: int = 7) -> list[dict[str, Any]]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        rows = self._query(
            "SELECT model, SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens, "
            "SUM(cost_usd) AS cost_usd, COUNT(*) AS calls FROM usage WHERE at>=? GROUP BY model "
            "ORDER BY cost_usd DESC", (cutoff,))
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    # Artifacts + dashboard stats
    # ------------------------------------------------------------------ #

    def log_artifact(self, path: str, kind: str, run_id: int | None = None,
                     note: str = "") -> None:
        with self._write() as conn:
            conn.execute(
                "INSERT INTO artifacts(at, path, kind, run_id, note) VALUES(?,?,?,?,?)",
                (_now(), str(path), kind, run_id, note),
            )

    def stats(self, days: int = 7) -> dict[str, Any]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        runs = self._query(
            "SELECT status, COUNT(*) AS n FROM mission_runs WHERE started_at>=? GROUP BY status",
            (cutoff,))
        artifacts = self._query(
            "SELECT kind, COUNT(*) AS n FROM artifacts WHERE at>=? GROUP BY kind", (cutoff,))
        pending = self._query("SELECT COUNT(*) AS n FROM approvals WHERE status='pending'")
        return {
            "days": days,
            "runs": {r["status"]: r["n"] for r in runs},
            "artifacts": {r["kind"]: r["n"] for r in artifacts},
            "pending_approvals": pending[0]["n"],
            "spend_24h": round(self.spend_since(24), 4),
            "spend_7d": round(self.spend_since(24 * days), 4),
        }
