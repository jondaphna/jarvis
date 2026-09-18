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

import contextlib
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
FILENAME = "house_style.json"

#: What this file was called before it grew the reference slots. Read once, on
#: first run, so an existing install keeps its edits instead of silently
#: getting the shipped placeholder back.
LEGACY_FILENAME = "content_styles.json"

#: How many reference Reels the file has room for, and which of them were
#: called out as the ones that matter most.
REFERENCE_SLOTS = 6
TOP_PRIORITY_SLOTS = (1, 2)

#: The questions each reference slot asks. These are the fields, in order;
#: the value of asking them in this shape is that the answers are exactly what
#: a scriptwriter needs and nothing else.
REFERENCE_FIELDS: tuple[tuple[str, str], ...] = (
    ("url", "Link to the Reel."),
    ("first_two_seconds", "What is on screen and said in the first two "
                          "seconds, in order."),
    ("narration", "Voice or no voice. Whose. Scripted or talking. Accent, "
                  "pace, and whether it is a real person or synthetic."),
    ("on_screen_text", "How much text, where it sits, when it changes, and "
                       "whether it repeats the narration or adds to it."),
    ("visuals", "What the footage actually is: stock, screen recording, "
                "generated, filmed. How each shot relates to the line."),
    ("cutting", "How often it cuts, and what the rhythm does across the "
                "thirty seconds."),
    ("sound", "Music, its energy, and any sound effects that carry a beat."),
    ("subject", "What it is about, and who it is for."),
    ("why_it_works", "Why you saved this one. The bit you want copied."),
    ("copy_this", "The specific thing to reproduce in our scripts."),
    ("do_not_copy", "Anything in it that is theirs, or that you dislike."),
)

#: Fields that, once filled, mean a slot has been genuinely described rather
#: than merely pasted in. A URL on its own tells the scriptwriter nothing.
DESCRIBED_BY = ("first_two_seconds", "why_it_works", "copy_this")

#: The six Reels themselves, in the order they were given, so each slot is
#: already addressed to a specific video rather than being a blank form.
#:
#: The links are here and not in the prompt because nothing in this repository
#: can open them: instagram.com is refused by the network these builds run in,
#: and a model asked to follow a link it cannot fetch invents what it found.
#: They are here so that the person filling the form knows which Reel each
#: slot is asking about.
REFERENCE_URLS: tuple[str, ...] = (
    "https://www.instagram.com/reel/DaNoqI8uIb7/?stkn=MXBreTZ1aTB4OWxocg==",
    "https://www.instagram.com/reel/DdQ18D_xMXd/?stkn=dzd3NnljOHUwcTNq",
    "https://www.instagram.com/reel/DbyTPOVCQxL/?stkn=MWJ1bTJ0bHpjOHdzbA==",
    "https://www.instagram.com/reel/DdUhNARR032/?stkn=dDhhOWplOTQ5eW5o",
    "https://www.instagram.com/reel/DdWfb85E-IO/?stkn=dTJ4ZW1tMGZtcmI4",
    "https://www.instagram.com/reel/DZ1BSCyRtD0/?stkn=cG0xcG14bmtmYWZx",
)


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
        block = reference_block()
        if block:
            lines.append("")
            lines.append(block)
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


def legacy_path() -> Path:
    from jarvis import paths

    return paths.ROOT / LEGACY_FILENAME


def blank_references() -> list[dict[str, Any]]:
    """Six empty slots, numbered, with the question in each field's place.

    The fields ship holding their own question rather than an empty string.
    An empty form tells you nothing about what to write in it, and the one
    thing this file needs is for somebody to answer eleven specific questions
    six times. Anything still holding its question counts as unanswered.
    """
    slots = []
    for number in range(1, REFERENCE_SLOTS + 1):
        slot: dict[str, Any] = {
            "slot": number,
            "priority": "top" if number in TOP_PRIORITY_SLOTS else "normal",
        }
        for key, question in REFERENCE_FIELDS:
            slot[key] = f"({question})"
        slot["url"] = (REFERENCE_URLS[number - 1]
                       if number <= len(REFERENCE_URLS) else "")
        slots.append(slot)
    return slots


def _answered(value: Any) -> bool:
    """Has this field been filled in, as opposed to still holding its prompt?"""
    text = str(value or "").strip()
    return bool(text) and not (text.startswith("(") and text.endswith(")"))


def references() -> list[dict[str, Any]]:
    """The reference slots as they stand on disk."""
    try:
        raw = json.loads(path().read_text("utf-8"))
    except Exception:
        return blank_references()
    stored = raw.get("references") if isinstance(raw, dict) else None
    if not isinstance(stored, list):
        return blank_references()
    return [slot for slot in stored if isinstance(slot, dict)]


def described() -> list[dict[str, Any]]:
    """Only the slots somebody has actually answered."""
    return [slot for slot in references()
            if any(_answered(slot.get(key)) for key in DESCRIBED_BY)]


def reference_block() -> str:
    """The described references, written for the model that has to match them.

    Empty until at least one slot is filled in, which is the point: an empty
    form in the prompt would be six paragraphs of parentheses for the model to
    imitate.
    """
    filled = described()
    if not filled:
        return ""
    lines = ["# The reference Reels",
             "These are the Reels this account is modelled on, described by "
             "the person who chose them. Match what they do. Where two "
             "disagree, the ones marked top priority win."]
    for slot in filled:
        number = slot.get("slot", "?")
        priority = " (top priority)" if slot.get("priority") == "top" else ""
        lines.append("")
        lines.append(f"## Reference {number}{priority}")
        for key, _question in REFERENCE_FIELDS:
            if key == "url" or not _answered(slot.get(key)):
                continue
            label = key.replace("_", " ").capitalize()
            lines.append(f"- {label}: {str(slot[key]).strip()}")
    return "\n".join(lines)


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
    """One profile by name, or the default. Never raises.

    A profile stops being a placeholder the moment the reference slots are
    answered, without anyone having to remember to flip a flag. That matters
    because the flag is what the status tool and the doctor report, and a flag
    you have to set by hand is a flag that stays wrong.
    """
    profiles = load_all()
    wanted = (name or "").strip() or default_name()
    profile = profiles.get(wanted) or profiles.get(DEFAULT.name) or DEFAULT
    if profile.placeholder and described():
        profile = StyleProfile.from_dict({**profile.as_dict(),
                                          "placeholder": False}, profile.name)
    return profile


def write_starter_file(force: bool = False) -> Path:
    """Put an editable copy of the default on disk, so there is something to edit.

    Called once when the engine first runs. It never overwrites your edits: an
    existing file is left exactly as it is unless you ask for it back.

    What it writes is a form as much as a setting. The six reference slots
    carry their own questions, numbered, with the two called out as top
    priority already marked - so filling it in is answering questions rather
    than inventing a schema, and the answers are exactly what the scriptwriter
    is missing.
    """
    from jarvis import paths

    paths.ensure_dirs()
    target = path()
    if target.exists() and not force:
        return target

    profiles = {DEFAULT.name: DEFAULT.as_dict()}
    default = DEFAULT.name
    # An install from before the rename keeps its edits rather than being
    # quietly reset to the shipped guesswork.
    with contextlib.suppress(Exception):
        old = json.loads(legacy_path().read_text("utf-8"))
        if isinstance(old, dict):
            carried = old.get("profiles")
            if isinstance(carried, dict) and carried:
                profiles = carried
            default = str(old.get("default") or default) or default

    body = {
        "_readme": (
            "This is the house style for every Reel Jarvis writes. Fill in "
            "the six reference slots below - the two marked top priority "
            "first - replacing each question in brackets with your answer. "
            "Nothing needs restarting: the next script picks it up. Slots "
            "you leave as questions are ignored."),
        "default": default,
        "references": blank_references(),
        "profiles": profiles,
    }
    target.write_text(json.dumps(body, indent=2, ensure_ascii=False),
                      encoding="utf-8")
    return target
