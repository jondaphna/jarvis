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
    return str(url), str(key), str(secret)


def mint(config, identity: str = "you", room: str = DEFAULT_ROOM,
         minutes: int = 120) -> dict[str, str]:
    """A join token for one participant."""
    from livekit import api

    url, key, secret = credentials(config)
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
