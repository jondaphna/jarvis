"""What "our style" means, kept in a file you edit rather than in code.

The six reference Reels are the spec for this engine, and a spec that lives in
a Python string is a spec only a developer can change. So the style is a JSON
file in your JARVIS folder. Edit it, say "write me three scripts", and the next
script follows the new rules - no code change, no restart of anything but the
job itself.

The profile that ships here is a placeholder built from what high-retention
faceless Reels generally do. It is deliberately labelled as such: it was
written without access to your references, and it should be replaced by a
description of what those six actually do. Everything downstream reads the
profile, so replacing it is the whole edit.
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

#: The repo root, so `jarvis.paths` is importable from inside this package.
REPO = Path(__file__).resolve().parents[3]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

#: Where the editable profiles live. Sits beside settings.json, so backing up
#: the JARVIS folder backs up the house style too.
FILENAME = "content_styles.json"


@dataclass
class StyleProfile:
    """One house style: length, pacing, voice, and what never to do."""

    name: str = "reference"
    description: str = ""
    seconds: int = 30
    hook_seconds: float = 2.0
    beats: int = 6
    voiceover: str = ""
    on_screen: str = ""
    visuals: str = ""
    pacing: str = ""
    caption: str = ""
    hashtags: int = 8
    avoid: list[str] = field(default_factory=list)
    reference_urls: list[str] = field(default_factory=list)
    #: Free text appended to the prompt verbatim. This is where "do it like
    #: the second reel" goes once you have described the second reel.
    notes: str = ""
    #: True while nobody has replaced the shipped guesswork with the real
    #: thing. Surfaced by the status tool so it cannot be forgotten.
    placeholder: bool = False

    @classmethod
    def from_dict(cls, raw: dict[str, Any], name: str = "") -> StyleProfile:
        known = set(cls.__dataclass_fields__)
        data = {key: value for key, value in (raw or {}).items() if key in known}
        data.setdefault("name", name or "reference")
        profile = cls(**data)
        profile.seconds = max(5, min(180, int(profile.seconds or 30)))
        profile.beats = max(2, min(20, int(profile.beats or 6)))
        profile.hashtags = max(0, min(30, int(profile.hashtags or 8)))
        return profile

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def as_prompt_block(self) -> str:
        """The style, written for the model that has to follow it."""
        lines = [f"# House style: {self.name}"]
        if self.description:
            lines.append(self.description)
        lines.append("")
        lines.append(f"- Target length: about {self.seconds} seconds, "
                     f"{self.beats} beats after the hook.")
        lines.append(f"- The hook has to land inside {self.hook_seconds} seconds.")
        for label, value in (("Voiceover", self.voiceover),
                             ("On-screen text", self.on_screen),
                             ("Visuals", self.visuals),
                             ("Pacing", self.pacing),
                             ("Caption", self.caption)):
            if value:
                lines.append(f"- {label}: {value}")
        if self.hashtags:
            lines.append(f"- Hashtags: {self.hashtags}, mixed broad and niche.")
        if self.avoid:
            lines.append("- Never: " + "; ".join(self.avoid) + ".")
        if self.notes:
            lines.append("")
            lines.append(self.notes)
        return "\n".join(lines)


#: The shipped starting point. Every line here is a guess made without seeing
#: the reference Reels, which is why `placeholder` is true: the status tool
#: reports it, and describing the references is what turns it off.
DEFAULT = StyleProfile(
    name="reference",
    description=(
        "Fast, faceless, information-dense short-form video. One idea per "
        "Reel, delivered as if the viewer already scrolled past three others "
        "and is deciding in the first second whether to keep going."
    ),
    seconds=30,
    hook_seconds=2.0,
    beats=6,
    voiceover=(
        "One narrator, plain spoken, present tense, short sentences. No "
        "greeting, no 'in this video', no sign-off. Speak as if mid-thought."
    ),
    on_screen=(
        "Three to five words a beat, in the viewer's own words rather than "
        "the script's. It repeats the point rather than transcribing the line."
    ),
    visuals=(
        "A new shot on every beat. Concrete, specific, high-contrast imagery "
        "that illustrates the sentence rather than decorating it. Motion in "
        "every shot, because a still frame is where people scroll."
    ),
    pacing=(
        "Nothing on screen for more than three seconds. The tension set up by "
        "the hook is not resolved until the last beat."
    ),
    caption=(
        "One or two lines that restate the hook for someone who watched with "
        "the sound off, then the call to action."
    ),
    hashtags=8,
    avoid=[
        "asking people to like and subscribe in the first five seconds",
        "stock-footage clichés: handshakes, rising graphs, typing hands",
        "claims of fact that are not in the brief",
        "anything that reads as written by a machine",
    ],
    reference_urls=[],
    notes=(
        "This profile is a placeholder. It was written without access to the "
        "reference Reels, so replace these lines with what those actually do: "
        "what happens in the first two seconds, whether there is a narrator "
        "or only text and music, the cutting rhythm, and the subject matter."
    ),
    placeholder=True,
)


# --------------------------------------------------------------------------- #
# Reading and writing the file
# --------------------------------------------------------------------------- #

def path() -> Path:
    from jarvis import paths

    return paths.ROOT / FILENAME


def load_all() -> dict[str, StyleProfile]:
    """Every profile on disk, falling back to the shipped one.

    A broken or missing file is not an error worth stopping for: the engine
    keeps working on the default, and the status tool says the file could not
    be read.
    """
    profiles: dict[str, StyleProfile] = {DEFAULT.name: DEFAULT}
    try:
        raw = json.loads(path().read_text("utf-8"))
    except Exception:
        return profiles

    stored = raw.get("profiles") if isinstance(raw, dict) else None
    if not isinstance(stored, dict):
        return profiles
    for name, body in stored.items():
        if isinstance(body, dict):
            try:
                profiles[str(name)] = StyleProfile.from_dict(body, name=str(name))
            except Exception:
                continue
    return profiles


def default_name() -> str:
    """Which profile a job uses when it doesn't name one."""
    try:
        raw = json.loads(path().read_text("utf-8"))
        chosen = str((raw or {}).get("default") or "").strip()
    except Exception:
        chosen = ""
    return chosen or DEFAULT.name


def get(name: str = "") -> StyleProfile:
    """One profile by name, or the default. Never raises."""
    profiles = load_all()
    wanted = (name or "").strip() or default_name()
    return profiles.get(wanted) or profiles.get(DEFAULT.name) or DEFAULT


def write_starter_file(force: bool = False) -> Path:
    """Put an editable copy of the default on disk, so there is something to edit.

    Called once when the engine first runs. It never overwrites your edits:
    an existing file is left exactly as it is unless you ask for it back.
    """
    from jarvis import paths

    paths.ensure_dirs()
    target = path()
    if target.exists() and not force:
        return target
    body = {"default": DEFAULT.name, "profiles": {DEFAULT.name: DEFAULT.as_dict()}}
    target.write_text(json.dumps(body, indent=2, ensure_ascii=False),
                      encoding="utf-8")
    return target
