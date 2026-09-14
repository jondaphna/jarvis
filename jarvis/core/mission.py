"""Missions: saved workflows JARVIS runs on a schedule, with nobody watching.

A mission is a JSON file listing steps. Each step names a plugin, a tool, or an
`ai` instruction, gets parameters with `{placeholders}` filled in from earlier
steps, and stores its result under an `output_key`.

The one subtlety worth knowing: a mission may carry `authorizations` - plain
sentences like "you have permission to post to TikTok". Those are honoured
because the *user wrote the mission file*, which makes it their instruction, in
the same way a spoken command is. They are scoped to that run and expire when
it ends. Nothing JARVIS generates can add one.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from . import events
from .events import bus, log
from .grants import Grant, parse_grants
from .tools import ExecContext

ON_ERROR_STOP = "stop"
ON_ERROR_CONTINUE = "continue"
ON_ERROR_RETRY = "retry"


class MissionError(RuntimeError):
    """A mission is malformed or could not run."""


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #

@dataclass
class Step:
    plugin: str
    params: dict[str, Any] = field(default_factory=dict)
    name: str = ""
    output_key: str = ""
    on_error: str = ON_ERROR_STOP
    retries: int = 1
    timeout_seconds: int = 900
    #: Only run when this earlier output is truthy, e.g. "scripts".
    condition: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any], index: int) -> "Step":
        plugin = raw.get("plugin") or raw.get("tool") or raw.get("action")
        if not plugin:
            raise MissionError(f"Step {index + 1} doesn't say which plugin to use.")
        on_error = str(raw.get("on_error", ON_ERROR_STOP)).lower()
        if on_error not in (ON_ERROR_STOP, ON_ERROR_CONTINUE, ON_ERROR_RETRY):
            raise MissionError(
                f"Step {index + 1}: on_error must be stop, continue or retry.")
        return cls(
            plugin=str(plugin),
            params=dict(raw.get("params") or {}),
            name=str(raw.get("name") or plugin),
            output_key=str(raw.get("output_key") or ""),
            on_error=on_error,
            retries=max(1, int(raw.get("retries", 1))),
            timeout_seconds=int(raw.get("timeout_seconds", 900)),
            condition=str(raw.get("condition") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name, "plugin": self.plugin,
                               "params": self.params}
        if self.output_key:
            out["output_key"] = self.output_key
        if self.on_error != ON_ERROR_STOP:
            out["on_error"] = self.on_error
        if self.retries != 1:
            out["retries"] = self.retries
        if self.condition:
            out["condition"] = self.condition
        return out


@dataclass
class Mission:
    id: str
    name: str
    steps: list[Step]
    description: str = ""
    schedule: str = ""              # 5-field cron, blank = manual only
    enabled: bool = True
    tags: list[str] = field(default_factory=list)
    #: Sentences authorising high-risk actions for this run. User-authored only.
    authorizations: list[str] = field(default_factory=list)
    max_minutes: int = 120
    notify_on_finish: bool = True
    path: Path | None = None

    # -- serialisation --------------------------------------------------- #

    @classmethod
    def from_dict(cls, raw: dict[str, Any], path: Path | None = None) -> "Mission":
        if not isinstance(raw, dict):
            raise MissionError("A mission file must contain a JSON object.")

        steps_raw = raw.get("steps")
        if not isinstance(steps_raw, list) or not steps_raw:
            raise MissionError("A mission needs a non-empty 'steps' list.")

        mission_id = str(raw.get("id") or (path.stem if path else "")).strip()
        if not mission_id:
            raise MissionError("A mission needs an 'id'.")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", mission_id):
            raise MissionError(
                f"Mission id {mission_id!r} must be letters, numbers, _ . or - only.")

        schedule = str(raw.get("schedule") or "").strip()
        if schedule:
            validate_cron(schedule)

        authorizations = raw.get("authorizations") or raw.get("permissions") or []
        if isinstance(authorizations, str):
            authorizations = [authorizations]

        return cls(
            id=mission_id,
            name=str(raw.get("name") or mission_id),
            description=str(raw.get("description") or ""),
            schedule=schedule,
            enabled=bool(raw.get("enabled", True)),
            tags=[str(t) for t in (raw.get("tags") or [])],
            authorizations=[str(a) for a in authorizations],
            max_minutes=int(raw.get("max_minutes", 120)),
            notify_on_finish=bool(raw.get("notify_on_finish", True)),
            steps=[Step.from_dict(step, index) for index, step in enumerate(steps_raw)],
            path=path,
        )

    @classmethod
    def load(cls, path: str | Path) -> "Mission":
        path = Path(path)
        try:
            raw = json.loads(path.read_text("utf-8"))
        except json.JSONDecodeError as exc:
            raise MissionError(f"{path.name} isn't valid JSON: {exc}") from exc
        except OSError as exc:
            raise MissionError(f"Couldn't read {path}: {exc}") from exc
        return cls.from_dict(raw, path=path)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "schedule": self.schedule,
            "enabled": self.enabled,
            "steps": [step.to_dict() for step in self.steps],
        }
        if self.tags:
            out["tags"] = self.tags
        if self.authorizations:
            out["authorizations"] = self.authorizations
        if self.max_minutes != 120:
            out["max_minutes"] = self.max_minutes
        return out

    def save(self, directory: Path | None = None) -> Path:
        from .. import paths

        target = self.path or ((directory or paths.MISSION_DIR) / f"{self.id}.json")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        self.path = target
        return target

    # -- helpers --------------------------------------------------------- #

    def grants(self) -> list[Grant]:
        """The authorisations this mission carries, as real Grant objects."""
        grants: list[Grant] = []
        for sentence in self.authorizations:
            parsed = parse_grants(sentence, source=f"mission:{self.id}")
            if not parsed:
                log.warning(
                    "mission %s: couldn't read an authorisation from %r - write it "
                    "like \"you have permission to post to TikTok\"", self.id, sentence)
            grants.extend(parsed)
        return grants

    def summary(self) -> str:
        when = f"on schedule {self.schedule}" if self.schedule else "manual only"
        return f"{self.name} ({len(self.steps)} steps, {when})"


def validate_cron(expression: str) -> None:
    """Fail loudly on a bad schedule at load time, not at 2am."""
    try:
        from apscheduler.triggers.cron import CronTrigger
        CronTrigger.from_crontab(expression)
    except ImportError:
        parts = expression.split()
        if len(parts) != 5:
            raise MissionError(
                f"Schedule {expression!r} should have 5 parts, like '0 2 * * *'.")
    except Exception as exc:
        raise MissionError(
            f"Schedule {expression!r} isn't a valid cron expression: {exc}. "
            f"'0 2 * * *' means 2am daily.") from exc


# --------------------------------------------------------------------------- #
# Placeholder interpolation
# --------------------------------------------------------------------------- #

_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*(?:\[[^\]]*\]|\.[A-Za-z0-9_]+)*)\}")


def resolve(value: Any, context: dict[str, Any]) -> Any:
    """Substitute {placeholders} anywhere in a params structure.

    A string that is *exactly* one placeholder yields the raw object, so a list
    of scripts stays a list. Placeholders inside a larger string are rendered as
    text, so prompts read naturally.
    """
    if isinstance(value, str):
        whole = _PLACEHOLDER.fullmatch(value.strip())
        if whole:
            return lookup(whole.group(1), context)
        return _PLACEHOLDER.sub(lambda m: _as_text(lookup(m.group(1), context)), value)
    if isinstance(value, dict):
        return {key: resolve(item, context) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve(item, context) for item in value]
    return value


def lookup(path: str, context: dict[str, Any]) -> Any:
    """Resolve `scripts[*].title`, `results[0].url`, `brief` against the context."""
    tokens = _tokenise(path)
    if not tokens:
        return ""
    current: Any = context.get(tokens[0], "")

    for token in tokens[1:]:
        if token == "*":
            if isinstance(current, dict):
                current = list(current.values())
            elif not isinstance(current, list):
                current = [current]
            continue

        if isinstance(current, list):
            if token.lstrip("-").isdigit():
                index = int(token)
                current = current[index] if -len(current) <= index < len(current) else ""
            else:
                # Mapping a field across a list: scripts[*].title
                current = [_field(item, token) for item in current]
            continue

        current = _field(current, token)

    return current


def _tokenise(path: str) -> list[str]:
    tokens: list[str] = []
    for part in re.split(r"\.|\[", path):
        part = part.rstrip("]").strip()
        if part:
            tokens.append(part)
    return tokens


def _field(item: Any, name: str) -> Any:
    if isinstance(item, dict):
        return item.get(name, "")
    return getattr(item, name, "")


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str, indent=2)
    except (TypeError, ValueError):
        return str(value)


def placeholders_in(mission: Mission) -> set[str]:
    """Every {name} a mission references - used to catch typos before running."""
    found: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, str):
            for match in _PLACEHOLDER.finditer(value):
                found.add(_tokenise(match.group(1))[0])
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    for step in mission.steps:
        walk(step.params)
    return found


def validate(mission: Mission, known_actions: Iterable[str]) -> list[str]:
    """Problems a user should fix. Empty list means it's good to run."""
    problems: list[str] = []
    known = set(known_actions)
    produced: set[str] = set()

    for index, step in enumerate(mission.steps, 1):
        if step.plugin not in known and step.plugin not in ("ai", "wait"):
            problems.append(
                f"Step {index} ({step.name}): there's no plugin or tool called "
                f"'{step.plugin}'.")

        for name in _referenced(step.params):
            if name not in produced:
                problems.append(
                    f"Step {index} ({step.name}): uses {{{name}}}, but no earlier "
                    f"step produces it.")

        if step.condition and step.condition not in produced:
            problems.append(
                f"Step {index} ({step.name}): condition '{step.condition}' isn't "
                f"produced by an earlier step.")

        if step.output_key:
            produced.add(step.output_key)

    if mission.schedule:
        try:
            validate_cron(mission.schedule)
        except MissionError as exc:
            problems.append(str(exc))

    for sentence in mission.authorizations:
        if not parse_grants(sentence):
            problems.append(
                f"Authorisation {sentence!r} doesn't read as a permission. Try "
                f'"You have permission to post to TikTok, today only."')

    return problems


def _referenced(params: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(params, str):
        for match in _PLACEHOLDER.finditer(params):
            found.add(_tokenise(match.group(1))[0])
    elif isinstance(params, dict):
        for item in params.values():
            found |= _referenced(item)
    elif isinstance(params, list):
        for item in params:
            found |= _referenced(item)
    return found


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #

@dataclass
class StepOutcome:
    name: str
    plugin: str
    status: str                  # ok | failed | skipped
    output: Any = None
    error: str = ""
    seconds: float = 0.0


@dataclass
class RunResult:
    mission_id: str
    run_id: int
    status: str                  # success | partial | failed
    steps: list[StepOutcome] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    seconds: float = 0.0

    def summary(self) -> str:
        done = sum(1 for s in self.steps if s.status == "ok")
        failed = [s for s in self.steps if s.status == "failed"]
        skipped = sum(1 for s in self.steps if s.status == "skipped")

        parts = [f"{done}/{len(self.steps)} steps completed"]
        if failed:
            parts.append("failed: " + ", ".join(f"{s.name} ({s.error[:80]})"
                                                for s in failed[:3]))
        if skipped:
            parts.append(f"{skipped} skipped")
        return "; ".join(parts)


class MissionRunner:
    """Executes a mission's steps in order, passing outputs along."""

    def __init__(self, app) -> None:
        self.app = app          # the Assistant

    async def run(self, mission: Mission, trigger: str = "manual",
                  unattended: bool = True) -> RunResult:
        memory = self.app.memory
        started = time.monotonic()
        run_id = memory.start_run(mission.id, mission.name, trigger)

        bus.publish(events.MISSION_START, f"Mission started: {mission.name}",
                    mission=mission.id, run_id=run_id, steps=len(mission.steps))
        log.info("mission %s started (run %s, %s)", mission.id, run_id, trigger)

        # The user authored this file, so its authorisations are their words.
        # They live only as long as the run.
        issued: list[Grant] = []
        for grant in mission.grants():
            grant.source = f"mission:{mission.id}"
            self.app.broker.add_grant(grant)
            issued.append(grant)

        context: dict[str, Any] = {"mission_id": mission.id, "run_id": run_id}
        result = RunResult(mission_id=mission.id, run_id=run_id, status="success",
                           context=context)
        deadline = started + max(60, mission.max_minutes * 60)

        try:
            for index, step in enumerate(mission.steps):
                if time.monotonic() > deadline:
                    result.status = "partial"
                    result.error = f"hit the {mission.max_minutes} minute time limit"
                    break

                outcome = await self._run_step(step, index, context, run_id,
                                               mission, unattended, deadline)
                result.steps.append(outcome)

                if outcome.status == "failed" and step.on_error == ON_ERROR_STOP:
                    result.status = "failed"
                    result.error = f"{step.name}: {outcome.error}"
                    break
                if outcome.status == "failed":
                    result.status = "partial"

        except asyncio.CancelledError:
            result.status = "failed"
            result.error = "cancelled"
            memory.finish_run(run_id, "cancelled", "cancelled", result.summary())
            raise
        except Exception as exc:
            log.exception("mission %s crashed", mission.id)
            result.status = "failed"
            result.error = str(exc)
        finally:
            for grant in issued:
                self.app.broker.revoke(grant.id)

        result.seconds = round(time.monotonic() - started, 1)
        memory.finish_run(run_id, result.status, result.error, result.summary())

        if result.status == "failed":
            bus.publish(events.MISSION_FAILED,
                        f"Mission failed: {mission.name} - {result.error}",
                        mission=mission.id, run_id=run_id)
        else:
            bus.publish(events.MISSION_DONE,
                        f"Mission finished: {mission.name} - {result.summary()}",
                        mission=mission.id, run_id=run_id, status=result.status)

        if mission.notify_on_finish:
            await self._notify(mission, result)

        return result

    # ------------------------------------------------------------------ #

    async def _run_step(self, step: Step, index: int, context: dict[str, Any],
                        run_id: int, mission: Mission, unattended: bool,
                        deadline: float) -> StepOutcome:
        memory = self.app.memory
        step_id = memory.start_step(run_id, index, step.name, step.plugin)
        began = time.monotonic()

        if step.condition:
            value = lookup(step.condition, context)
            if not value:
                memory.finish_step(step_id, "skipped", None,
                                   f"condition '{step.condition}' was empty")
                bus.publish(events.MISSION_STEP,
                            f"Skipped {step.name} (nothing to work with)",
                            mission=mission.id, run_id=run_id, status="skipped")
                return StepOutcome(step.name, step.plugin, "skipped",
                                   error=f"condition '{step.condition}' was empty")

        try:
            params = resolve(step.params, context)
        except Exception as exc:
            memory.finish_step(step_id, "failed", None, f"bad placeholder: {exc}")
            return StepOutcome(step.name, step.plugin, "failed", error=str(exc))

        bus.publish(events.MISSION_STEP, f"{mission.name}: {step.name}",
                    mission=mission.id, run_id=run_id, step=index + 1,
                    total=len(mission.steps), plugin=step.plugin)

        execution = ExecContext(actor=f"mission:{mission.id}", run_id=run_id,
                                assistant=self.app, unattended=unattended)

        attempts = step.retries if step.on_error == ON_ERROR_RETRY else 1
        last_error = ""

        for attempt in range(1, attempts + 1):
            remaining = max(5.0, deadline - time.monotonic())
            try:
                output = await asyncio.wait_for(
                    self._dispatch(step, params, execution, mission),
                    timeout=min(step.timeout_seconds, remaining),
                )
            except asyncio.TimeoutError:
                last_error = f"timed out after {step.timeout_seconds}s"
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_error = str(exc)
            else:
                if step.output_key:
                    context[step.output_key] = output
                memory.finish_step(step_id, "ok", output)
                return StepOutcome(step.name, step.plugin, "ok", output=output,
                                   seconds=round(time.monotonic() - began, 1))

            if attempt < attempts:
                wait = min(30, 2 ** attempt)
                log.info("step %s failed (%s), retrying in %ss", step.name,
                         last_error, wait)
                await asyncio.sleep(wait)

        log.warning("mission %s step %s failed: %s", mission.id, step.name, last_error)
        memory.finish_step(step_id, "failed", None, last_error)
        return StepOutcome(step.name, step.plugin, "failed", error=last_error,
                           seconds=round(time.monotonic() - began, 1))

    async def _dispatch(self, step: Step, params: dict[str, Any],
                        context: ExecContext, mission: Mission) -> Any:
        """Run a step against a plugin, a tool, or the AI itself."""
        if step.plugin == "wait":
            await asyncio.sleep(float(params.get("seconds", 1)))
            return f"waited {params.get('seconds', 1)}s"

        if step.plugin == "ai":
            # A free-form instruction Claude carries out with full tool access.
            instruction = params.get("instruction") or params.get("prompt") or ""
            reply = await self.app.brain.chat(
                str(instruction),
                tier="deep",
                unattended=True,
                run_id=context.run_id,
                actor=context.actor,
                parse_permissions=False,   # a mission can't grant itself anything
                deadline_seconds=step.timeout_seconds,
                system_extra=f"You are running mission '{mission.name}'.",
            )
            return reply.text

        # Prefer the tool registry: that is where the permission gate lives, and
        # every available plugin is registered there. Going straight to the
        # plugin would let a mission step act without a permission ruling.
        tool = self.app.tools.get(step.plugin)
        if tool is not None:
            result = await self.app.tools.execute(step.plugin, params, context)
            if result.is_error:
                raise MissionError(result.content)
            return result.data if result.data is not None else result.content

        plugin = self.app.plugins.get(step.plugin)
        if plugin is not None:
            # Say "install ffmpeg" before asking for approval to run something
            # that cannot run - an impossible step should not reach the queue.
            missing = plugin.missing_requirements()
            if missing:
                raise MissionError(
                    f"The {plugin.NAME} plugin isn't ready - missing: "
                    f"{', '.join(missing)}. Add it in Settings or with `jarvis setup`.")

            # A plugin that isn't exposed as a tool still gets judged, using the
            # capability it declares. Nothing runs ungated.
            if plugin.CAPABILITY:
                from .permissions import Request

                params = self.app.tools.resolve_paths_for(
                    tuple(plugin.PATH_KEYS), params,
                    required=tuple(plugin.SCHEMA.get("required") or ()))

                resource = plugin.SERVICE or (
                    params.get(plugin.RESOURCE_KEY) if plugin.RESOURCE_KEY else None)
                amount = None
                if plugin.AMOUNT_KEY:
                    try:
                        amount = float(params.get(plugin.AMOUNT_KEY))
                    except (TypeError, ValueError):
                        amount = None
                await self.app.broker.require(Request(
                    capability=plugin.CAPABILITY,
                    resource=None if resource is None else str(resource),
                    summary=f"{step.name} ({plugin.NAME})",
                    actor=context.actor,
                    amount_usd=amount,
                    run_id=context.run_id,
                ))
            return await self.app.plugins.run(step.plugin, params, context)

        raise MissionError(
            f"Step '{step.name}' wants '{step.plugin}', which isn't a plugin or a "
            f"tool. Check the spelling, or add a plugin file for it.")

    async def _notify(self, mission: Mission, result: RunResult) -> None:
        verdict = {"success": "finished", "partial": "finished with problems",
                   "failed": "failed"}.get(result.status, result.status)
        message = f"{mission.name} {verdict}. {result.summary()}"
        try:
            await self.app.computer.notify("JARVIS", message[:250])
        except Exception:
            log.debug("desktop notification failed", exc_info=True)
