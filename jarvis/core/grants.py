"""Turning "you have permission to buy this, once" into a machine-checkable grant.

This module parses authorisation out of *the user's own words*. It is the only
route by which a HIGH-risk capability (spending money, publishing, emailing)
becomes permitted.

SECURITY RULE, non-negotiable: only ever feed this function text the user
authored - a spoken command, a typed message, or a mission file the user wrote.
Never feed it web pages, email bodies, model output, or anything else JARVIS
merely *read*, or a hostile page could authorise its own purchase.
"""

from __future__ import annotations

import math
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from fnmatch import fnmatch
from typing import Iterable

# --------------------------------------------------------------------------- #
# Capability vocabulary
# --------------------------------------------------------------------------- #

def valid_amount(amount: object) -> bool:
    """Is this a number a budget can be checked against?

    `None` is not: a spend with no stated price cannot be shown to fit under a
    cap, so it must not match one. Nor is a bool (which is an `int` in Python
    and would arrive as 0 or 1), a negative (which would *credit* the budget),
    or a NaN or infinity (which compare in ways that quietly defeat the check).
    """
    return (isinstance(amount, (int, float)) and not isinstance(amount, bool)
            and math.isfinite(amount) and amount >= 0)


CAP_FS_READ = "fs.read"
CAP_FS_WRITE = "fs.write"
CAP_FS_DELETE = "fs.delete"
CAP_APP_LAUNCH = "app.launch"
CAP_INPUT_CONTROL = "input.control"
CAP_SCREEN_CAPTURE = "screen.capture"
CAP_SHELL_EXEC = "shell.exec"
CAP_WEB_SEARCH = "web.search"
CAP_WEB_READ = "web.read"
CAP_WEB_PUBLISH = "web.publish"
CAP_COMMS_SEND = "comms.send"
CAP_PAYMENT_SPEND = "payment.spend"
CAP_SYSTEM_CONFIG = "system.config"
CAP_LLM_CALL = "llm.call"

ALL_CAPABILITIES = (
    CAP_FS_READ, CAP_FS_WRITE, CAP_FS_DELETE, CAP_APP_LAUNCH, CAP_INPUT_CONTROL,
    CAP_SCREEN_CAPTURE, CAP_SHELL_EXEC, CAP_WEB_SEARCH, CAP_WEB_READ,
    CAP_WEB_PUBLISH, CAP_COMMS_SEND, CAP_PAYMENT_SPEND, CAP_SYSTEM_CONFIG,
    CAP_LLM_CALL,
)

#: Capabilities that are blocked outright unless the user grants them in the
#: command that starts the work. Approval can never be implied.
HIGH_RISK_CAPABILITIES = frozenset({
    CAP_PAYMENT_SPEND, CAP_WEB_PUBLISH, CAP_COMMS_SEND, CAP_SYSTEM_CONFIG,
})

CAPABILITY_HELP = {
    CAP_FS_READ: "read a file",
    CAP_FS_WRITE: "create or modify a file",
    CAP_FS_DELETE: "delete a file",
    CAP_APP_LAUNCH: "open an application",
    CAP_INPUT_CONTROL: "control the keyboard or mouse",
    CAP_SCREEN_CAPTURE: "take a screenshot",
    CAP_SHELL_EXEC: "run a terminal command",
    CAP_WEB_SEARCH: "search the web",
    CAP_WEB_READ: "read a web page",
    CAP_WEB_PUBLISH: "post or upload something publicly",
    CAP_COMMS_SEND: "send a message or email",
    CAP_PAYMENT_SPEND: "spend money",
    CAP_SYSTEM_CONFIG: "change system or JARVIS configuration",
    CAP_LLM_CALL: "think",
}


# --------------------------------------------------------------------------- #
# Grant
# --------------------------------------------------------------------------- #

@dataclass
class Grant:
    """A specific, bounded authorisation the user handed out."""

    capabilities: tuple[str, ...]
    resources: tuple[str, ...] = ("*",)
    uses: int | None = 1                    # None = unlimited within its window
    used: int = 0
    expires_at: datetime | None = None
    max_amount_usd: float | None = None
    spent_usd: float = 0.0
    reason: str = ""
    source: str = "user"
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # -- state ----------------------------------------------------------- #

    @property
    def expired(self) -> bool:
        if self.expires_at and datetime.now(timezone.utc) >= self.expires_at:
            return True
        return self.uses is not None and self.used >= self.uses

    @property
    def uses_left(self) -> str:
        if self.uses is None:
            return "unlimited"
        return str(max(0, self.uses - self.used))

    # -- matching -------------------------------------------------------- #

    def covers(self, capability: str, resource: str | None = None,
               amount_usd: float | None = None) -> bool:
        if self.expired:
            return False
        if not any(_cap_match(pattern, capability) for pattern in self.capabilities):
            return False
        if resource is not None and self.resources != ("*",):
            target = str(resource).lower()
            if not any(fnmatch(target, pattern.lower()) for pattern in self.resources):
                return False
        if self.max_amount_usd is not None:
            # A capped grant is only satisfied by a *known, sane* amount. It
            # used to check the cap only when an amount was supplied, so a
            # request that named no price matched a grant with a price limit -
            # the one case the limit exists for. `valid_amount` also rejects
            # negatives, which `consume` would otherwise subtract from the
            # running total, and non-finite values, which pass every
            # comparison without meaning anything.
            if not valid_amount(amount_usd):
                return False
            if self.spent_usd + amount_usd > self.max_amount_usd + 1e-9:
                return False
        elif amount_usd is not None and not valid_amount(amount_usd):
            return False
        return True

    def consume(self, amount_usd: float | None = None) -> None:
        self.used += 1
        if valid_amount(amount_usd) and amount_usd > 0:
            self.spent_usd += amount_usd

    # -- display --------------------------------------------------------- #

    def describe(self) -> str:
        caps = ", ".join(CAPABILITY_HELP.get(c, c) for c in self.capabilities)
        bits = [caps]
        if self.resources != ("*",):
            bits.append("on " + ", ".join(self.resources))
        if self.max_amount_usd is not None:
            bits.append(f"up to ${self.max_amount_usd:.2f} (${self.spent_usd:.2f} used)")
        if self.uses is not None:
            bits.append(f"{self.uses_left} use(s) left")
        if self.expires_at:
            bits.append("until " + self.expires_at.astimezone().strftime("%H:%M"))
        return " - ".join(bits)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "capabilities": list(self.capabilities),
            "resources": list(self.resources),
            "uses": self.uses,
            "used": self.used,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "max_amount_usd": self.max_amount_usd,
            "spent_usd": self.spent_usd,
            "reason": self.reason,
            "source": self.source,
        }


def _cap_match(pattern: str, capability: str) -> bool:
    """`web.*` matches `web.publish`; `*` matches everything."""
    return fnmatch(capability, pattern)


# --------------------------------------------------------------------------- #
# Natural-language parsing
# --------------------------------------------------------------------------- #

#: Phrases that mean "I am authorising you". Everything before one of these is
#: ignored; the authorised action is whatever follows.
_AUTH_LEAD = re.compile(
    r"""(?:^|[\s,.;:-])(?:
        you\s+(?:have|has)\s+(?:my\s+)?permission\s+to |
        (?:i\s+)?(?:hereby\s+)?(?:authorize|authorise|approve|permit)\s+(?:you\s+)?to |
        you(?:'re|\s+are)\s+(?:allowed|authorized|authorised|cleared)\s+to |
        (?:you\s+)?(?:may|can)\s+go\s+ahead\s+and |
        go\s+ahead\s+and |
        feel\s+free\s+to |
        permission\s+(?:granted\s+)?to |
        it(?:'s|\s+is)\s+ok(?:ay)?\s+to |
        you\s+have\s+(?:my\s+)?(?:approval|go[\s-]?ahead)\s+to |
        i\s+approve\s+(?:of\s+)?(?:you\s+)?
    )""",
    re.IGNORECASE | re.VERBOSE,
)

#: Weaker lead-ins ("you may post this") - accepted, but only when a high-risk
#: verb follows closely, so "you can see why" is not mistaken for a grant.
_SOFT_LEAD = re.compile(
    r"(?:^|[\s,.;:-])(?:you\s+(?:may|can|should)|i\s+want\s+you\s+to|please)\s+",
    re.IGNORECASE,
)

_VERB_CAPABILITIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (r"\b(?:buy|purchase|pay|spend|order|checkout|check\s*out|subscribe|"
     r"top\s*up|topup|renew|upgrade\s+the\s+plan|book)\b", (CAP_PAYMENT_SPEND,)),
    (r"\b(?:post|publish|upload|tweet|share\s+(?:it|this|them)\s+(?:on|to)|"
     r"go\s+live|schedule\s+(?:the\s+)?(?:post|video))\b", (CAP_WEB_PUBLISH,)),
    (r"\b(?:email|e-mail|message|dm|text|reply\s+to|respond\s+to|send\s+(?:an?\s+)?"
     r"(?:email|message|invite|reply))\b", (CAP_COMMS_SEND,)),
    (r"\b(?:delete|remove|erase|wipe|uninstall|rm\b)", (CAP_FS_DELETE,)),
    (r"\b(?:install|configure\s+(?:the\s+)?system|"
     r"change\s+(?:the\s+|my\s+|your\s+)?(?:system\s+|jarvis\s+)?(?:settings|config\w*)|"
     r"edit\s+the\s+registry|update\s+windows|"
     r"add\s+.{0,20}\bto\s+(?:the\s+)?allow\s?list)\b", (CAP_SYSTEM_CONFIG,)),
    (r"\b(?:run|execute)\s+(?:the\s+)?(?:command|script|terminal|powershell|shell|cmd)\b",
     (CAP_SHELL_EXEC,)),
    (r"\b(?:screenshot|capture\s+(?:the\s+)?screen|read\s+my\s+screen)\b",
     (CAP_SCREEN_CAPTURE,)),
    (r"\b(?:type|click|control\s+(?:the\s+)?(?:mouse|keyboard))\b", (CAP_INPUT_CONTROL,)),
)

_AMOUNT = re.compile(
    r"(?:up\s+to|max(?:imum)?(?:\s+of)?|no\s+more\s+than|limit(?:ed)?\s+to|under|"
    r"spend|budget(?:\s+of)?)?\s*"
    r"(?:\$|usd\s*|eur\s*|£)?\s*(\d+(?:[.,]\d{1,2})?)\s*"
    r"(?:\$|dollars?|usd|bucks|euros?)?",
    re.IGNORECASE,
)
_HAS_CURRENCY = re.compile(r"\$|\bdollars?\b|\busd\b|\bbucks\b|\beuros?\b|£", re.IGNORECASE)

_ONCE = re.compile(r"\b(?:this\s+(?:one\s+)?time|just\s+once|only\s+once|once|"
                   r"a\s+single\s+time|one\s+time)\b", re.IGNORECASE)
_ALWAYS = re.compile(r"\b(?:always|from\s+now\s+on|whenever\s+you\s+(?:need|want)|"
                     r"any\s+time|permanently|forever|standing\s+permission)\b", re.IGNORECASE)
_TODAY = re.compile(r"\b(?:today|tonight|for\s+(?:the\s+rest\s+of\s+)?(?:today|tonight)|"
                    r"this\s+(?:evening|session)|overnight)\b", re.IGNORECASE)
_DURATION = re.compile(
    r"\bfor\s+(?:the\s+next\s+)?(\d+)\s*(minute|min|hour|hr|day)s?\b", re.IGNORECASE)
_TIMES = re.compile(r"\b(\d+)\s*(?:times|uses)\b", re.IGNORECASE)

#: Platform / service names worth binding a grant to, so "post to TikTok" does
#: not also authorise posting to LinkedIn.
_KNOWN_TARGETS = (
    "tiktok", "instagram", "youtube", "twitter", "x.com", "facebook", "linkedin",
    "threads", "reddit", "pinterest", "shopify", "etsy", "ebay", "amazon",
    "gmail", "outlook", "slack", "discord", "telegram", "whatsapp",
    "kling", "heygen", "replicate", "openai", "anthropic", "runway", "midjourney",
)


def parse_grants(text: str, source: str = "user") -> list[Grant]:
    """Extract every authorisation the user expressed in *their own* words.

    Returns an empty list when nothing was authorised - which is the common and
    correct case. False negatives are safe (JARVIS asks); false positives are
    not, so the patterns are deliberately conservative.
    """
    if not text or not text.strip():
        return []

    grants: list[Grant] = []

    for clause, sentence in _authorised_clauses(text):
        capabilities = _capabilities_in(clause)
        if not capabilities:
            continue

        uses, expires_at = _scope_from(clause, sentence)
        amount = _amount_in(clause) or _amount_in(sentence) \
            if CAP_PAYMENT_SPEND in capabilities else None
        resources = _targets_in(clause, sentence)

        grants.append(Grant(
            capabilities=tuple(sorted(capabilities)),
            resources=resources,
            uses=uses,
            expires_at=expires_at,
            max_amount_usd=amount,
            reason=clause.strip()[:200],
            source=source,
        ))

    return _merge(grants)


_SENTENCE_SPLIT = re.compile(r"(?<=[.!?;])[\s]+|\n+")


def _sentences(text: str) -> list[tuple[int, int]]:
    """(start, end) spans of each sentence, so a clause cannot leak into the next."""
    spans: list[tuple[int, int]] = []
    cursor = 0
    for match in _SENTENCE_SPLIT.finditer(text):
        if match.start() > cursor:
            spans.append((cursor, match.start()))
        cursor = match.end()
    if cursor < len(text):
        spans.append((cursor, len(text)))
    return spans or [(0, len(text))]


def _authorised_clauses(text: str) -> list[tuple[str, str]]:
    """(authorised clause, containing sentence) for each authorising phrase.

    A clause stops at the end of its sentence, or at the next authorising
    phrase - whichever comes first. Without that, "you have permission to
    upload this. You may also buy credits" would read as one grant covering
    both verbs, and the upload would inherit the purchase's restrictions.
    """
    leads = [m for m in _AUTH_LEAD.finditer(text)]
    lead_starts = [m.start() for m in leads]
    results: list[tuple[str, str]] = []

    def clip(start: int, sentence_end: int) -> int:
        later = [pos for pos in lead_starts if pos > start]
        return min([sentence_end] + later[:1])

    covered: set[tuple[int, int]] = set()
    for match in leads:
        sent_start, sent_end = _sentence_containing(text, match.start())
        end = clip(match.start(), sent_end)
        results.append((text[match.end():end], text[sent_start:sent_end]))
        covered.add((sent_start, sent_end))

    # Weak lead-ins ("you may post this, once") count only when a high-risk verb
    # follows closely AND the sentence carries a scope or amount marker - so
    # "please post this" stays an instruction, not an authorisation. Sentences a
    # strong lead-in already claimed are skipped to avoid double-counting.
    for match in _SOFT_LEAD.finditer(text):
        span = _sentence_containing(text, match.start())
        if span in covered:
            continue
        sent_start, sent_end = span
        clause = text[match.end():clip(match.start(), sent_end)]
        sentence = text[sent_start:sent_end]
        if not _capabilities_in(clause[:120]) & HIGH_RISK_CAPABILITIES:
            continue
        if _ONCE.search(sentence) or _ALWAYS.search(sentence) \
                or _HAS_CURRENCY.search(clause) or _DURATION.search(sentence) \
                or _TODAY.search(sentence):
            results.append((clause, sentence))
            covered.add(span)

    return results


def _sentence_containing(text: str, index: int) -> tuple[int, int]:
    for start, end in _sentences(text):
        if start <= index < end:
            return start, end
    return 0, len(text)


def _capabilities_in(clause: str) -> set[str]:
    found: set[str] = set()
    for pattern, caps in _VERB_CAPABILITIES:
        if re.search(pattern, clause, re.IGNORECASE):
            found.update(caps)
    return found


def _scope_from(clause: str, full_text: str) -> tuple[int | None, datetime | None]:
    """How many uses and for how long. Defaults to a single use - the safe read."""
    now = datetime.now(timezone.utc)
    scope_text = f"{clause} {full_text}"

    # Most specific wins. An explicit "this time" in the authorising clause
    # outranks a vaguer "tonight" elsewhere in the sentence.
    times = _TIMES.search(clause)
    if times:
        return int(times.group(1)), None
    if _ONCE.search(clause):
        return 1, None

    duration = _DURATION.search(scope_text)
    if duration:
        n, unit = int(duration.group(1)), duration.group(2).lower()
        delta = {"minute": timedelta(minutes=n), "min": timedelta(minutes=n),
                 "hour": timedelta(hours=n), "hr": timedelta(hours=n),
                 "day": timedelta(days=n)}[unit]
        return None, now + delta

    if _ALWAYS.search(scope_text):
        return None, None                      # standing permission
    if _TODAY.search(clause) or _TODAY.search(scope_text):
        return None, _end_of_local_day(now)
    if _ONCE.search(scope_text):
        return 1, None

    return 1, None                             # unqualified = exactly once


def _end_of_local_day(now: datetime) -> datetime:
    local = now.astimezone()
    tomorrow = (local + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return tomorrow.astimezone(timezone.utc)


def _amount_in(clause: str) -> float | None:
    if not _HAS_CURRENCY.search(clause):
        return None
    best: float | None = None
    for match in _AMOUNT.finditer(clause):
        window = clause[max(0, match.start() - 12): match.end() + 12]
        if not _HAS_CURRENCY.search(window):
            continue
        try:
            value = float(match.group(1).replace(",", "."))
        except ValueError:
            continue
        best = value if best is None else min(best, value)
    return best


def _targets_in(clause: str, full_text: str = "") -> tuple[str, ...]:
    """Bind the grant to any service named nearby.

    "post them to TikTok - you're allowed to upload them" names the platform
    *before* the authorising phrase, so fall back to the whole sentence.
    Narrowing a grant can only ever make it safer: an unmatched resource means
    JARVIS asks instead of acting.
    """
    for scope in (clause, full_text):
        if not scope:
            continue
        lowered = scope.lower()
        hits = [name for name in _KNOWN_TARGETS if name in lowered]
        if hits:
            return tuple(f"*{name}*" for name in hits)
    return ("*",)


def _merge(grants: Iterable[Grant]) -> list[Grant]:
    """Collapse duplicates produced by overlapping lead-in matches."""
    merged: dict[tuple, Grant] = {}
    for grant in grants:
        key = (grant.capabilities, grant.resources, grant.max_amount_usd)
        existing = merged.get(key)
        if existing is None:
            merged[key] = grant
            continue
        # Keep the most permissive interpretation of a repeated authorisation.
        if grant.uses is None or (existing.uses is not None and grant.uses > existing.uses):
            existing.uses = grant.uses
        if grant.expires_at and (existing.expires_at is None
                                 or grant.expires_at > existing.expires_at):
            existing.expires_at = grant.expires_at
    return list(merged.values())


# --------------------------------------------------------------------------- #
# Allowlist widening ("JARVIS, you can use Photoshop")
# --------------------------------------------------------------------------- #

_APP_ALLOW = re.compile(
    r"""(?:you\s+(?:can|may|should)\s+(?:now\s+)?(?:use|open|control|drive|run)|
        you(?:'re|\s+are)\s+allowed\s+to\s+(?:use|open|control|drive)|
        (?:add|allow)(?!\s+me\b)|
        (?:i\s+)?(?:want|need)\s+you\s+to\s+use|
        feel\s+free\s+to\s+use|
        permission\s+to\s+use
    )\s+(?:the\s+|my\s+|an?\s+)?
    (?P<app>[A-Za-z][A-Za-z0-9 .+&_-]{1,28}?)
    (?=\s+(?:for|to|when|whenever|if|and|instead|on|from|with|so|because|app\b|
        application\b|program\b)\b|[,.;!?]|$)""",
    re.IGNORECASE | re.VERBOSE,
)

#: Words that show the match ran past the app name into the rest of the sentence.
_APP_STOPWORDS = {
    "it", "them", "that", "this", "these", "those", "anything", "everything",
    "whatever", "something", "the", "my", "your", "a", "an", "any", "all",
    "computer", "internet", "web", "money", "time", "files", "file",
}


_APP_BOUNDARY = (r"(?=\s+(?:for|to|when|whenever|if|and|instead|on|from|with|so|"
                 r"because|app\b|application\b|program\b)\b|[,.;!?]|$)")

#: "...use CapCut and Premiere Pro" - pick up the second name too.
_APP_CONTINUATION = re.compile(
    r"^\s*(?:,|and|&|\+)\s+(?P<app>[A-Z][A-Za-z0-9 .+&_-]{1,28}?)" + _APP_BOUNDARY)


def parse_app_allowances(text: str) -> list[str]:
    """App names the user just approved, in their own words.

    Deliberately deterministic and separate from the model: saying "you can use
    Photoshop" widens the allowlist, but JARVIS deciding it would like to use
    Photoshop does not. Same security boundary as `parse_grants`.
    """
    found: list[str] = []
    text = text or ""

    def take(raw: str) -> None:
        name = " ".join(raw.split()[:3]).strip(" .,-_&")
        if len(name) < 2 or name.lower() in _APP_STOPWORDS:
            return
        if name.lower() in {existing.lower() for existing in found}:
            return
        found.append(name)

    for match in _APP_ALLOW.finditer(text):
        take(match.group("app"))
        # Walk any "and X, Y" list that follows the first app name.
        cursor = match.end()
        while (nxt := _APP_CONTINUATION.match(text[cursor:])) is not None:
            take(nxt.group("app"))
            cursor += nxt.end()

    return found
