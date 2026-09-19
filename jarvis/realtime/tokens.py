"""Minting the join tokens browsers and phones need to enter the call."""

from __future__ import annotations

import secrets
from datetime import timedelta

DEFAULT_ROOM = "jarvis"


class TokenError(RuntimeError):
    """LiveKit credentials are missing or wrong."""


def credentials(config) -> tuple[str, str, str]:
    """(url, api_key, api_secret), or a clear explanation of what's missing."""
    url = config.key("LIVEKIT_URL") or config.settings.get("realtime.url", "")
    key = config.key("LIVEKIT_API_KEY")
    secret = config.key("LIVEKIT_API_SECRET")

    missing = [name for name, value in
               (("LIVEKIT_URL", url), ("LIVEKIT_API_KEY", key),
                ("LIVEKIT_API_SECRET", secret)) if not value]
    if missing:
        raise TokenError(
            "Realtime voice needs LiveKit credentials - the free tier is plenty.\n"
            "  1. Sign up at cloud.livekit.io and make a project\n"
            "  2. Copy its URL, API key and secret\n"
            "  3. Run:  jarvis keys set " + "  /  jarvis keys set ".join(missing))
    return normalise_url(str(url)), str(key), str(secret)


def normalise_url(url: str) -> str:
    """Accept the project URL however it was copied.

    LiveKit speaks WebSocket, but the project page shows an https:// address
    too and that is the one people paste. Connecting with it fails in the
    browser with nothing useful on screen, so fix it here instead of making
    that someone's afternoon.
    """
    url = url.strip().rstrip("/")
    if url.startswith("https://"):
        return "wss://" + url[len("https://"):]
    if url.startswith("http://"):
        return "ws://" + url[len("http://"):]
    if not url.startswith(("ws://", "wss://")) and url:
        return "wss://" + url
    return url


def mint(config, identity: str = "", room: str = DEFAULT_ROOM,
         minutes: int = 120) -> dict[str, str]:
    """A join token for one participant.

    The identity is unique per token on purpose. LiveKit treats identity as the
    primary key for a participant, so two devices sharing one - your laptop and
    your phone, or the same page opened twice - means the second arrival
    silently evicts the first. Naming everyone "you" would make picking up your
    phone hang up your desktop.
    """
    from livekit import api

    url, key, secret = credentials(config)
    identity = identity or f"you-{secrets.token_hex(4)}"
    token = (
        api.AccessToken(key, secret)
        .with_identity(identity)
        .with_name(config.settings.get("user_name") or "You")
        .with_ttl(timedelta(minutes=minutes))
        .with_grants(api.VideoGrants(
            room_join=True,
            room=room,
            can_publish=True,        # microphone and camera
            can_subscribe=True,      # hear JARVIS
        ))
        .to_jwt()
    )
    return {"url": url, "token": token, "room": room, "identity": identity}


def new_pin() -> str:
    """A short code that has to be typed before a token is handed out.

    Six digits from a cryptographic source. The endpoint that checks it is rate
    limited, because six digits is only meaningful if guessing is slow.
    """
    return f"{secrets.randbelow(10**6):06d}"
