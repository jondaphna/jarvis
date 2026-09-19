"""Running against a LiveKit server on your own machine.

LiveKit Cloud is the easy path, but its sign-up can hit an SSO wall on some
email domains - and a local server is genuinely just as good for this:
everything stays on your machine, there is no account, and the audio path
(WebRTC, echo cancellation, turn detection) is identical.

`livekit-server --dev` ships a fixed development key pair, which is why no
credentials are needed. That pair is public knowledge, so it is only ever used
here for a server bound to your own machine.
"""

from __future__ import annotations

import socket
from typing import Any

from ..core.events import log

#: The well-known pair `livekit-server --dev` starts with.
DEV_URL = "ws://localhost:7880"
DEV_KEY = "devkey"
DEV_SECRET = "secret"
DEV_PORT = 7880


def is_running(host: str = "127.0.0.1", port: int = DEV_PORT,
               timeout: float = 1.0) -> bool:
    """Is a LiveKit server listening locally?"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def apply(config: Any) -> None:
    """Point this run at the local dev server, without storing credentials.

    Set in memory only: writing a publicly known secret into the encrypted
    vault would be a confusing thing to leave behind.
    """
    import os

    os.environ.setdefault("LIVEKIT_URL", DEV_URL)
    os.environ.setdefault("LIVEKIT_API_KEY", DEV_KEY)
    os.environ.setdefault("LIVEKIT_API_SECRET", DEV_SECRET)


# The official release archives, named by goreleaser convention.
RELEASES = "https://github.com/livekit/livekit/releases"
KNOWN_VERSION = "1.13.7"


def _platform_asset(version: str) -> tuple[str, str]:
    """(asset name, binary name inside it) for this machine."""
    import platform
    import sys

    machine = platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "amd64"

    if sys.platform == "win32":
        return f"livekit_{version}_windows_{arch}.zip", "livekit-server.exe"
    if sys.platform == "darwin":
        return f"livekit_{version}_darwin_{arch}.zip", "livekit-server"
    return f"livekit_{version}_linux_{arch}.tar.gz", "livekit-server"


def bin_dir():
    from .. import paths

    target = paths.ROOT / "bin"
    target.mkdir(parents=True, exist_ok=True)
    return target


def find_binary():
    """The livekit-server executable, from PATH or our own bin folder."""
    import shutil
    import sys

    name = "livekit-server.exe" if sys.platform == "win32" else "livekit-server"

    found = shutil.which("livekit-server")
    if found:
        from pathlib import Path
        return Path(found)

    local_copy = bin_dir() / name
    return local_copy if local_copy.exists() else None


def download_url(version: str = KNOWN_VERSION) -> str:
    asset, _ = _platform_asset(version)
    return f"{RELEASES}/download/v{version}/{asset}"


def download(version: str = KNOWN_VERSION, on_progress=None):
    """Fetch the official release and unpack just the server binary.

    Only ever from github.com/livekit/livekit - the URL is built here rather
    than taken from anywhere, and the host is checked before anything is read.
    """
    import io
    import tarfile
    import urllib.request
    import zipfile
    from urllib.parse import urlparse

    url = download_url(version)
    if urlparse(url).hostname != "github.com":
        raise RuntimeError("refusing to download from anywhere but github.com")

    _, binary_name = _platform_asset(version)
    if on_progress:
        on_progress(f"Downloading {url}")

    with urllib.request.urlopen(url, timeout=180) as response:
        payload = response.read()

    target = bin_dir() / binary_name
    if url.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            member = next((n for n in archive.namelist()
                           if n.rsplit("/", 1)[-1] == binary_name), None)
            if member is None:
                raise RuntimeError(f"{binary_name} wasn't in the archive")
            target.write_bytes(archive.read(member))
    else:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
            member = next((m for m in archive.getmembers()
                           if m.name.rsplit("/", 1)[-1] == binary_name), None)
            if member is None:
                raise RuntimeError(f"{binary_name} wasn't in the archive")
            extracted = archive.extractfile(member)
            if extracted is None:
                raise RuntimeError("could not read the binary from the archive")
            target.write_bytes(extracted.read())

    import sys
    if sys.platform != "win32":
        target.chmod(0o755)
    if on_progress:
        on_progress(f"Saved to {target}")
    return target


def start(binary=None, bind: str | None = None):
    """Launch `livekit-server --dev` as a child process.

    Its output is captured rather than discarded: when this fails it is almost
    always for a specific, fixable reason (a port already in use, no IPv6 on
    the machine), and "it exited immediately" helps nobody.
    """
    import subprocess
    import sys
    import time

    binary = binary or find_binary()
    if binary is None:
        raise FileNotFoundError("livekit-server isn't installed")

    command = [str(binary), "--dev"]
    if bind:
        command += ["--bind", bind]

    creation = 0
    if sys.platform == "win32":
        creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        creationflags=creation,
    )

    # Give it a moment to bind before anyone tries to connect.
    for _ in range(40):
        if is_running():
            return process
        if process.poll() is not None:
            raise RuntimeError(_why_it_stopped(process, bind))
        time.sleep(0.25)

    process.terminate()
    raise TimeoutError("the LiveKit server didn't start listening in time")


def _why_it_stopped(process, bind: str | None) -> str:
    """Turn the server's own output into something worth reading."""
    try:
        out, err = process.communicate(timeout=5)
    except Exception:
        out, err = "", ""
    text = f"{out or ''}\n{err or ''}".strip()
    tail = " / ".join(line.strip() for line in text.splitlines()[-3:] if line.strip())

    lowered = text.lower()
    if "address family not supported" in lowered and not bind:
        return ("the LiveKit server couldn't listen - this machine has IPv6 "
                "disabled. Retrying on IPv4 usually fixes it.")
    if "address already in use" in lowered:
        return ("port 7880 is already taken - a LiveKit server may already be "
                "running, or something else is using it.")
    return f"the LiveKit server stopped straight away: {tail or 'no output'}"


def start_resilient(binary=None):
    """Start the server, falling back to IPv4-only when IPv6 is unavailable."""
    try:
        return start(binary)
    except RuntimeError as exc:
        if "IPv6" not in str(exc):
            raise
        log.info("retrying the LiveKit server on IPv4 only")
        return start(binary, bind="127.0.0.1")


def install_help() -> str:
    return f"""To run LiveKit on this machine, easiest first:

  Let JARVIS fetch it for you
      python -m jarvis realtime --install-server

    It downloads the official release from
      {RELEASES}
    into your JARVIS folder, and starts it automatically from then on.

  Or do it by hand
      1. Download {_platform_asset(KNOWN_VERSION)[0]} from
           {RELEASES}
      2. Unzip it and put livekit-server on your PATH
      3. Run it in its own window:  livekit-server --dev

  (There is no winget package for it - that was my mistake.)"""
