"""The stop latch: one switch that survives everything.

The dashboard's kill switch used to be a variable in one process's memory.
Pressing it stopped the worker host and set `_state = "killed"`, and both of
those facts lived only in that process, which meant two things that the panel
told you were not happening:

- **Anything could start it again.** `WorkerHost.submit` began with "if the
  host isn't running, start it", so a routine firing, a voice command, or the
  dashboard's own status poll brought the workers straight back up. Pressing
  kill and then asking for a Reel script ran the Reel script.
- **Closing the window forgot it.** The state was in memory, so the next
  launch started every service as though nothing had ever been switched off.

So the latch lives in a file. It is committed *before* anything is stopped, on
the principle that the order has to be deny-then-stop rather than stop-then-
deny: a stop that fails halfway must still leave the machine refusing work.
Every path that could start work reads it. Clearing it is a deliberate act
through the dashboard, and nothing the model can reach writes to it.

**What this does not do**, said plainly because the panel used to overclaim.
A job that is a plain blocking call - an FFmpeg render, an HTTP request to a
provider already in flight - runs to its end: Python cannot stop a thread from
outside. The latch stops anything *new* from starting, in this process and in
any other that reads the same file, and it survives a restart. It does not
reach out and cancel work somebody else's server already accepted.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from jarvis import paths  # noqa: E402  - needs the path above

#: Beside the control token, in the JARVIS home rather than the repository:
#: the latch is a property of this machine, not of this checkout.
FILENAME = "execution.disabled"

_lock = threading.RLock()
#: (stat signature, parsed contents). `engaged()` runs on every submission, so
#: the file is stat-ed rather than re-read each time and parsed only when it
#: has actually changed.
_cache: tuple[tuple[int, int] | None, dict[str, Any]] | None = None


class ExecutionDisabledError(RuntimeError):
    """Raised when work is refused because the stop latch is engaged.

    A distinct type so callers can tell "you switched this off" from "this
    broke". The dashboard says the first out loud; the second is a bug.
    """


def latch_path() -> Path:
    return paths.ROOT / FILENAME


def guard(what: str = "run background work") -> None:
    """Refuse `what` if execution is disabled. Does nothing otherwise."""
    current = _read()
    if not current.get("disabled"):
        return
    reason = str(current.get("reason") or "").strip()
    detail = f": {reason}" if reason else "."
    raise ExecutionDisabledError(
        f"JARVIS is stopped, so it won't {what}{detail} Start the services "
        f"again from the dashboard when you want work to run.")


def _signature(path: Path) -> tuple[int, int] | None:
    try:
        info = path.stat()
    except OSError:
        return None
    return (info.st_mtime_ns, info.st_size)


def _read() -> dict[str, Any]:
    """What the file says, or a disengaged latch if there is no file.

    A file that exists but cannot be read or parsed counts as **engaged**. It
    is the only safe reading: this file exists to say "do not run things", and
    the failure to understand it is not evidence that it said the opposite.
    """
    global _cache

    path = latch_path()
    signature = _signature(path)

    with _lock:
        cached = _cache
    if cached is not None and cached[0] == signature:
        return cached[1]

    if signature is None:
        state: dict[str, Any] = {"disabled": False}
    else:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("the stop latch is not a JSON object")
            state = {
                "disabled": raw.get("disabled") is True,
                "at": str(raw.get("at") or ""),
                "reason": str(raw.get("reason") or ""),
                "by": str(raw.get("by") or ""),
                "generation": int(raw.get("generation") or 0),
            }
        except Exception as exc:
            state = {
                "disabled": True,
                "at": "",
                "reason": f"the stop latch file could not be read ({exc}), "
                          f"so execution stays disabled",
                "by": "",
                "generation": 0,
                "unreadable": True,
            }

    with _lock:
        _cache = (signature, state)
    return state


def engaged() -> bool:
    """Is execution disabled right now?"""
    return bool(_read().get("disabled"))


def state() -> dict[str, Any]:
    """The latch, for the dashboard."""
    payload = dict(_read())
    payload["path"] = str(latch_path())
    return payload


def generation() -> int:
    """How many times the latch has been engaged on this machine.

    A start that began before a kill can finish after it. Comparing the
    generation it started under with the current one tells that start it has
    been overtaken, without needing a lock held across the whole of start-up.
    """
    return int(_read().get("generation") or 0)


def _write(state: dict[str, Any]) -> dict[str, Any]:
    global _cache

    path = latch_path()
    paths.ensure_dirs()
    tmp = path.with_suffix(f".{os.getpid()}-{int(time.time() * 1000)}.tmp")
    with _lock:
        try:
            with open(tmp, "w", encoding="utf-8") as stream:
                json.dump(state, stream, indent=2)
                stream.flush()
                # The point of the latch is that it survives a machine that
                # stopped rudely, which is exactly the case where an unflushed
                # write is lost.
                os.fsync(stream.fileno())
            tmp.replace(path)
        finally:
            with contextlib.suppress(OSError):
                if tmp.exists():
                    tmp.unlink()
        _cache = None
    return state


def engage(reason: str = "kill switch", by: str = "owner") -> dict[str, Any]:
    """Disable execution, durably. Returns the latch's new state."""
    current = _read()
    return _write({
        "disabled": True,
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "reason": reason,
        "by": by,
        "generation": int(current.get("generation") or 0) + 1,
    })


def release(reason: str = "restart requested", by: str = "owner") -> dict[str, Any]:
    """Allow execution again. A deliberate act, and only the owner's."""
    current = _read()
    return _write({
        "disabled": False,
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "reason": reason,
        "by": by,
        "generation": int(current.get("generation") or 0),
    })


def reset_cache_for_tests() -> None:
    """Forget the parsed copy, keeping the file. For tests that edit it."""
    global _cache

    with _lock:
        _cache = None


def reset_for_tests() -> None:
    """Forget the latch entirely, file and cache. For tests."""
    global _cache

    with _lock:
        _cache = None
        with contextlib.suppress(OSError):
            latch_path().unlink()
