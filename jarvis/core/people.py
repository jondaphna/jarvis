"""Who is talking, and what they're allowed to ask for.

JARVIS can know several people apart - by voice, and by face when a camera is
available - and give each of them a different level of authority. That's the
difference between "answer anyone in the room" and "chat to my brother, but only
*I* can touch the machine".

Four levels:

  owner    - it's you. Everything, still subject to the normal permission system.
  trusted  - can ask for things and have them read, but nothing that changes the
             machine or the outside world.
  guest    - conversation only. No tools at all.
  blocked  - ignored entirely.

Identity is a convenience, not a credential. A recording of your voice or a photo
of your face can fool it, so it never *grants* anything on its own: it only ever
narrows what the permission system would already have allowed. Spending money,
publishing and messaging still need your explicit authorisation in the request,
exactly as before.

Privacy: voice embeddings and face encodings are computed and stored on this
machine only, as numbers. No audio or images are kept, and nothing is uploaded.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import IntEnum
from pathlib import Path
from typing import Any, Sequence

from .events import log
from .speaker_id import cosine_similarity

#: How close a voice or face must be to count as a match.
VOICE_THRESHOLD = 0.70
FACE_THRESHOLD = 0.62          # face encodings sit closer together than voices


class Authority(IntEnum):
    BLOCKED = 0
    GUEST = 1
    TRUSTED = 2
    OWNER = 3

    @property
    def label(self) -> str:
        return self.name.lower()

    @classmethod
    def parse(cls, value: Any) -> "Authority":
        if isinstance(value, Authority):
            return value
        if isinstance(value, int):
            return cls(max(0, min(3, value)))
        text = str(value or "").strip().lower()
        for level in cls:
            if level.name.lower() == text:
                return level
        raise ValueError(
            f"Unknown authority {value!r}. Use: owner, trusted, guest or blocked.")

    def describe(self) -> str:
        return {
            Authority.OWNER: "full control (still subject to permissions)",
            Authority.TRUSTED: "can ask and be answered, but can't change anything",
            Authority.GUEST: "conversation only, no tools",
            Authority.BLOCKED: "ignored",
        }[self]


@dataclass
class Person:
    name: str
    authority: Authority = Authority.GUEST
    voice: list[list[float]] = field(default_factory=list)
    face: list[list[float]] = field(default_factory=list)
    note: str = ""
    created_at: str = ""

    @property
    def key(self) -> str:
        return self.name.strip().lower()

    @property
    def knows_voice(self) -> bool:
        return bool(self.voice)

    @property
    def knows_face(self) -> bool:
        return bool(self.face)

    def best_voice_score(self, embedding: Sequence[float]) -> float:
        return max((cosine_similarity(embedding, known) for known in self.voice),
                   default=0.0)

    def best_face_score(self, encoding: Sequence[float]) -> float:
        return max((cosine_similarity(encoding, known) for known in self.face),
                   default=0.0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "authority": self.authority.label,
            "voice": self.voice,
            "face": self.face,
            "note": self.note,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Person":
        return cls(
            name=raw.get("name", "unknown"),
            authority=Authority.parse(raw.get("authority", "guest")),
            voice=[list(map(float, v)) for v in raw.get("voice", [])],
            face=[list(map(float, f)) for f in raw.get("face", [])],
            note=raw.get("note", ""),
            created_at=raw.get("created_at", ""),
        )

    def describe(self) -> str:
        traits = []
        if self.knows_voice:
            traits.append(f"{len(self.voice)} voice sample(s)")
        if self.knows_face:
            traits.append(f"{len(self.face)} face sample(s)")
        return (f"{self.name} - {self.authority.label} "
                f"({', '.join(traits) or 'nothing enrolled'})")


@dataclass
class Identification:
    person: Person | None
    score: float = 0.0
    method: str = ""

    @property
    def known(self) -> bool:
        return self.person is not None

    @property
    def authority(self) -> Authority:
        return self.person.authority if self.person else Authority.GUEST

    @property
    def name(self) -> str:
        return self.person.name if self.person else "someone I don't know"


class PeopleRegistry:
    """Everyone JARVIS can recognise, and how much each of them is trusted."""

    def __init__(self, settings, path: Path | None = None) -> None:
        from .. import paths

        self.settings = settings
        self.path = path or (paths.ROOT / "people.json")
        self._people: dict[str, Person] = {}
        self._lock = threading.RLock()
        self._load()

    # ------------------------------------------------------------------ #
    # Storage
    # ------------------------------------------------------------------ #

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text("utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("couldn't read the people file: %s", exc)
            return
        for entry in raw.get("people", []):
            try:
                person = Person.from_dict(entry)
            except (ValueError, TypeError) as exc:
                log.warning("skipping a malformed person entry: %s", exc)
                continue
            self._people[person.key] = person

    def _save(self) -> None:
        from .. import paths

        paths.ensure_dirs()
        payload = {"people": [p.to_dict() for p in self._people.values()]}
        self.path.write_text(json.dumps(payload), encoding="utf-8")
        try:
            import sys
            if sys.platform != "win32":
                self.path.chmod(0o600)
        except OSError:
            pass

    # ------------------------------------------------------------------ #
    # Managing people
    # ------------------------------------------------------------------ #

    def all(self) -> list[Person]:
        return sorted(self._people.values(),
                      key=lambda p: (-int(p.authority), p.name.lower()))

    def get(self, name: str) -> Person | None:
        return self._people.get(str(name).strip().lower())

    def owner(self) -> Person | None:
        for person in self._people.values():
            if person.authority is Authority.OWNER:
                return person
        return None

    def add(self, name: str, authority: Any = Authority.GUEST,
            note: str = "") -> Person:
        level = Authority.parse(authority)
        with self._lock:
            person = self._people.get(str(name).strip().lower())
            if person is None:
                person = Person(
                    name=name.strip(),
                    authority=level,
                    created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    note=note,
                )
                self._people[person.key] = person
            else:
                person.authority = level
                if note:
                    person.note = note
            self._save()
            return person

    def set_authority(self, name: str, authority: Any) -> Person:
        person = self.get(name)
        if person is None:
            raise KeyError(f"I don't know anyone called {name!r}.")
        with self._lock:
            person.authority = Authority.parse(authority)
            self._save()
        return person

    def remove(self, name: str) -> bool:
        with self._lock:
            removed = self._people.pop(str(name).strip().lower(), None) is not None
            if removed:
                self._save()
            return removed

    def clear(self) -> int:
        with self._lock:
            count = len(self._people)
            self._people.clear()
            self._save()
            return count

    # ------------------------------------------------------------------ #
    # Enrolment
    # ------------------------------------------------------------------ #

    def add_voice(self, name: str, embeddings: Sequence[Sequence[float]],
                  authority: Any | None = None) -> Person:
        person = self.get(name) or self.add(
            name, authority if authority is not None else Authority.GUEST)
        if authority is not None:
            person.authority = Authority.parse(authority)
        with self._lock:
            person.voice.extend([list(map(float, e)) for e in embeddings])
            self._save()
        log.info("enrolled %d voice sample(s) for %s", len(embeddings), person.name)
        return person

    def add_face(self, name: str, encodings: Sequence[Sequence[float]],
                 authority: Any | None = None) -> Person:
        person = self.get(name) or self.add(
            name, authority if authority is not None else Authority.GUEST)
        if authority is not None:
            person.authority = Authority.parse(authority)
        with self._lock:
            person.face.extend([list(map(float, e)) for e in encodings])
            self._save()
        log.info("enrolled %d face sample(s) for %s", len(encodings), person.name)
        return person

    # ------------------------------------------------------------------ #
    # Recognition
    # ------------------------------------------------------------------ #

    def identify_voice(self, embedding: Sequence[float],
                       threshold: float | None = None) -> Identification:
        cutoff = self._threshold("voice", threshold, VOICE_THRESHOLD)
        best, best_score = None, 0.0
        for person in self._people.values():
            if not person.knows_voice:
                continue
            score = person.best_voice_score(embedding)
            if score > best_score:
                best, best_score = person, score
        if best is not None and best_score >= cutoff:
            return Identification(best, best_score, "voice")
        return Identification(None, best_score, "voice")

    def identify_face(self, encoding: Sequence[float],
                      threshold: float | None = None) -> Identification:
        cutoff = self._threshold("face", threshold, FACE_THRESHOLD)
        best, best_score = None, 0.0
        for person in self._people.values():
            if not person.knows_face:
                continue
            score = person.best_face_score(encoding)
            if score > best_score:
                best, best_score = person, score
        if best is not None and best_score >= cutoff:
            return Identification(best, best_score, "face")
        return Identification(None, best_score, "face")

    def _threshold(self, kind: str, override: float | None, default: float) -> float:
        if override is not None:
            return override
        configured = self.settings.get(f"people.{kind}_threshold")
        try:
            return float(configured) if configured is not None else default
        except (TypeError, ValueError):
            return default

    # ------------------------------------------------------------------ #
    # Policy
    # ------------------------------------------------------------------ #

    @property
    def active(self) -> bool:
        """Recognition only applies once somebody has actually been enrolled."""
        return any(p.knows_voice or p.knows_face for p in self._people.values())

    def authority_for(self, identification: Identification) -> Authority:
        """What an unrecognised voice gets - configurable, guest by default."""
        if identification.known:
            return identification.authority
        try:
            return Authority.parse(
                self.settings.get("people.unknown_authority", "guest"))
        except ValueError:
            return Authority.GUEST

    def summary(self) -> str:
        if not self._people:
            return "nobody enrolled"
        return "; ".join(person.describe() for person in self.all())
