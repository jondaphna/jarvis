"""Realtime voice: LiveKit + a speech-to-speech model.

This is the answer to the two problems a hand-rolled Python voice loop cannot
really solve:

* **It hears itself.** Your microphone picks up your speakers. Telling the two
  apart needs acoustic echo cancellation running on the audio itself. WebRTC
  does that in the browser, for free, properly.
* **Interrupting is unreliable.** Knowing whether you have finished a sentence -
  or are just pausing mid-thought - needs a model trained on the audio, not an
  energy threshold. LiveKit ships one.

It also removes the Windows audio-device problem entirely: sound goes to your
browser or phone over WebRTC, so there is no output device to choose wrongly.

Everything JARVIS already knows how to do comes along - the same tools, the same
permission system, the same missions, commands and people.
"""

from __future__ import annotations

__all__ = ["available", "why_unavailable"]


def available() -> bool:
    """True when the realtime stack can actually start."""
    return not why_unavailable()


def why_unavailable() -> str:
    """Plain-language description of what's missing, or "" when ready."""
    try:
        import livekit.agents  # noqa: F401  (probe only)
    except ImportError:
        return ("The realtime stack isn't installed. Run:\n"
                "  pip install \"livekit-agents[google]\" livekit-api")
    try:
        from livekit.plugins import google  # noqa: F401  (probe only)
    except ImportError:
        return ("The Gemini realtime plugin is missing. Run:\n"
                "  pip install \"livekit-agents[google]\"")
    return ""
