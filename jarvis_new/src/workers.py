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
import concurrent.futures as futures
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


#: Statuses a job will never leave. Used to decide what may be forgotten.
FINAL = ("done", "failed", "cancelled")

#: How long to wait before the first retry of a job that asked for one, and
#: the longest that wait may grow to as it doubles.
RETRY_BACKOFF = 2.0
RETRY_CEILING = 30.0


async def _guard(coro: Any, label: str) -> None:
    """Run a spawned daemon and say something if it dies.

    A task that raises on a loop nobody awaits is collected in silence. For
    the routine ticker that means every scheduled thing stops happening and
    the only evidence is that nothing happens, which is indistinguishable
    from it never having been set up.
    """
    try:
        await coro
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        print(f"  [worker] {label} stopped: {type(exc).__name__}: {exc}")
        print("  " + traceback.format_exc().replace("\n", "\n  ").strip())


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
    #: Set by `cancel()`. Kept separate from `status` so a worker popping a
    #: job it has already been told to drop can tell the difference between
    #: "you were cancelled" and "the whole host is shutting down".
    cancel_requested: bool = False
    #: How many times the work has been run, and how many times it may be.
    #: A content job spends most of its life waiting on somebody else's API,
    #: and the usual way that fails is a 503 that would have worked a second
    #: later - which, before this, threw away the whole job.
    attempts: int = 0
    max_attempts: int = 1

    def as_dict(self) -> dict[str, Any]:
        seconds = 0.0
        if self.started_at:
            seconds = round((self.finished_at or time.time()) - self.started_at, 1)
        row = {"ref": self.ref, "name": self.name, "status": self.status,
               "seconds": seconds, "error": self.error}
        if self.max_attempts > 1:
            row["attempts"] = self.attempts
            row["max_attempts"] = self.max_attempts
        return row


class WorkerHost:
    """A thread with an event loop in it, and a queue of jobs for that loop."""

    def __init__(self, name: str = "jarvis-workers", concurrency: int = 1) -> None:
        self.name = name
        self.concurrency = max(1, int(concurrency))
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._queue: asyncio.Queue | None = None
        self._workers: list[asyncio.Task] = []
        #: Long-lived tasks put on the loop by `spawn` - the routine ticker is
        #: the one that matters. Held so they are not garbage collected, and
        #: cancelled with the workers on the way out.
        self._daemons: list[asyncio.Task] = []
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        #: ref -> the task running that job right now, so it can be cancelled
        #: on its own without taking the worker down with it.
        self._current: dict[str, asyncio.Future[Any]] = {}
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
            # Cancel, then actually wait. A single tick of the loop is not
            # enough for a cancelled worker to run its finally block, and a
            # worker that never runs it leaves its job on "running" forever
            # and prints "Task was destroyed but it is pending" on the way
            # out. Bounded, because shutdown may not hang on a job that
            # ignores cancellation.
            with contextlib.suppress(Exception):
                pending = [*self._workers, *self._daemons]
                for task in pending:
                    task.cancel()
                loop.run_until_complete(
                    asyncio.wait(pending, timeout=SHUTDOWN_SECONDS / 2))
                self._daemons.clear()
            with contextlib.suppress(Exception):
                loop.run_until_complete(loop.shutdown_asyncgens())
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
               retries: int = 0, backoff: float = RETRY_BACKOFF,
               **kwargs: Any) -> str | None:
        """Queue a job and return its reference straight away.

        `work` may be a coroutine function or an ordinary blocking one; a
        blocking one is run in a thread so it cannot hold the worker loop
        either. Returns None only when the host could not be started at all,
        which the caller should report rather than pretend away.

        `retries` is how many *extra* attempts a failure may have, with a
        delay of `backoff` seconds doubling each time. It is off by default:
        a retry is only ever right for work that is safe to run twice, and
        the caller is the only one who knows whether it is. Cancelling is
        never retried, and neither is a job the host is shutting down under.
        """
        if not self.running and not self.start():
            return None
        loop, queue = self._loop, self._queue
        if loop is None or queue is None:
            return None

        job = Job(ref=uuid.uuid4().hex[:12],
                  name=name or getattr(work, "__name__", "job"),
                  max_attempts=max(1, int(retries) + 1))
        with self._lock:
            self._jobs[job.ref] = job
            self._order.append(job.ref)
            self._trim()

        def push() -> None:
            queue.put_nowait((job, work, args, kwargs, max(0.0, float(backoff))))

        try:
            loop.call_soon_threadsafe(push)
        except Exception:
            with self._lock:
                job.status = "failed"
                job.error = "the background worker wasn't accepting jobs"
            return job.ref
        return job.ref

    def spawn(self, work: Callable[..., Any], *args: Any, name: str = "",
              **kwargs: Any) -> Any:
        """Put a long-lived coroutine on the worker loop, outside the queue.

        The queue is for jobs that finish. A ticker does not finish, and
        submitting one would take a worker slot forever - at the shipped
        concurrency of one, that is the entire pool, and no content job would
        ever run again. So this creates a task directly on the loop instead:
        same thread, same isolation from the voice loop, no queue slot.

        Returns the task, or None if the host would not start. The caller
        holds the return value, and so does the host: the loop keeps only a
        weak reference to a running task, and one that is garbage collected
        mid-flight simply stops, silently, which for a scheduler means every
        routine quietly never firing again.
        """
        if not self.running and not self.start():
            return None
        loop = self._loop
        if loop is None:
            return None

        label = name or getattr(work, "__name__", "daemon")
        handoff: futures.Future = futures.Future()

        def make() -> None:
            try:
                task = loop.create_task(_guard(work(*args, **kwargs), label))
            except Exception as exc:                # pragma: no cover - defensive
                handoff.set_exception(exc)
                return
            with self._lock:
                self._daemons.append(task)
            handoff.set_result(task)

        try:
            loop.call_soon_threadsafe(make)
            return handoff.result(timeout=5.0)
        except Exception:
            return None

    def cancel_spawned(self, task: Any) -> bool:
        """Stop one spawned daemon. True if there was one to stop."""
        loop = self._loop
        if task is None or loop is None:
            return False
        with self._lock:
            if task in self._daemons:
                self._daemons.remove(task)
        with contextlib.suppress(Exception):
            loop.call_soon_threadsafe(task.cancel)
            return True
        return False

    async def _worker(self, index: int) -> None:
        queue = self._queue
        assert queue is not None
        while True:
            job, work, args, kwargs, backoff = await queue.get()
            try:
                await self._run_one(job, work, args, kwargs, backoff)
            finally:
                queue.task_done()

    async def _run_one(self, job: Job, work: Callable[..., Any],
                       args: tuple[Any, ...], kwargs: dict[str, Any],
                       backoff: float = RETRY_BACKOFF) -> None:
        """Run one job, retrying a failure if it was allowed any, and leave it
        in a final state whatever happens.

        The work goes in a task of its own rather than being awaited inline.
        That is what makes `cancel()` possible: cancelling the inner task
        stops one job, while cancelling the worker stops the host, and
        awaiting the work directly would make those two indistinguishable.
        """
        if job.cancel_requested:
            # Cancelled while it sat in the queue. Never started, so there is
            # nothing to stop - just don't run it.
            self._set(job, status="cancelled", finished_at=time.time())
            return

        self._set(job, status="running", started_at=time.time())
        delay = backoff

        while True:
            self._set(job, attempts=job.attempts + 1)
            if inspect.iscoroutinefunction(work):
                inner = asyncio.ensure_future(work(*args, **kwargs))
            else:
                # A blocking call on this loop would stall every other
                # background job behind it, which is the same bug as blocking
                # the voice loop, one layer down.
                inner = asyncio.ensure_future(
                    asyncio.to_thread(work, *args, **kwargs))
            self._current[job.ref] = inner

            try:
                result = await inner
            except asyncio.CancelledError:
                self._set(job, status="cancelled", finished_at=time.time())
                # Only the job was cancelled: the worker carries on to the
                # next one. If the host is going down, the worker's own task
                # is cancelled too and that propagates from the await in
                # _worker.
                if job.cancel_requested:
                    return
                raise
            except Exception as exc:
                reason = f"{type(exc).__name__}: {exc}"
                more = job.attempts < job.max_attempts and not job.cancel_requested
                if more:
                    print(f"  [worker] {job.name} failed on attempt "
                          f"{job.attempts} of {job.max_attempts} ({reason}); "
                          f"retrying in {delay:.0f}s")
                    self._set(job, error=reason)
                    self._current.pop(job.ref, None)
                    try:
                        await asyncio.sleep(delay)
                    except asyncio.CancelledError:
                        self._set(job, status="cancelled",
                                  finished_at=time.time())
                        raise
                    # Doubling rather than a fixed wait: whatever is failing
                    # is usually either momentary or not going to be fixed by
                    # asking again immediately.
                    delay = min(delay * 2, RETRY_CEILING)
                    continue
                self._set(job, status="failed", finished_at=time.time(),
                          error=reason)
                # Printed rather than swallowed: a background job that fails
                # silently is a business that quietly stops working.
                print(f"  [worker] {job.name} failed: {job.error}")
                print("  " + traceback.format_exc().replace("\n", "\n  ").strip())
            else:
                self._set(job, status="done", finished_at=time.time(),
                          result=result, error="")
            finally:
                self._current.pop(job.ref, None)
            return

    # ------------------------------------------------------------------ #
    # Looking at it
    # ------------------------------------------------------------------ #

    def _set(self, job: Job, **fields: Any) -> None:
        """Change a job's state under the lock.

        `status()` reads several fields of the same job in one pass, and a
        reader that catches a job half-updated reports a finished job with no
        finish time - which shows up as a duration counted from now, growing
        every time you look at it.
        """
        with self._lock:
            for key, value in fields.items():
                setattr(job, key, value)

    def cancel(self, ref: str) -> bool:
        """Stop a queued or running job. True if there was one to stop.

        A queued job is simply never started. A running job has its task
        cancelled - and for a job that is a plain blocking function, that is
        honest but partial: Python cannot stop a thread from outside, so the
        work keeps running to its end with nobody waiting for the result. The
        job is cancelled from the caller's point of view, and the machine
        finishes what it started. Jobs that may need stopping mid-flight
        should be coroutines with await points.
        """
        job = self.job(ref)
        if job is None or job.status in FINAL:
            return False
        self._set(job, cancel_requested=True)
        inner = self._current.get(ref)
        loop = self._loop
        if inner is not None and loop is not None:
            with contextlib.suppress(Exception):
                loop.call_soon_threadsafe(inner.cancel)
        else:
            # Still in the queue: mark it now so a reader sees the truth
            # immediately, and the worker drops it when it reaches the front.
            self._set(job, status="cancelled", finished_at=time.time())
        return True

    def job(self, ref: str) -> Job | None:
        with self._lock:
            return self._jobs.get(ref)

    def status(self) -> dict[str, Any]:
        """What the host is doing, for the tool that reads it out loud.

        Anything still live is listed first and is never cut, however long
        ago it was queued. Ordering by recency alone loses exactly the wrong
        job: an overnight render is the oldest entry in the history long
        before it is finished, so it drops off the end of the window while it
        is still running, and "what is it doing" answers "nothing".
        """
        with self._lock:
            jobs = [self._jobs[ref].as_dict() for ref in self._order
                    if ref in self._jobs]
        queued = sum(1 for j in jobs if j["status"] == "queued")
        running = sum(1 for j in jobs if j["status"] == "running")

        live = [job for job in jobs if job["status"] not in FINAL]
        done = [job for job in reversed(jobs) if job["status"] in FINAL]
        return {"running": self.running, "queued": queued, "in_progress": running,
                "concurrency": self.concurrency,
                "jobs": live + done[:max(0, HISTORY - len(live))]}

    def _trim(self) -> None:
        """Forget the oldest finished jobs once there are too many.

        Live jobs are walked past rather than stopped at, and - this is the
        part that matters - they keep their place. The obvious version pops
        the front and, when that job is still live, puts it back at the *end*
        before giving up; the queue stays bounded either way, but a running
        job silently becomes the newest entry in the history, so the status
        tool reports an hour-old render as the thing that just started.

        The durable record is in the database. What is dropped here is only
        the in-memory copy the status tool reads.
        """
        surplus = len(self._order) - HISTORY * 2
        if surplus <= 0:
            return
        kept: list[str] = []
        for ref in self._order:
            job = self._jobs.get(ref)
            if job is None:
                continue                     # already forgotten
            if surplus > 0 and job.status in FINAL:
                self._jobs.pop(ref, None)
                surplus -= 1
                continue
            kept.append(ref)
        self._order = kept


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
