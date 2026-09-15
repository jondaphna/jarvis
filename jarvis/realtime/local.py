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


def install_help() -> str:
    return """To run LiveKit on this machine:

  1. Install it
       winget install LiveKit.LiveKitServer
     If winget doesn't find it, download livekit-server for Windows from
       https://github.com/livekit/livekit/releases
     and put the .exe somewhere on your PATH.

  2. Start it in its own terminal, and leave it running
       livekit-server --dev

     It prints a line with ws://localhost:7880 when it's ready.

  3. Back here
       python -m jarvis realtime --local"""
