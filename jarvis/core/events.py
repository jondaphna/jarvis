"""A tiny synchronous/async event bus plus the app-wide logger.

Everything interesting JARVIS does is published here. The CLI prints events,
the UI paints them, and the database records them - none of those layers has to
know about the others.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Any, Callable, Iterable

from .. import paths

# --------------------------------------------------------------------------- #
# Event types (plain strings - plugins may invent their own)
# --------------------------------------------------------------------------- #
LISTENING = "voice.listening"
TRANSCRIPT = "voice.transcript"
SPEAKING = "voice.speaking"
THINKING = "brain.thinking"
REPLY = "brain.reply"
REPLY_DELTA = "brain.reply_delta"
TOOL_CALL = "brain.tool_call"
TOOL_RESULT = "brain.tool_result"
PERMISSION = "permission.decision"
APPROVAL_NEEDED = "permission.approval_needed"
MISSION_START = "mission.start"
MISSION_STEP = "mission.step"
MISSION_DONE = "mission.done"
MISSION_FAILED = "mission.failed"
ERROR = "error"
INFO = "info"
USAGE = "usage"


@dataclass
class Event:
    kind: str
    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return f"[{self.kind}] {self.message}"


Subscriber = Callable[[Event], Any]


class EventBus:
    """Fan-out to sync callbacks and async queues. Never raises into the caller."""

    def __init__(self, history: int = 500) -> None:
        self._subs: list[Subscriber] = []
        self._queues: list[asyncio.Queue] = []
        self._lock = threading.RLock()
        self._history: deque[Event] = deque(maxlen=history)

    def subscribe(self, callback: Subscriber) -> Callable[[], None]:
        with self._lock:
            self._subs.append(callback)

        def unsubscribe() -> None:
            with self._lock:
                if callback in self._subs:
                    self._subs.remove(callback)

        return unsubscribe

    def stream(self, maxsize: int = 0) -> asyncio.Queue:
        """An asyncio.Queue fed with every future event (for the UI / CLI)."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        with self._lock:
            self._queues.append(queue)
        return queue

    def close_stream(self, queue: asyncio.Queue) -> None:
        with self._lock:
            if queue in self._queues:
                self._queues.remove(queue)

    def publish(self, kind: str, message: str = "", **data: Any) -> Event:
        event = Event(kind=kind, message=message, data=data)
        with self._lock:
            self._history.append(event)
            subs = list(self._subs)
            queues = list(self._queues)

        for callback in subs:
            try:
                result = callback(event)
                if asyncio.iscoroutine(result):
                    _spawn(result)
            except Exception:  # a broken listener must never break the app
                log.debug("event subscriber failed", exc_info=True)

        for queue in queues:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass
        return event

    def recent(self, kinds: Iterable[str] | None = None, limit: int = 50) -> list[Event]:
        with self._lock:
            events = list(self._history)
        if kinds is not None:
            wanted = set(kinds)
            events = [e for e in events if e.kind in wanted]
        return events[-limit:]


def _spawn(coro: Any) -> None:
    try:
        asyncio.get_running_loop().create_task(coro)
    except RuntimeError:
        coro.close()


#: The bus every component shares.
bus = EventBus()


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #

log = logging.getLogger("jarvis")


def setup_logging(level: int = logging.INFO, console: bool = True) -> logging.Logger:
    """Configure file + console logging. Idempotent."""
    if getattr(setup_logging, "_done", False):
        log.setLevel(level)
        return log

    paths.ensure_dirs()
    log.setLevel(level)
    log.propagate = False

    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s %(name)s: %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")

    file_handler = RotatingFileHandler(
        paths.LOG_DIR / "jarvis.log", maxBytes=4_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    log.addHandler(file_handler)

    if console:
        stream = logging.StreamHandler()
        stream.setFormatter(logging.Formatter("%(levelname)-7s %(message)s"))
        stream.setLevel(level)
        log.addHandler(stream)

    # Third-party chatter stays in the file, out of the user's face.
    for noisy in ("httpx", "httpx2", "httpcore", "apscheduler", "urllib3", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    setup_logging._done = True  # type: ignore[attr-defined]
    return log
