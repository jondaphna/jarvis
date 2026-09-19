"""Glue between Qt (main thread) and the Assistant (asyncio, worker thread).

Qt owns the main thread; the Assistant wants an event loop. Rather than pull in
qasync, the loop runs on its own thread and results come back through Qt
signals, which are queued and therefore safe across threads.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from typing import Any, Callable, Coroutine

from PyQt6.QtCore import QObject, pyqtSignal

from ..core import events
from ..core.events import bus, log


class AsyncRunner(QObject):
    """Runs coroutines on a background event loop and reports back to Qt."""

    finished = pyqtSignal(object, object)   # (result, error)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name="jarvis-async",
                                        daemon=True)
        self._thread.start()
        self._ready.wait(timeout=10)

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        try:
            self._loop.run_forever()
        finally:
            pending = asyncio.all_tasks(self._loop)
            for task in pending:
                task.cancel()
            self._loop.close()

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None:
            raise RuntimeError("The async loop never started.")
        return self._loop

    def submit(self, coro: Coroutine,
               on_done: Callable[[Any, Exception | None], None] | None = None):
        """Schedule a coroutine. `on_done` is called on the Qt main thread."""
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)

        def _callback(fut) -> None:
            try:
                result, error = fut.result(), None
            except (asyncio.CancelledError, concurrent.futures.CancelledError):
                # Stopping voice mode cancels its task; that's normal, not a fault.
                # Both names are caught because a threadsafe future can raise
                # either depending on the Python version.
                result, error = None, None
            except Exception as exc:   # surfaced to the UI, never swallowed
                result, error = None, exc
                log.warning("background task failed: %s", exc, exc_info=True)
            self.finished.emit(result, error)
            if on_done is not None:
                # Signals are queued, so hop through one to reach the GUI thread.
                self._dispatch(on_done, result, error)

        future.add_done_callback(_callback)
        return future

    def _dispatch(self, callback: Callable, result: Any, error: Exception | None) -> None:
        from PyQt6.QtCore import QTimer
        QTimer.singleShot(0, lambda: callback(result, error))

    def stop(self) -> None:
        if self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=3)


class EventRelay(QObject):
    """Turns JARVIS events into a Qt signal."""

    event = pyqtSignal(str, str, object)    # kind, message, data

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._unsubscribe = bus.subscribe(self._on_event)

    def _on_event(self, item: events.Event) -> None:
        # Emitting from a worker thread is fine: Qt queues cross-thread signals.
        self.event.emit(item.kind, item.message, dict(item.data))

    def close(self) -> None:
        self._unsubscribe()
