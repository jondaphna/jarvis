"""Custom commands - your own rules, remembered forever.

Three kinds, because "a command" turns out to mean three different things:

  reply     Say these exact words. Answered locally - no model call, so it is
            instant and free. "When I say hey Jarvis, say: Yes sir, how can I
            help you."
  instruct  A standing instruction added to every conversation. "Always call me
            sir." "Answer in Hebrew unless I ask otherwise."
  run       A shorthand that expands into a fuller request. "Movie night" ->
            "dim the lights, open Netflix on the TV and put my phone on silent."

Rules can be scoped to one person, so your rules and your brother's don't
collide. You can add them by talking ("from now on, when I say X, say Y") or
from the terminal, and they survive restarts.
"""

from __future__ import annotations

import json
import re
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .events import log

KIND_REPLY = "reply"
KIND_INSTRUCT = "instruct"
KIND_RUN = "run"
KINDS = (KIND_REPLY, KIND_INSTRUCT, KIND_RUN)

MATCH_CONTAINS = "contains"
MATCH_EXACT = "exact"
MATCH_STARTS = "starts"
MATCHES = (MATCH_CONTAINS, MATCH_EXACT, MATCH_STARTS)

ANYONE = "*"


@dataclass
class Rule:
    trigger: str = ""
    response: str = ""
    kind: str = KIND_REPLY
    match: str = MATCH_CONTAINS
    #: Person this applies to, by name, or "*" for everyone.
    scope: str = ANYONE
    enabled: bool = True
    #: Higher wins when several rules match. Exact beats contains by default.
    priority: int = 0
    uses: int = 0
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))

    def __post_init__(self) -> None:
        self.kind = str(self.kind).lower().strip()
        if self.kind not in KINDS:
            raise ValueError(f"Rule kind must be one of {', '.join(KINDS)}.")
        self.match = str(self.match).lower().strip()
        if self.match not in MATCHES:
            raise ValueError(f"Rule match must be one of {', '.join(MATCHES)}.")
        if self.kind != KIND_INSTRUCT and not self.trigger.strip():
            raise ValueError("A rule needs a trigger phrase.")
        if not self.response.strip():
            raise ValueError("A rule needs something to say or do.")

    # ------------------------------------------------------------------ #

    def applies_to(self, person: str | None) -> bool:
        if self.scope == ANYONE:
            return True
        return (person or "").strip().lower() == self.scope.strip().lower()

    def matches(self, text: str) -> bool:
        if not self.enabled or self.kind == KIND_INSTRUCT:
            return False
        spoken = _normalise(text)
        trigger = _normalise(self.trigger)
        if not trigger:
            return False
        if self.match == MATCH_EXACT:
            return spoken == trigger
        if self.match == MATCH_STARTS:
            return spoken.startswith(trigger)
        return trigger in spoken

    def remainder(self, text: str) -> str:
        """What's left of the utterance after the trigger - for `run` rules."""
        spoken = _normalise(text)
        trigger = _normalise(self.trigger)
        index = spoken.find(trigger)
        if index == -1:
            return ""
        return text[index + len(trigger):].strip(" ,.!?-:;")

    def rank(self) -> tuple[int, int, int]:
        """Sort key: priority, then specificity, then trigger length."""
        specificity = {MATCH_EXACT: 2, MATCH_STARTS: 1, MATCH_CONTAINS: 0}[self.match]
        return (self.priority, specificity, len(self.trigger))

    def describe(self) -> str:
        who = "" if self.scope == ANYONE else f" [{self.scope} only]"
        state = "" if self.enabled else " (off)"
        if self.kind == KIND_INSTRUCT:
            return f"always: {self.response}{who}{state}"
        verb = "say" if self.kind == KIND_REPLY else "do"
        return f'"{self.trigger}" -> {verb}: {self.response}{who}{state}'

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "trigger": self.trigger, "response": self.response,
            "kind": self.kind, "match": self.match, "scope": self.scope,
            "enabled": self.enabled, "priority": self.priority,
            "uses": self.uses, "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Rule":
        return cls(
            trigger=raw.get("trigger", ""),
            response=raw.get("response", ""),
            kind=raw.get("kind", KIND_REPLY),
            match=raw.get("match", MATCH_CONTAINS),
            scope=raw.get("scope", ANYONE),
            enabled=bool(raw.get("enabled", True)),
            priority=int(raw.get("priority", 0)),
            uses=int(raw.get("uses", 0)),
            id=raw.get("id", uuid.uuid4().hex[:8]),
            created_at=raw.get("created_at", ""),
        )


def _normalise(text: str) -> str:
    """Lowercase, drop punctuation, collapse whitespace.

    The collapse matters: "Hey, Jarvis!" becomes "hey  jarvis" with a double
    space once the comma goes, which would never match the stored "hey jarvis".
    """
    stripped = re.sub(r"[^\w\s]", " ", (text or "").lower())
    return re.sub(r"\s+", " ", stripped).strip()


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #

class RuleBook:
    """Every custom command, persisted."""

    def __init__(self, settings, path: Path | None = None) -> None:
        from .. import paths

        self.settings = settings
        self.path = path or (paths.ROOT / "commands.json")
        self._rules: list[Rule] = []
        self._lock = threading.RLock()
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text("utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("couldn't read the commands file: %s", exc)
            return
        for entry in raw.get("rules", []):
            try:
                self._rules.append(Rule.from_dict(entry))
            except (ValueError, TypeError) as exc:
                log.warning("skipping a malformed rule: %s", exc)

    def _save(self) -> None:
        from .. import paths

        paths.ensure_dirs()
        self.path.write_text(
            json.dumps({"rules": [r.to_dict() for r in self._rules]}, indent=2),
            encoding="utf-8")

    # ------------------------------------------------------------------ #
    # Managing rules
    # ------------------------------------------------------------------ #

    def all(self, person: str | None = None) -> list[Rule]:
        rules = list(self._rules)
        if person is not None:
            rules = [r for r in rules if r.applies_to(person)]
        return sorted(rules, key=lambda r: (-r.rank()[0], r.kind, r.trigger.lower()))

    def add(self, rule: Rule) -> Rule:
        with self._lock:
            self._rules.append(rule)
            self._save()
        log.info("command added: %s", rule.describe())
        return rule

    def create(self, trigger: str, response: str, kind: str = KIND_REPLY,
               **options: Any) -> Rule:
        return self.add(Rule(trigger=trigger, response=response, kind=kind,
                             **options))

    def get(self, rule_id: str) -> Rule | None:
        for rule in self._rules:
            if rule.id == rule_id:
                return rule
        return None

    def remove(self, rule_id: str) -> bool:
        with self._lock:
            before = len(self._rules)
            self._rules = [r for r in self._rules if r.id != rule_id]
            changed = len(self._rules) < before
            if changed:
                self._save()
            return changed

    def set_enabled(self, rule_id: str, enabled: bool) -> bool:
        rule = self.get(rule_id)
        if rule is None:
            return False
        with self._lock:
            rule.enabled = enabled
            self._save()
        return True

    def clear(self) -> int:
        with self._lock:
            count = len(self._rules)
            self._rules.clear()
            self._save()
            return count

    # ------------------------------------------------------------------ #
    # Using rules
    # ------------------------------------------------------------------ #

    def find(self, text: str, person: str | None = None) -> Rule | None:
        """The best rule triggered by this utterance, if any."""
        candidates = [r for r in self._rules
                      if r.applies_to(person) and r.matches(text)]
        if not candidates:
            return None
        # A rule naming the speaker beats a general one at the same rank.
        return max(candidates,
                   key=lambda r: (r.rank(), r.scope != ANYONE))

    def consume(self, rule: Rule) -> None:
        with self._lock:
            rule.uses += 1
            self._save()

    def instructions(self, person: str | None = None) -> list[str]:
        """Standing instructions for the system prompt."""
        return [r.response for r in self._rules
                if r.enabled and r.kind == KIND_INSTRUCT and r.applies_to(person)]

    def instruction_block(self, person: str | None = None) -> str:
        lines = self.instructions(person)
        if not lines:
            return ""
        return ("Standing instructions from the user - follow these every time:\n"
                + "\n".join(f"- {line}" for line in lines))

    def summary(self) -> str:
        if not self._rules:
            return "no custom commands yet"
        return f"{len(self._rules)} command(s)"


# --------------------------------------------------------------------------- #
# "From now on, when I say X, say Y"
# --------------------------------------------------------------------------- #

_QUOTES = r"[\"'“”‘’]"

_WHEN_SAY = re.compile(
    rf"""(?:^|\b)(?:from\s+now\s+on\s*,?\s*)?
    (?:every\s+time|whenever|when|if)\s+i\s+say\s*
    {_QUOTES}?(?P<trigger>[^,\"'“”]{{1,80}}?){_QUOTES}?\s*
    [,:]?\s*
    (?:i\s+want\s+you\s+to\s+|you\s+should\s+|please\s+)?
    (?P<verb>say|reply|respond|answer|do|run)\s*
    (?:with\s+|that\s+|this\s*:\s*)?
    {_QUOTES}?(?P<response>.+?){_QUOTES}?\s*$""",
    re.IGNORECASE | re.VERBOSE,
)

_ALWAYS = re.compile(
    r"""(?:^|\b)(?:from\s+now\s+on|always|never|remember\s+to)\s*,?\s*
    (?P<instruction>.{4,200}?)\s*$""",
    re.IGNORECASE | re.VERBOSE,
)

#: "from now on when I say..." is a trigger rule, not a standing instruction.
_LOOKS_LIKE_TRIGGER = re.compile(r"\b(?:when|whenever|every\s+time|if)\s+i\s+say\b",
                                 re.IGNORECASE)


def parse_rule_command(text: str) -> Rule | None:
    """Turn a spoken instruction into a Rule, or None if it isn't one.

    Only ever called on the user's own words - the same boundary the permission
    grants use. Nothing JARVIS reads can define its own commands.
    """
    if not text or not text.strip():
        return None

    match = _WHEN_SAY.search(text)
    if match is not None:
        trigger = match.group("trigger").strip(" ,.!?-:;")
        response = match.group("response").strip(" ,.!?-:;")
        verb = match.group("verb").lower()
        if not trigger or not response:
            return None
        kind = KIND_RUN if verb in ("do", "run") else KIND_REPLY
        try:
            return Rule(trigger=trigger, response=response, kind=kind)
        except ValueError:
            return None

    if not _LOOKS_LIKE_TRIGGER.search(text):
        always = _ALWAYS.search(text)
        if always is not None:
            instruction = always.group("instruction").strip(" ,.!?-:;")
            leading = text.strip().lower()
            if leading.startswith("never"):
                instruction = f"never {instruction}"
            elif leading.startswith("always"):
                instruction = f"always {instruction}"
            if len(instruction.split()) < 2:
                return None
            try:
                return Rule(trigger="", response=instruction, kind=KIND_INSTRUCT)
            except ValueError:
                return None

    return None
