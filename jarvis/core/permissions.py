"""The guardrail engine - what JARVIS may do, and what it must ask about first.

Three tiers, in order of how much they trust:

1. **Free rein** inside the workspace folder and the app/site allowlist.
   Reading, writing and organising files there, searching the web, thinking,
   launching apps you approved - all silent and immediate.

2. **Queued approval** for anything outside that box: touching files elsewhere,
   opening an app you never approved, driving the keyboard in an unknown app,
   running a shell command. The action stops, lands in the approval queue, and
   you clear it with one tap.

3. **Default-blocked, grant-only** for the four that cost you money or
   reputation: spending, publishing/uploading, sending messages, changing
   system config. Policy can never allow these. The only thing that unblocks
   one is *you*, either in the command that starts the work ("you have
   permission to buy this, once") or by approving the request afterwards.

Plus a hard floor that nothing overrides: a short list of catastrophic
commands and protected paths (credential stores, system directories, JARVIS's
own vault) that are refused no matter who asks.
"""

from __future__ import annotations

import re
import sys
import threading
from dataclasses import dataclass, field
from enum import IntEnum
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

from . import events
from .events import bus, log
from .grants import (
    CAP_APP_LAUNCH, CAP_COMMS_SEND, CAP_FS_DELETE, CAP_FS_READ, CAP_FS_WRITE,
    CAP_INPUT_CONTROL, CAP_LLM_CALL, CAP_PAYMENT_SPEND, CAP_SCREEN_CAPTURE,
    CAP_SHELL_EXEC, CAP_SYSTEM_CONFIG, CAP_WEB_PUBLISH, CAP_WEB_READ,
    CAP_WEB_SEARCH, CAPABILITY_HELP, HIGH_RISK_CAPABILITIES, Grant, parse_grants,
)
from .memory import Memory
from .people import Authority


class Risk(IntEnum):
    SAFE = 0
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    FORBIDDEN = 4

    @property
    def label(self) -> str:
        return self.name.lower()


class Outcome(IntEnum):
    ALLOW = 0
    NEEDS_APPROVAL = 1
    DENY = 2


class PermissionDenied(Exception):
    """Raised by ``require()`` when an action is not permitted."""

    def __init__(self, decision: "Decision") -> None:
        super().__init__(decision.reason)
        self.decision = decision


@dataclass
class Request:
    """One thing JARVIS wants to do."""

    capability: str
    resource: str | None = None
    summary: str = ""
    actor: str = "jarvis"
    amount_usd: float | None = None
    run_id: int | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def describe(self) -> str:
        if self.summary:
            return self.summary
        verb = CAPABILITY_HELP.get(self.capability, self.capability)
        return f"{verb}: {self.resource}" if self.resource else verb


@dataclass
class Decision:
    outcome: Outcome
    risk: Risk
    reason: str
    request: Request
    grant_id: str | None = None
    approval_id: int | None = None

    @property
    def allowed(self) -> bool:
        return self.outcome is Outcome.ALLOW

    def user_message(self) -> str:
        """Plain-language explanation to read back to the user."""
        if self.allowed:
            return f"Allowed: {self.request.describe()}"
        if self.outcome is Outcome.NEEDS_APPROVAL:
            return (f"I need your approval to {self.request.describe()}. "
                    f"{self.reason} It's in the approval queue"
                    + (f" as #{self.approval_id}." if self.approval_id else "."))
        return f"I can't {self.request.describe()}. {self.reason}"


# --------------------------------------------------------------------------- #
# Hard floor - refused regardless of grants or approvals
# --------------------------------------------------------------------------- #

_CATASTROPHIC_SHELL = (
    r"rm\s+(-[a-z]*\s+)*-[a-z]*[rf]", r"\bmkfs\b", r"\bdd\s+.*of=/dev/",
    r":\(\)\s*\{.*\};\s*:", r"\bformat\s+[a-z]:", r"\bdiskpart\b",
    r"del\s+/[sf]\b.*\\", r"rd\s+/s\s+/q\s+[a-z]:\\?$", r"\bshutdown\b",
    r"\breboot\b", r"\bhalt\b", r"vssadmin\s+delete", r"cipher\s+/w",
    r"\bfdisk\b", r"chmod\s+-R\s+777\s+/", r"\bkillall\b", r"Remove-Item.*-Recurse.*C:\\",
    r"curl[^|]*\|\s*(ba)?sh", r"wget[^|]*\|\s*(ba)?sh",
    r"Invoke-Expression.*DownloadString", r"iex\s*\(.*DownloadString",
)

_PROTECTED_DIR_NAMES = (
    ".ssh", ".aws", ".gnupg", ".gpg", ".kube", ".docker", ".config/gcloud",
    "credentials", "keychains", "keyrings",
)

_PROTECTED_FILE_PATTERNS = (
    "*id_rsa*", "*id_ed25519*", "*.pem", "*.pfx", "*.p12", "*.keystore",
    "*shadow", "*sam", "*ntds.dit", "keys.enc", "vault.salt", "*cookies.sqlite",
    "*login data", "*key4.db", "*logins.json",
)


def _system_roots() -> tuple[Path, ...]:
    if sys.platform == "win32":
        roots = [Path("C:/Windows"), Path("C:/Program Files"), Path("C:/Program Files (x86)")]
    elif sys.platform == "darwin":
        roots = [Path("/System"), Path("/Library"), Path("/usr"), Path("/bin"), Path("/sbin")]
    else:
        roots = [Path("/etc"), Path("/usr"), Path("/bin"), Path("/sbin"), Path("/boot"),
                 Path("/lib"), Path("/sys"), Path("/proc"), Path("/dev")]
    return tuple(roots)


# --------------------------------------------------------------------------- #
# Broker
# --------------------------------------------------------------------------- #

ApprovalCallback = Callable[[Request, Risk, str], Awaitable[bool]]


class PermissionBroker:
    """Evaluates every action JARVIS wants to take and records the decision."""

    def __init__(self, settings, memory: Memory) -> None:
        self.settings = settings
        self.memory = memory
        self._grants: list[Grant] = []
        self._lock = threading.RLock()
        #: Set by an attended front-end (CLI/UI) to ask the user live. When
        #: None - the overnight case - medium-risk actions queue instead.
        self.approval_callback: ApprovalCallback | None = None
        #: Who is currently being served. Identity can only ever NARROW what is
        #: allowed - it never grants anything the owner wouldn't already have.
        self.actor_authority: Authority = Authority.OWNER
        self.actor_name: str = ""

    # ------------------------------------------------------------------ #
    # Grants
    # ------------------------------------------------------------------ #

    def grant_from_user_command(self, text: str, source: str = "voice") -> list[Grant]:
        """Parse authorisations out of something *the user* said or typed.

        Never call this with text JARVIS merely read (web pages, emails, model
        output). That is the whole security boundary of the grant system.
        """
        grants = parse_grants(text, source=source)
        for grant in grants:
            self.add_grant(grant)
        return grants

    def add_grant(self, grant: Grant) -> Grant:
        with self._lock:
            self._grants.append(grant)
        log.info("permission granted: %s", grant.describe())
        bus.publish(events.PERMISSION, f"Permission granted - {grant.describe()}",
                    grant=grant.to_dict(), decision="grant")
        self.memory.log_action("user", ",".join(grant.capabilities),
                               ",".join(grant.resources), "grant", "granted",
                               grant.reason, details=grant.to_dict())
        return grant

    def note_user_paths(self, text: str) -> list[Grant]:
        """Treat paths the user names in a command as read-approved for a while.

        Asking "what's in C:\\Reports\\Q3.xlsx?" and then being asked for
        permission to read C:\\Reports\\Q3.xlsx is the kind of thing that makes
        assistants useless. Naming a path is consent to read it.
        """
        paths = _extract_paths(text)
        if not paths:
            return []
        from datetime import datetime, timedelta, timezone
        grant = Grant(
            capabilities=(CAP_FS_READ,),
            resources=tuple(f"{p}*" for p in paths),
            uses=None,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            reason=f"user referenced {', '.join(paths)}",
            source="user-reference",
        )
        with self._lock:
            self._grants.append(grant)
        return [grant]

    def active_grants(self) -> list[Grant]:
        with self._lock:
            self._grants = [g for g in self._grants if not g.expired]
            return list(self._grants)

    def revoke(self, grant_id: str) -> bool:
        with self._lock:
            before = len(self._grants)
            self._grants = [g for g in self._grants if g.id != grant_id]
            return len(self._grants) < before

    def clear_grants(self) -> int:
        with self._lock:
            count = len(self._grants)
            self._grants.clear()
        return count

    def _matching_grant(self, req: Request) -> Grant | None:
        for grant in self.active_grants():
            if grant.covers(req.capability, req.resource, req.amount_usd):
                return grant
        return None

    # ------------------------------------------------------------------ #
    # Standing allowlists (the "5-second widen")
    # ------------------------------------------------------------------ #

    def allow_app(self, name: str) -> None:
        apps = list(self.settings.get("autonomy.allowed_apps", []))
        key = name.strip().lower()
        if key and key not in apps:
            apps.append(key)
            self.settings.set("autonomy.allowed_apps", apps)
            self.settings.save()
            log.info("app allowlisted: %s", key)

    def allow_domain(self, domain: str) -> None:
        domains = list(self.settings.get("autonomy.allowed_domains", []))
        key = _hostname(domain)
        if key and key not in domains:
            domains.append(key)
            self.settings.set("autonomy.allowed_domains", domains)
            self.settings.save()

    def allow_workspace(self, path: str) -> None:
        extra = list(self.settings.get("autonomy.extra_workspaces", []))
        resolved = str(Path(path).expanduser())
        if resolved not in extra:
            extra.append(resolved)
            self.settings.set("autonomy.extra_workspaces", extra)
            self.settings.save()

    def workspaces(self) -> list[Path]:
        roots = [self.settings.workspace]
        for extra in self.settings.get("autonomy.extra_workspaces", []) or []:
            roots.append(Path(extra).expanduser())
        return roots

    # ------------------------------------------------------------------ #
    # Who is asking
    # ------------------------------------------------------------------ #

    def acting_as(self, authority: Authority, name: str = "") -> None:
        """Serve the next requests as this person."""
        self.actor_authority = authority
        self.actor_name = name

    def reset_actor(self) -> None:
        self.actor_authority = Authority.OWNER
        self.actor_name = ""

    def _authority_block(self, req: Request) -> Decision | None:
        """Refuse anything this person's level doesn't reach. None = carry on.

        Deliberately a veto and nothing more: a guest can never be granted
        something the owner's own policy would refuse, because this runs before
        the normal rules and can only say no.
        """
        level = self.actor_authority
        if level is Authority.OWNER:
            return None

        who = self.actor_name or "they"

        if level is Authority.BLOCKED:
            return Decision(Outcome.DENY, Risk.FORBIDDEN,
                            f"I don't take instructions from {who}.", req)

        if level is Authority.GUEST:
            if req.capability == CAP_LLM_CALL:
                return None
            return Decision(
                Outcome.DENY, Risk.FORBIDDEN,
                f"{who} can talk to me, but can't have me do things on this "
                f"machine. Ask {self._owner_phrase()} if you need that.", req)

        if level is Authority.TRUSTED:
            risk, _ = self.assess(req)
            if risk <= Risk.SAFE:
                return None
            return Decision(
                Outcome.DENY, Risk.FORBIDDEN,
                f"{who} can ask me things, but changing anything is reserved "
                f"for {self._owner_phrase()}.", req)

        return None

    def _owner_phrase(self) -> str:
        owner = getattr(self, "people", None)
        if owner is not None:
            person = owner.owner()
            if person is not None:
                return person.name
        return "the owner"

    # ------------------------------------------------------------------ #
    # Risk assessment
    # ------------------------------------------------------------------ #

    def assess(self, req: Request) -> tuple[Risk, str]:
        """Classify a request. Pure - no side effects, safe to call for preview."""
        cap = req.capability
        resource = req.resource or ""

        if cap in (CAP_FS_READ, CAP_FS_WRITE, CAP_FS_DELETE):
            return self._assess_path(cap, resource)

        if cap == CAP_SHELL_EXEC:
            return self._assess_shell(resource)

        if cap == CAP_APP_LAUNCH:
            if self._app_allowed(resource):
                return Risk.LOW, "app is on your allowlist"
            return Risk.MEDIUM, f"'{resource}' is not on your approved app list yet"

        if cap == CAP_INPUT_CONTROL:
            # Driving an app you already approved is part of using that app.
            if resource and self._app_allowed(resource):
                return Risk.LOW, "controlling an app on your allowlist"
            return Risk.MEDIUM, "controlling keyboard/mouse outside an approved app"

        if cap == CAP_SCREEN_CAPTURE:
            return Risk.LOW, "reading the screen"

        if cap in (CAP_WEB_SEARCH,):
            return Risk.SAFE, "read-only web search"

        if cap == CAP_WEB_READ:
            allowed = self.settings.get("autonomy.allowed_domains", []) or []
            if not allowed:
                return Risk.SAFE, "read-only browsing"
            host = _hostname(resource)
            if any(host == d or host.endswith("." + d) for d in allowed):
                return Risk.SAFE, "domain is on your allowlist"
            return Risk.MEDIUM, f"'{host}' is not on your approved site list"

        if cap == CAP_LLM_CALL:
            cap_usd = float(self.settings.get("autonomy.daily_spend_cap_usd", 0) or 0)
            if cap_usd > 0 and self.memory.spend_since(24) >= cap_usd:
                return Risk.FORBIDDEN, (
                    f"daily AI spend cap of ${cap_usd:.2f} reached - raise it in "
                    f"Settings if you want JARVIS to keep working")
            return Risk.SAFE, "thinking"

        if cap in HIGH_RISK_CAPABILITIES:
            return Risk.HIGH, _high_risk_reason(cap)

        return Risk.MEDIUM, "unrecognised capability - treated as needing approval"

    def _assess_path(self, cap: str, raw: str) -> tuple[Risk, str]:
        if not raw:
            return Risk.MEDIUM, "no path given"
        try:
            path = Path(raw).expanduser()
            resolved = path.resolve()
        except (OSError, RuntimeError):
            return Risk.FORBIDDEN, "path could not be resolved"

        if _is_protected(resolved):
            return Risk.FORBIDDEN, (
                "that's a protected location (credentials, system files, or "
                "JARVIS's own key vault) - I never touch those")

        writing = cap in (CAP_FS_WRITE, CAP_FS_DELETE)
        for root in self.workspaces():
            try:
                if resolved == root.resolve() or resolved.is_relative_to(root.resolve()):
                    return (Risk.LOW if writing else Risk.SAFE), "inside your workspace"
            except (OSError, RuntimeError, ValueError):
                continue

        if cap == CAP_FS_READ:
            return Risk.MEDIUM, "outside your workspace"
        return Risk.MEDIUM, "writing outside your workspace"

    def _assess_shell(self, command: str) -> tuple[Risk, str]:
        text = (command or "").strip()
        if not text:
            return Risk.MEDIUM, "empty command"
        for pattern in _CATASTROPHIC_SHELL:
            if re.search(pattern, text, re.IGNORECASE):
                return Risk.FORBIDDEN, (
                    "that command is on the never-run list (it can destroy data "
                    "or the machine). Edit it by hand if you really mean it")
        allowed = self.settings.get("autonomy.allowed_shell", []) or []
        for pattern in allowed:
            if fnmatch(text, pattern):
                return Risk.LOW, "matches a command you pre-approved"
        return Risk.MEDIUM, "shell commands need your approval"

    def _app_allowed(self, name: str) -> bool:
        key = (name or "").strip().lower()
        if not key:
            return False
        stem = Path(key).stem
        for allowed in self.settings.get("autonomy.allowed_apps", []) or []:
            allowed = allowed.strip().lower()
            if not allowed:
                continue
            if allowed in key or allowed == stem or fnmatch(key, allowed):
                return True
        return False

    # ------------------------------------------------------------------ #
    # The decision
    # ------------------------------------------------------------------ #

    def check(self, req: Request) -> Decision:
        """Decide without consuming grants or queueing. For previews and tools."""
        # Identity is checked before anything else, and can only refuse.
        blocked = self._authority_block(req)
        if blocked is not None:
            return blocked

        risk, reason = self.assess(req)

        if risk is Risk.FORBIDDEN:
            return Decision(Outcome.DENY, risk, reason, req)

        grant = self._matching_grant(req)
        if grant is not None:
            return Decision(Outcome.ALLOW, risk,
                            f"you authorised this ({grant.reason or grant.describe()})",
                            req, grant_id=grant.id)

        if risk <= Risk.LOW:
            if risk is Risk.SAFE or self.settings.get("autonomy.auto_approve_low_risk", True):
                return Decision(Outcome.ALLOW, risk, reason, req)
            return Decision(Outcome.NEEDS_APPROVAL, risk, reason, req)

        if risk is Risk.HIGH:
            return Decision(Outcome.DENY, risk, (
                f"{reason} I only do that when you authorise it in the request "
                f'itself - for example: "you have permission to '
                f'{CAPABILITY_HELP.get(req.capability, req.capability)} this time".'), req)

        return Decision(Outcome.NEEDS_APPROVAL, risk, reason, req)

    async def authorize(self, req: Request) -> Decision:
        """Decide and act: consume the grant, ask live, or queue for approval."""
        decision = self.check(req)

        if decision.outcome is Outcome.ALLOW and decision.grant_id:
            for grant in self.active_grants():
                if grant.id == decision.grant_id:
                    grant.consume(req.amount_usd)
                    break

        # Medium risk with somebody watching: just ask.
        if decision.outcome is Outcome.NEEDS_APPROVAL and self.approval_callback is not None:
            try:
                approved = await self.approval_callback(req, decision.risk, decision.reason)
            except Exception:
                log.warning("approval callback failed", exc_info=True)
                approved = False
            if approved:
                decision = Decision(Outcome.ALLOW, decision.risk,
                                    "you approved it just now", req)
            else:
                decision = Decision(Outcome.DENY, decision.risk,
                                    "you declined it", req)

        # Nobody watching (or high risk): record it so it can be cleared later.
        elif decision.outcome in (Outcome.NEEDS_APPROVAL, Outcome.DENY) \
                and decision.risk in (Risk.MEDIUM, Risk.HIGH):
            decision.approval_id = self.memory.queue_approval(
                capability=req.capability,
                resource=req.resource,
                risk=decision.risk.label,
                summary=req.describe(),
                requested_by=req.actor,
                payload={"details": req.details, "amount_usd": req.amount_usd,
                         "reason": decision.reason, "run_id": req.run_id},
            )
            bus.publish(events.APPROVAL_NEEDED, decision.user_message(),
                        approval_id=decision.approval_id, capability=req.capability,
                        resource=req.resource, risk=decision.risk.label)

        self._record(decision)
        return decision

    async def require(self, req: Request) -> Decision:
        """authorize(), but raise PermissionDenied when the answer is no."""
        decision = await self.authorize(req)
        if not decision.allowed:
            raise PermissionDenied(decision)
        return decision

    def _record(self, decision: Decision) -> None:
        req = decision.request
        self.memory.log_action(
            actor=req.actor,
            capability=req.capability,
            resource=req.resource,
            risk=decision.risk.label,
            decision=decision.outcome.name.lower(),
            reason=decision.reason,
            run_id=req.run_id,
            details={"summary": req.describe(), "amount_usd": req.amount_usd,
                     "grant": decision.grant_id, "approval": decision.approval_id},
        )
        level = "allow" if decision.allowed else decision.outcome.name.lower()
        bus.publish(events.PERMISSION, decision.user_message(),
                    capability=req.capability, resource=req.resource,
                    risk=decision.risk.label, decision=level,
                    approval_id=decision.approval_id)

    # ------------------------------------------------------------------ #
    # Clearing the queue
    # ------------------------------------------------------------------ #

    def approve(self, approval_id: int, note: str = "", grant_future: bool = False) -> bool:
        """Approve a queued request, optionally granting the capability going forward."""
        item = self.memory.get_approval(approval_id)
        if not item or item["status"] != "pending":
            return False
        self.memory.decide_approval(approval_id, "approved", note)
        self.add_grant(Grant(
            capabilities=(item["capability"],),
            resources=(item["resource"],) if item["resource"] and not grant_future else ("*",),
            uses=None if grant_future else 1,
            max_amount_usd=(item.get("payload") or {}).get("amount_usd"),
            reason=f"approved from the queue: {item['summary']}",
            source=f"approval:{approval_id}",
        ))
        return True

    def deny(self, approval_id: int, note: str = "") -> bool:
        return self.memory.decide_approval(approval_id, "denied", note)

    def pending(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.memory.pending_approvals(limit)

    # ------------------------------------------------------------------ #
    # Prompt fragment
    # ------------------------------------------------------------------ #

    def policy_brief(self) -> str:
        """A short description of current permissions, for the system prompt."""
        workspace = self.settings.workspace
        apps = self.settings.get("autonomy.allowed_apps", []) or []
        domains = self.settings.get("autonomy.allowed_domains", []) or []
        grants = self.active_grants()

        lines = [
            f"Workspace (full freedom to read/write/organise): {workspace}",
            f"Approved apps: {', '.join(apps) if apps else '(none yet)'}",
            f"Approved sites: {', '.join(domains) if domains else '(any, read-only)'}",
            "Blocked unless the user authorises it in their request: spending money, "
            "posting/uploading publicly, sending email or messages, changing system config.",
            "Needs one-tap approval: files outside the workspace, unapproved apps, "
            "shell commands.",
        ]
        if self.actor_authority is not Authority.OWNER:
            lines.append(
                f"You are currently talking to {self.actor_name or 'someone else'}, "
                f"not the owner. Their level is '{self.actor_authority.label}': "
                f"{self.actor_authority.describe()}. Be friendly, answer what you "
                f"can, and say plainly that anything else is the owner's call.")
        if grants:
            lines.append("Authorisations currently in force:")
            lines += [f"  - {g.describe()}" for g in grants]
        else:
            lines.append("No special authorisations are in force right now.")
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _high_risk_reason(cap: str) -> str:
    return {
        CAP_PAYMENT_SPEND: "spending your money",
        CAP_WEB_PUBLISH: "publishing something under your name",
        CAP_COMMS_SEND: "sending a message as you",
        CAP_SYSTEM_CONFIG: "changing system configuration",
    }.get(cap, "a high-risk action")


def _is_protected(path: Path) -> bool:
    from .. import paths as jarvis_paths

    text = str(path).lower().replace("\\", "/")
    name = path.name.lower()

    # Inside JARVIS's own config dir, protect only what's actually sensitive.
    # Blanket-blocking the whole folder would stop JARVIS reading its own logs
    # or mission files, which it legitimately needs to do.
    try:
        root = jarvis_paths.ROOT.resolve()
        if path == root:
            return True
        if path.is_relative_to(root):
            sensitive = {jarvis_paths.VAULT_FILE.name, jarvis_paths.VAULT_SALT_FILE.name,
                         jarvis_paths.DB_FILE.name}
            if path.name in sensitive or path.name.startswith(jarvis_paths.DB_FILE.name):
                return True
            return False
    except (OSError, RuntimeError, ValueError):
        pass

    for root in _system_roots():
        try:
            if path == root or path.is_relative_to(root):
                return True
        except (OSError, RuntimeError, ValueError):
            continue

    for directory in _PROTECTED_DIR_NAMES:
        if f"/{directory}/" in text + "/" or text.endswith(f"/{directory}"):
            return True

    return any(fnmatch(name, pattern) for pattern in _PROTECTED_FILE_PATTERNS)


_PATH_TOKEN = re.compile(
    r"""(?<!\w)(
        [A-Za-z]:[\\/][^\s"'<>|]+          |   # C:\Users\me\file.txt
        ~[\\/][^\s"'<>|]+                  |   # ~/Documents/x
        /(?:home|users|mnt|media|opt|srv|tmp|var)/[^\s"'<>|]+
    )""",
    re.VERBOSE | re.IGNORECASE,
)


def _extract_paths(text: str) -> list[str]:
    out: list[str] = []
    for match in _PATH_TOKEN.finditer(text or ""):
        token = match.group(1).rstrip(".,;:!?)\"'")
        try:
            out.append(str(Path(token).expanduser()))
        except (OSError, RuntimeError):
            continue
    return out


def _hostname(url_or_host: str) -> str:
    raw = (url_or_host or "").strip().lower()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    host = urlparse(raw).hostname or ""
    return host[4:] if host.startswith("www.") else host
