"""Audio device discovery and selection.

Device *indexes* shift around when you plug in a headset or a monitor, so
JARVIS stores device *names* ("NVIDIA High Definition Audio", "Lenovo FHD
WC310") and resolves them at use time. A name that no longer matches falls back
to the system default with a warning, rather than throwing.
"""

from __future__ import annotations

from typing import Any

from .events import log


def _sounddevice():
    try:
        import sounddevice
        return sounddevice
    except Exception:
        return None


def available() -> bool:
    return _sounddevice() is not None


def list_devices(kind: str = "all") -> list[dict[str, Any]]:
    """Every audio device, with its index, name and channel counts."""
    sd = _sounddevice()
    if sd is None:
        return []
    try:
        devices = sd.query_devices()
    except Exception as exc:
        log.warning("couldn't list audio devices: %s", exc)
        return []

    try:
        default_in, default_out = sd.default.device
    except Exception:
        default_in = default_out = None

    rows: list[dict[str, Any]] = []
    for index, device in enumerate(devices):
        inputs = int(device.get("max_input_channels", 0))
        outputs = int(device.get("max_output_channels", 0))
        if kind == "input" and inputs <= 0:
            continue
        if kind == "output" and outputs <= 0:
            continue
        rows.append({
            "index": index,
            "name": device.get("name", f"device {index}"),
            "inputs": inputs,
            "outputs": outputs,
            "default_input": index == default_in,
            "default_output": index == default_out,
            "hostapi": device.get("hostapi"),
        })
    return rows


def resolve_device(selector: Any, want_input: bool) -> int | None:
    """Turn a stored name (or index) into a device index sounddevice accepts.

    Returns None for "use the system default", which is also what happens when
    a remembered device has been unplugged.
    """
    if selector is None or selector == "":
        return None
    if isinstance(selector, int):
        return selector
    if isinstance(selector, str) and selector.strip().isdigit():
        return int(selector.strip())

    wanted = str(selector).strip().lower()
    if wanted in ("default", "auto", "system"):
        return None

    candidates = list_devices("input" if want_input else "output")
    if not candidates:
        return None

    names = [(row, row["name"].lower()) for row in candidates]

    for row, name in names:                         # exact
        if name == wanted:
            return row["index"]
    for row, name in names:                         # starts with
        if name.startswith(wanted):
            return row["index"]
    for row, name in names:                         # contains
        if wanted in name:
            return row["index"]
    for row, name in names:                         # loose word overlap
        words = [w for w in wanted.split() if len(w) > 2]
        if words and all(w in name for w in words):
            return row["index"]

    log.warning("audio device %r not found - using the system default. "
                "Run `jarvis devices` to see what's available.", selector)
    return None


def describe_selection(selector: Any, want_input: bool) -> str:
    """Human-readable answer to 'which device will actually be used?'"""
    if not selector:
        return "system default"
    index = resolve_device(selector, want_input)
    if index is None:
        return f"{selector} (not found - falling back to the system default)"
    for row in list_devices("input" if want_input else "output"):
        if row["index"] == index:
            return f"{row['name']} (#{index})"
    return f"#{index}"
