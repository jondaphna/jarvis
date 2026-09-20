"""The background half of JARVIS, owned by the process rather than by a call.

Before this module, workers and routines were started inside the LiveKit
`@server.rtc_session` entrypoint and their shutdown callbacks hung off the job
context. So the scheduler did not exist until somebody spoke to him and stopped
again when they hung up, and "run the morning briefing while I am away" was not
implemented whatever the settings panel said.

Two facts about how LiveKit runs a job make that worse than it first looks, and
both were checked against the installed SDK (livekit-agents >= 1.6.9) rather
than remembered:

- **A job is not the process.** `add_shutdown_callback` runs when the *session*
  ends, and the server gives those callbacks a short timeout before terminating
  (`shutdown_process_timeout`, ten seconds by default). A standing service
  hanging off that is a service with a ten-second lifetime at the end of every
  call. [Job lifecycle](https://docs.livekit.io/agents/server/job/)
- **The entrypoint may not even run in the main process.** `worker.py` sets
  `_default_job_executor_type` to `PROCESS` everywhere except Windows, where a
  BrokenPipeError on some Python versions forces `THREAD`. So on jonathan's
  Windows machine the entrypoint shares the main process, and on Linux - CI,
  and the Dockerfile in this directory - it is a child that exits with the job.
  Anything started from the entrypoint therefore has a different lifetime
  depending on the platform, which is not a thing a scheduler can be built on.

So services start in the **main** process, before `cli.run_app`, and stop when
that process exits. The job executor never touches them.

**Which process owns them.** `butler-web.bat` can start a standalone control API
while `butler-agent.bat` is starting the voice agent. Both processes imported
`workers`, both got their own host, and pausing the workers from the dashboard
paused whichever host happened to be in the process the dashboard was talking
to - while the job being watched ran in the other one. The election here is the
control API's own port: whichever process binds 8765 calls `start()`, and a
process that finds the port taken does not. The dashboard reaches the control
API on that port, so the dashboard is now always talking to the process actually
doing the work. It is a lock file whose lock is a socket, which is the one piece
of mutual exclusion both processes already agreed on.

The cost of that choice is worth stating plainly: if the process holding the
port dies, the services die with it, and the other process does not notice and
take over. `state()` reports the owner and whether this process is it, so the
condition is visible rather than silently wrong, but nothing supervises it yet.
"""

from __future__ import annotations

import atexit
import contextlib
import os
import signal
import threading
import time
from typing import Any

import latch

#: Not started, or deliberately stopped. The dashboard draws this as "off".
STATE_STOPPED = "stopped"
#: Running normally.
STATE_RUNNING = "running"
#: Running, but something did not come up. Separated from `STATE_RUNNING`
#: because `start()` used to write "running" whatever happened - a machine
#: whose scheduler failed to arm reported itself healthy, and the only sign
#: was a line of text in an error field nothing drew.
STATE_DEGRADED = "degraded"
#: Stopped by the kill switch rather than by shutting down. Distinguished from
#: `STATE_STOPPED` because it is the one state somebody chose, and a dashboard
#: that cannot tell "never started" from "you stopped this" will eventually
#: start it again on somebody's behalf.
STATE_KILLED = "killed"

#: How long to wait for the worker host to wind down before giving up on it.
#: A blocking job cannot be interrupted from outside (see `WorkerHost.cancel`),
#: so this is a bound on waiting, not a guarantee of stopping.
STOP_TIMEOUT = 10.0

#: How long to let the loop absorb the cancellations before stopping it. Short
#: on purpose: it is there so a cancelled job gets to run its own cleanup, not
#: so a blocking job gets to finish.
SETTLE_TIMEOUT = 2.0

#: One lane for every lifecycle transition. `start`, `stop`, `kill` and
#: `restart` are all reachable from HTTP handlers, which means concurrently:
#: `start()` used to check the state under `_lock`, release it for the several
#: seconds start-up takes, and write "running" at the end - so a kill that
#: arrived in the middle was overwritten by a start that had already been
#: overtaken. This is held for the whole of a transition, and `_lock` is kept
#: for the short reads that `state()` does so the dashboard never waits behind
#: a start.
_transition = threading.RLock()

_lock = threading.RLock()
_state = STATE_STOPPED
_started_at: float | None = None
_owner = ""
_last_error = ""
_hooks_installed = False


def owner_tag() -> str:
    """This process, in a way a restarted machine cannot confuse.

    A bare pid is reused, so after a reboot a row claiming pid 4312 reads as
    belonging to whatever is running under that number now. The same shape the
    job stores use.
    """
    try:
        import psutil

        process = psutil.Process(os.getpid())
        return f"{os.getpid()}:{int(process.create_time())}"
    except Exception:
        return str(os.getpid())


def state() -> dict[str, Any]:
    """What the background side is doing, for the dashboard and the API.

    Cheap on purpose: the dashboard polls it, and the one thing that must not
    cost anything is looking at the machine. No database, no locks held on
    anything but this module's own state.
    """
    with _lock:
        current, since, owner, error = _state, _started_at, _owner, _last_error

    # The latch, not this process's memory, is what "killed" means. A fresh
    # process that has never called `kill()` still reports killed if the
    # previous one did, which is the whole point of the latch.
    stop_latch = latch.state()
    disabled = bool(stop_latch.get("disabled"))
    if disabled and current in (STATE_STOPPED, STATE_KILLED):
        current = STATE_KILLED

    payload: dict[str, Any] = {
        "state": current,
        "running": current in (STATE_RUNNING, STATE_DEGRADED),
        "degraded": current == STATE_DEGRADED,
        "killed": current == STATE_KILLED or disabled,
        "execution_disabled": disabled,
        "latch": stop_latch,
        "owner": owner,
        "this_process": owner_tag(),
        "owns_services": owner == owner_tag(),
        "uptime_seconds": round(time.time() - since, 1) if since else 0.0,
        "error": error,
        "workers": {"running": False, "paused": False, "queued": 0,
                    "in_progress": 0},
        "routines": {"ticking": False},
    }

    try:
        import workers

        status = workers.host().status()
        payload["workers"] = {
            "running": bool(status.get("running")),
            "paused": bool(status.get("paused")),
            "queued": int(status.get("queued", 0)),
            "in_progress": int(status.get("in_progress", 0)),
        }
    except Exception as exc:
        payload["workers"]["error"] = f"{type(exc).__name__}: {exc}"

    try:
        from routines.engine import get_engine

        payload["routines"] = {"ticking": bool(get_engine().started)}
    except Exception as exc:
        payload["routines"] = {"ticking": False,
                               "error": f"{type(exc).__name__}: {exc}"}

    return payload


def running() -> bool:
    with _lock:
        return _state in (STATE_RUNNING, STATE_DEGRADED)


def start(reason: str = "process start") -> dict[str, Any]:
    """Bring the background side up. Idempotent, and never raises.

    Called by `control_api.serve()` once the port is bound, which is what makes
    "owns the port" and "owns the services" the same thing. Safe to call again:
    a second call while running is a no-op that reports the truth.

    Never raises because every caller is a process trying to start. A machine
    whose scheduler will not arm should still answer the door and still talk;
    it should say loudly that it is not scheduling anything, which is what the
    error in `state()` is for.

    Refuses outright while the stop latch is engaged. That is what makes the
    kill switch survive a restart: the launcher calls this on every start, and
    a latch nobody cleared means it declines and says so.
    """
    global _state, _started_at, _owner, _last_error

    with _transition:
        if latch.engaged():
            with _lock:
                _state = STATE_KILLED
                _started_at = None
                _owner = ""
                _last_error = ("execution is disabled; start the services "
                               "again from the dashboard")
            print("  Background services stay off: JARVIS was stopped, and "
                  "that outlasts closing the window. Start them again from "
                  "the dashboard.")
            return state()

        with _lock:
            if _state in (STATE_RUNNING, STATE_DEGRADED):
                return state()
            _last_error = ""

        # The generation this start belongs to. A kill arriving while start-up
        # is still running bumps it, and the check before publishing "running"
        # below sees that this start has been overtaken and stands down rather
        # than writing itself over the kill.
        began_at_generation = latch.generation()

        print(f"  Starting background services ({reason}).")
        problems: list[str] = []

        try:
            import workers

            if not workers.host().start():
                problems.append("the worker host did not come up")
        except Exception as exc:
            problems.append(f"workers: {type(exc).__name__}: {exc}")

        # Anything left mid-flight by a process that died. On a worker rather
        # than here: it is several SQLite reads, and start-up is not the place
        # to spend them. Submitted before the ticker so a recovered job is
        # already visible by the time the first tick looks.
        try:
            import workers
            from content.pipeline import recover_interrupted

            workers.host().submit(recover_interrupted,
                                  name="recover interrupted jobs")
        except Exception as exc:
            problems.append(f"recovery: {type(exc).__name__}: {exc}")

        try:
            import routines

            if not routines.start_routines():
                problems.append("the routine ticker did not start")
        except Exception as exc:
            problems.append(f"routines: {type(exc).__name__}: {exc}")

        _install_shutdown_hooks()

        if latch.engaged() or latch.generation() != began_at_generation:
            # Overtaken. Undo the partial start rather than reporting a
            # machine that is running after somebody stopped it.
            print("  Background services were stopped while starting; "
                  "standing down.")
            _tear_down()
            with _lock:
                _state = STATE_KILLED
                _started_at = None
                _owner = ""
                _last_error = "stopped while starting"
            return state()

        with _lock:
            # "Running" now means running. A start where the worker host or
            # the ticker failed reports degraded, because a scheduler that
            # never armed and a scheduler that is working look identical from
            # a green dot, and the difference is every routine never firing.
            _state = STATE_DEGRADED if problems else STATE_RUNNING
            _started_at = time.time()
            _owner = owner_tag()
            _last_error = "; ".join(problems)

        if problems:
            print(f"  Background services started with problems: {_last_error}")
        else:
            print("  Background services running: workers and routines.")
        return state()


def stop(timeout: float = STOP_TIMEOUT, killed: bool = False,
         reason: str = "process exit") -> dict[str, Any]:
    """Wind the background side down. Idempotent, and never raises.

    Stops the ticker first, then the host, so that nothing new is scheduled
    into a host that is on its way out.

    What this cannot do is worth being precise about, because the kill switch
    calls it and a person pressing a button called "kill" will believe what
    they are told. Cancelling a job that is a plain blocking function does not
    end the function: Python cannot stop a thread from outside, so the work
    runs to its end with nobody waiting for the result. The job is cancelled
    from every caller's point of view, no further job starts, and an FFmpeg
    render or a paid API call already in flight finishes on its own. `state()`
    reports what is still winding down rather than claiming an empty machine.

    Order matters here and is deliberate: when this is a kill, the latch is
    committed **before** anything is stopped. A stop that fails halfway then
    still leaves a machine that refuses new work, whereas stopping first and
    recording it afterwards leaves a window - and a crash in that window - in
    which everything is down and nothing says it should stay down.
    """
    global _state, _started_at, _owner

    with _transition:
        if killed:
            latch.engage(reason=reason)

        with _lock:
            was = _state
        if was not in (STATE_RUNNING, STATE_DEGRADED):
            with _lock:
                if killed:
                    _state = STATE_KILLED
                    _started_at = None
                    _owner = ""
            return state()

        print(f"  Stopping background services ({reason}).")
        cancelled = _tear_down(timeout=timeout)

        with _lock:
            _state = STATE_KILLED if killed else STATE_STOPPED
            _started_at = None
            _owner = ""

        print(f"  Background services stopped; {cancelled} job(s) cancelled.")
        result = state()
        result["cancelled"] = cancelled
        return result


def _tear_down(timeout: float = STOP_TIMEOUT) -> int:
    """Stop the ticker and the host. Returns how many jobs were cancelled.

    Never raises: every caller is already shutting something down.
    """
    cancelled = 0
    try:
        import routines

        routines.get_engine().stop()
    except Exception as exc:
        print(f"  [services] the routine ticker did not stop cleanly: {exc}")

    try:
        import workers

        host = workers.host()
        # Cancel before stopping, so a job that *can* be stopped is, rather
        # than being left to the shutdown timeout. `cancel` is best effort by
        # design; see `stop`'s docstring.
        for job in host.status().get("jobs", []):
            ref = str(job.get("ref") or "")
            if ref and job.get("status") in ("queued", "running"):
                try:
                    if host.cancel(ref):
                        cancelled += 1
                except Exception:
                    pass
        # A moment for the loop to actually process those cancellations before
        # the loop is stopped underneath it. Without it, `stop()` halts the
        # loop while a worker is still unwinding, and the cancelled job's
        # `finally` - the one that writes its final state - never runs, so the
        # row stays on "running" until the next start-up's recovery pass.
        # Bounded tightly: a blocking job will not finish inside it and this
        # is not the place to wait for one.
        deadline = time.monotonic() + min(SETTLE_TIMEOUT, timeout)
        while time.monotonic() < deadline:
            if host.status().get("in_progress", 0) == 0:
                break
            time.sleep(0.05)

        host.stop(timeout=timeout)
    except Exception as exc:
        print(f"  [services] the worker host did not stop cleanly: {exc}")
    return cancelled


def kill(reason: str = "kill switch") -> dict[str, Any]:
    """The dashboard's master stop.

    Writes the latch first and then shuts down, so that nothing starts the
    services again - not a submitted job, not a routine, not the next launch
    of the program. Clearing it is `restart()`, which is reachable only from
    the dashboard.
    """
    result = stop(killed=True, reason=reason)
    result["killed"] = True
    return result


def restart(reason: str = "restart requested") -> dict[str, Any]:
    """Undo a kill. The one thing that clears the latch.

    A stop button with no way back is not a control, it is a trap: the only
    remedy would be closing the window and running the launcher again, and
    somebody will press it to see what it does.

    Releasing the latch is deliberately *here* and not inside `start()`.
    Start runs on every launch; if it cleared the latch, closing the window
    would undo the kill switch, which is the exact bug this replaces.
    """
    with _transition:
        stop(reason=reason)
        latch.release(reason=reason)
        return start(reason=reason)


def _install_shutdown_hooks() -> None:
    """Stop cleanly when the process ends, however it ends.

    `atexit` covers the ordinary exit and an unhandled exception. The signal
    handlers cover Ctrl-C and a `taskkill`, which is how these windows
    actually get closed - without them a SQLite row stays on "running" until
    the next start-up's recovery pass notices the owner is gone.
    """
    global _hooks_installed

    with _lock:
        if _hooks_installed:
            return
        _hooks_installed = True

    atexit.register(_on_exit)

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        number = getattr(signal, name, None)
        if number is None:
            continue                     # SIGBREAK is Windows-only, and so on
        try:
            previous = signal.getsignal(number)
        except (OSError, ValueError):
            continue

        def handler(signum: int, frame: Any, _previous: Any = previous) -> None:
            stop(reason=f"signal {signum}")
            # Chain rather than swallow: the default for SIGINT raises
            # KeyboardInterrupt, and an agent that stops handling Ctrl-C is a
            # window nobody can close.
            if callable(_previous):
                _previous(signum, frame)
            elif _previous == signal.SIG_DFL:
                signal.signal(signum, signal.SIG_DFL)
                os.kill(os.getpid(), signum)

        try:
            signal.signal(number, handler)
        except (OSError, ValueError):
            # Not the main thread, or not supported here. The atexit hook
            # still runs; this is the belt to its braces.
            continue


def _on_exit() -> None:
    with contextlib.suppress(Exception):
        stop(reason="process exit")


def reset_for_tests() -> None:
    """Forget this module's state. For tests, which start their own services."""
    global _state, _started_at, _owner, _last_error

    with _lock:
        _state = STATE_STOPPED
        _started_at = None
        _owner = ""
        _last_error = ""
    latch.reset_for_tests()
