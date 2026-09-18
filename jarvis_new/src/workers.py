"""Background work that must never be heard.

The business engine and the conversation want different things from the same
process. A conversation is judged in tens of milliseconds: the audio pipeline
has to keep feeding frames, and anything that sits on its event loop for a
second is a stutter the user hears. Writing a Reel script takes ten to sixty
seconds, rendering one takes minutes, and neither is allowed to cost the voice
a single frame.

So background jobs do not run on the voice loop at all. This module owns a
separate daemon thread with its own event loop, a queue, and a small number of
workers pulling from it. Submitting is a queue push that returns immediately -
the voice tool that starts a job answers the user in the same breath.

Three properties are deliberate:

* **Nothing raises into the caller.** `submit` returns a job reference or
  None; a worker that explodes logs it and takes the next job. A failing
  content job must never be able to end a call.
* **One job at a time, by default.** These jobs spend money and hammer the
  same browser profile. Serialising them is worth more than throughput, and
  `concurrency` is there for when it isn't.
* **It stops when the process does.** The thread is a daemon and the loop is
  closed on `stop()`, which the agent registers as a shutdown callback. A
  worker pool that outlives the call that started it is how one leaked browser
  becomes ten.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

#: How long `stop()` waits for the loop to drain before giving up on it. A
#: render that ignores cancellation must not hold the whole process open.
SHUTDOWN_SECONDS = 5.0

#: How many finished jobs to keep in memory for `status()`. The durable record
#: lives in the database; this is only what the status tool reads back.
HISTORY = 40


@dataclass
class Job:
    """One unit of background work, as the host sees it."""

    ref: str
    name: str
    status: str = "queued"          # queued | running | done | failed | cancelled
    queued_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    finished_at: float = 0.0
    error: str = ""
    result: Any = None

    def as_dict(self) -> dict[str, Any]:
        seconds = 0.0
        if self.started_at:
            seconds = round((self.finished_at or time.time()) - self.started_at, 1)
        return {"ref": self.ref, "name": self.name, "status": self.status,
                "seconds": seconds, "error": self.error}


class WorkerHost:
    """A thread with an event loop in it, and a queue of jobs for that loop."""

    def __init__(self, name: str = "jarvis-workers", concurrency: int = 1) -> None:
        self.name = name
        self.concurrency = max(1, int(concurrency))
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._queue: asyncio.Queue | None = None
        self._workers: list[asyncio.Task] = []
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._lock = threading.RLock()
        self._ready = threading.Event()

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and self._ready.is_set())

    def start(self) -> bool:
        """Bring the thread up. Safe to call twice; the second call is a no-op."""
        with self._lock:
            if self.running:
                return True
            self._ready.clear()
            self._thread = threading.Thread(target=self._run, name=self.name,
                                            daemon=True)
            self._thread.start()
        # Waiting here is not a stall: the loop is up in microseconds, and the
        # alternative is a submit that silently lands nowhere because the queue
        # does not exist yet.
        return self._ready.wait(timeout=5.0)

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._queue = asyncio.Queue()
        try:
            self._workers = [loop.create_task(self._worker(i))
                             for i in range(self.concurrency)]
            self._ready.set()
            loop.run_forever()
        finally:
            self._ready.clear()
            try:
                for task in self._workers:
                    task.cancel()
                loop.run_until_complete(asyncio.sleep(0))
            except Exception:
                pass
            with contextlib.suppress(Exception):
                loop.close()

    def stop(self, timeout: float = SHUTDOWN_SECONDS) -> None:
        """Ask the loop to finish. Never raises - it runs during shutdown."""
        loop = self._loop
        if loop is None:
            return
        with contextlib.suppress(Exception):
            loop.call_soon_threadsafe(loop.stop)
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        self._ready.clear()

    # ------------------------------------------------------------------ #
    # Submitting
    # ------------------------------------------------------------------ #

    def submit(self, work: Callable[..., Any], *args: Any, name: str = "",
               **kwargs: Any) -> str | None:
        """Queue a job and return its reference straight away.

        `work` may be a coroutine function or an ordinary blocking one; a
        blocking one is run in a thread so it cannot hold the worker loop
        either. Returns None only when the host could not be started at all,
        which the caller should report rather than pretend away.
        """
        if not self.running and not self.start():
            return None
        loop, queue = self._loop, self._queue
        if loop is None or queue is None:
            return None

        job = Job(ref=uuid.uuid4().hex[:12], name=name or getattr(work, "__name__", "job"))
        with self._lock:
            self._jobs[job.ref] = job
            self._order.append(job.ref)
            self._trim()

        def push() -> None:
            queue.put_nowait((job, work, args, kwargs))

        try:
            loop.call_soon_threadsafe(push)
        except Exception:
            with self._lock:
                job.status = "failed"
                job.error = "the background worker wasn't accepting jobs"
            return job.ref
        return job.ref

    async def _worker(self, index: int) -> None:
        queue = self._queue
        assert queue is not None
        while True:
            job, work, args, kwargs = await queue.get()
            job.status = "running"
            job.started_at = time.time()
            try:
                if inspect.iscoroutinefunction(work):
                    job.result = await work(*args, **kwargs)
                else:
                    # A blocking call on this loop would stall every other
                    # background job behind it, which is the same bug as
                    # blocking the voice loop, one layer down.
                    job.result = await asyncio.to_thread(work, *args, **kwargs)
            except asyncio.CancelledError:
                job.status = "cancelled"
                job.finished_at = time.time()
                raise
            except Exception as exc:
                job.status = "failed"
                job.error = f"{type(exc).__name__}: {exc}"
                # Printed rather than swallowed: a background job that fails
                # silently is a business that quietly stops working.
                print(f"  [worker] {job.name} failed: {job.error}")
                print("  " + traceback.format_exc().replace("\n", "\n  ").strip())
            else:
                job.status = "done"
            finally:
                job.finished_at = job.finished_at or time.time()
                queue.task_done()

    # ------------------------------------------------------------------ #
    # Looking at it
    # ------------------------------------------------------------------ #

    def job(self, ref: str) -> Job | None:
        with self._lock:
            return self._jobs.get(ref)

    def status(self) -> dict[str, Any]:
        with self._lock:
            jobs = [self._jobs[ref].as_dict() for ref in self._order
                    if ref in self._jobs]
        queued = sum(1 for j in jobs if j["status"] == "queued")
        running = sum(1 for j in jobs if j["status"] == "running")
        return {"running": self.running, "queued": queued, "in_progress": running,
                "concurrency": self.concurrency, "jobs": list(reversed(jobs))[:HISTORY]}

    def _trim(self) -> None:
        while len(self._order) > HISTORY * 2:
            stale = self._order.pop(0)
            job = self._jobs.get(stale)
            if job is not None and job.status in ("queued", "running"):
                # Still live: keep it and drop the next one instead.
                self._order.append(stale)
                return
            self._jobs.pop(stale, None)


# --------------------------------------------------------------------------- #
# The one the agent uses
# --------------------------------------------------------------------------- #

_HOST: WorkerHost | None = None
_HOST_LOCK = threading.Lock()


def host() -> WorkerHost:
    """The process-wide background host, created on first use."""
    global _HOST
    with _HOST_LOCK:
        if _HOST is None:
            _HOST = WorkerHost()
        return _HOST


def start_workers() -> bool:
    """Start the host and say so. Called once when a call begins."""
    started = host().start()
    print("  Background workers ready." if started
          else "  Background workers could not start; voice is unaffected.")
    return started


async def stop_workers() -> None:
    """Shutdown callback shape: awaited by the agent when the call ends."""
    host().stop()
