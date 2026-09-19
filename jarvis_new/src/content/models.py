"""A Reel script as an object, because the next stage has to read it.

A script that comes back as prose is fine for a human and useless for a
pipeline: the voiceover stage needs the narration alone, the visual stage
needs one prompt per shot, the publisher needs the caption and the hashtags
separately, and the renderer needs to know when each line lands. So the model
is asked for structure, and structure is what is stored.

Everything here survives a round trip through JSON, because that is how it is
written to the database and read back by a stage that may run an hour later in
a different process.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

#: Instagram rejects a caption over this, and truncates the visible part long
#: before it. Enforced rather than hoped for.
CAPTION_LIMIT = 2200

#: More than this reads as spam to both the algorithm and a human.
MAX_HASHTAGS = 12


def _text(value: Any, fallback: str = "") -> str:
    if isinstance(value, str):
        return value.strip()
    if value is None:
        return fallback
    return str(value).strip()


@dataclass
class Beat:
    """One shot: what is heard, what is seen, and what is written on it."""

    #: Seconds from the start of the Reel.
    at: float = 0.0
    #: The narration for this beat. Concatenated in order, this is the whole
    #: voiceover, which is exactly what the TTS stage is handed.
    voiceover: str = ""
    #: The words burned onto the screen. Short - this is read at a glance.
    on_screen: str = ""
    #: A prompt for the image or clip behind it, written for an image model.
    visual: str = ""

    @classmethod
    def from_dict(cls, raw: Any) -> Beat:
        if not isinstance(raw, dict):
            return cls(voiceover=_text(raw))
        try:
            at = float(raw.get("at", raw.get("second", 0)) or 0)
        except (TypeError, ValueError):
            at = 0.0
        return cls(
            at=round(max(0.0, at), 1),
            voiceover=_text(raw.get("voiceover") or raw.get("narration")),
            on_screen=_text(raw.get("on_screen") or raw.get("text")),
            visual=_text(raw.get("visual") or raw.get("visual_prompt")),
        )

    def as_dict(self) -> dict[str, Any]:
        return {"at": self.at, "voiceover": self.voiceover,
                "on_screen": self.on_screen, "visual": self.visual}


@dataclass
class ReelScript:
    """One finished script, ready for the voiceover and visual stages."""

    topic: str = ""
    style: str = ""
    #: The first line. It is its own field because it is the only line that
    #: decides whether the other twenty are ever heard.
    hook: str = ""
    title: str = ""
    beats: list[Beat] = field(default_factory=list)
    caption: str = ""
    hashtags: list[str] = field(default_factory=list)
    call_to_action: str = ""
    seconds: int = 0

    # -- derived ---------------------------------------------------------- #

    def voiceover_text(self) -> str:
        """The whole narration, in order - what the TTS stage speaks."""
        lines = [self.hook] + [beat.voiceover for beat in self.beats]
        return " ".join(line.strip() for line in lines if line.strip())

    def word_count(self) -> int:
        return len(re.findall(r"\w+", self.voiceover_text()))

    def estimated_seconds(self) -> float:
        """Roughly how long the narration runs at a normal Reel pace.

        Around 2.8 words a second: faster than conversation, which is how
        short-form narration is actually delivered, and the number that makes
        a 30 second target come out at 30 seconds rather than 45.
        """
        return round(self.word_count() / 2.8, 1)

    def full_caption(self) -> str:
        """Caption, call to action and hashtags, in the shape Instagram wants."""
        parts = [self.caption.strip()]
        if self.call_to_action.strip():
            parts.append(self.call_to_action.strip())
        if self.hashtags:
            parts.append(" ".join(self.hashtags))
        joined = "\n\n".join(part for part in parts if part)
        return joined[:CAPTION_LIMIT]

    # -- serialisation ---------------------------------------------------- #

    @classmethod
    def from_dict(cls, raw: dict[str, Any], topic: str = "",
                  style: str = "") -> ReelScript:
        # Only a real sequence counts. A string is iterable, so a model that
        # answers "beats": "three quick beats" would otherwise be read one
        # character at a time and produce a script of twenty empty shots that
        # passes every other check.
        raw_beats = raw.get("beats")
        raw_beats = raw_beats if isinstance(raw_beats, (list, tuple)) else []
        beats = [Beat.from_dict(item) for item in raw_beats]
        beats = [beat for beat in beats
                 if beat.voiceover or beat.on_screen or beat.visual]
        return cls(
            topic=_text(raw.get("topic"), topic) or topic,
            style=_text(raw.get("style"), style) or style,
            hook=_text(raw.get("hook")),
            title=_text(raw.get("title")),
            beats=beats,
            caption=_text(raw.get("caption")),
            hashtags=normalise_hashtags(raw.get("hashtags")),
            call_to_action=_text(raw.get("call_to_action") or raw.get("cta")),
            seconds=int(raw.get("seconds") or 0),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "topic": self.topic,
            "style": self.style,
            "title": self.title,
            "hook": self.hook,
            "beats": [beat.as_dict() for beat in self.beats],
            "caption": self.caption,
            "hashtags": list(self.hashtags),
            "call_to_action": self.call_to_action,
            "seconds": self.seconds,
            "estimated_seconds": self.estimated_seconds(),
            "words": self.word_count(),
        }

    def as_json(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False, indent=2)

    def problems(self) -> list[str]:
        """What is wrong with this script, in the words a person would use.

        Used to decide whether to ask the model again. A script with no hook
        or no beats is not a near miss - it is a failed generation, and
        publishing it would be worse than producing nothing.
        """
        issues: list[str] = []
        if not self.hook:
            issues.append("no hook")
        if len(self.beats) < 2:
            issues.append("fewer than two beats")
        if not self.caption:
            issues.append("no caption")
        if not any(beat.visual for beat in self.beats):
            issues.append("no visual directions")
        return issues

    def is_usable(self) -> bool:
        return not self.problems()


def normalise_hashtags(raw: Any) -> list[str]:
    """Whatever the model returned, as a clean, capped, de-duplicated list."""
    if isinstance(raw, str):
        items = re.split(r"[,\s]+", raw)
    elif isinstance(raw, (list, tuple)):
        items = [str(item) for item in raw]
    else:
        return []

    seen: list[str] = []
    for item in items:
        tag = re.sub(r"[^0-9A-Za-z_]", "", str(item))
        if not tag:
            continue
        tag = f"#{tag.lower()}"
        if tag not in seen:
            seen.append(tag)
    return seen[:MAX_HASHTAGS]
