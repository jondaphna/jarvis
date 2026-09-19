"""What the voice can ask the content engine for.

Every tool here returns in milliseconds. None of them waits for a script to be
written: they queue the work, hand back a reference, and let the conversation
carry on. That is not a nicety - a tool that blocks for forty seconds inside a
realtime voice session is forty seconds of an assistant that has stopped
listening, and the user's only evidence is silence.

So the shape is always the same: start it, say it started, and let them ask
again. `content_engine_status` is how they ask again, and it is also how they
find out what the engine cannot do yet and exactly what it needs.
"""

from __future__ import annotations

from typing import Any

from livekit.agents import RunContext, function_tool
from livekit.agents.llm import ToolError

from . import pipeline, store, styles
from .models import ReelScript


class ContentStudio:
    """The business engine, as three things the voice can say."""

    def __init__(self, db: Any = None) -> None:
        #: Injected by the tests. Left alone, it is the shared store.
        self._db = db

    @property
    def db(self) -> Any:
        return self._db if self._db is not None else store.store()

    @property
    def tools(self) -> list:
        return [self.write_reel_scripts, self.content_engine_status,
                self.read_reel_script]

    # ------------------------------------------------------------------ #

    @function_tool()
    async def write_reel_scripts(self, context: RunContext, topic: str,
                                 count: int = 3, style: str = "",
                                 notes: str = "") -> str:
        """Write Instagram Reel scripts about a topic, in the background.

        Use this when they ask for content, scripts, Reels, video ideas, or
        anything for one of their channels. It starts the work and comes back
        immediately - it does not wait for the scripts.

        Say that you've started it and roughly what it will produce. Do NOT
        claim the scripts exist yet: they take under a minute, and they'll ask
        for them when they want them.

        Args:
            topic: What the Reels are about. A sentence, not a word - "why
                most home espresso tastes sour" gives a far better script than
                "coffee".
            count: How many different scripts to write. Three by default, five
                at most in one go.
            style: Which house style to use. Leave blank for the usual one.
            notes: Anything specific they asked for - an angle, a fact to
                include, an audience.
        """
        cleaned = (topic or "").strip()
        if not cleaned:
            raise ToolError("What should the Reels be about?")
        try:
            ref = pipeline.queue_scripts(cleaned, count=count, style=style,
                                         extra=notes, db=self.db)
        except Exception as exc:
            raise ToolError(f"I couldn't start that: {exc}") from exc

        job = self.db.job(ref)
        if job and job.get("status") == store.STATUS_FAILED:
            raise ToolError(f"The content engine didn't start: {job.get('error')}")
        wanted = max(1, min(5, int(count or 1)))
        return (f"Started writing {wanted} Reel script(s) about {cleaned}. "
                f"Reference {ref}. It runs in the background and takes under a "
                f"minute; ask for the scripts when you want them.")

    @function_tool()
    async def content_engine_status(self, context: RunContext) -> str:
        """Say what the content engine is doing and what it still needs.

        Use this when they ask how the content side is going, whether the
        scripts are ready, why nothing has been posted, or what the business
        engine can do yet.
        """
        try:
            jobs = self.db.recent_jobs(limit=5)
        except Exception as exc:
            raise ToolError(f"I couldn't read the content jobs: {exc}") from exc

        lines: list[str] = []
        if not jobs:
            lines.append("Nothing has been queued yet.")
        else:
            for job in jobs:
                lines.append(_describe_job(job))

        state = pipeline.readiness()
        blocked = [stage for stage in state["stages"] if not stage["ready"]]
        if blocked:
            first = blocked[0]
            lines.append(f"The pipeline runs as far as the script. Next is "
                         f"{first['label'].lower()}, which {first['blocker']}.")
        if state["style"]["placeholder"]:
            lines.append(
                "The house style is still the shipped placeholder - it was "
                "written without seeing the reference reels, so the scripts "
                "follow generic best practice rather than your look.")
        return " ".join(lines)

    @function_tool()
    async def read_reel_script(self, context: RunContext,
                               reference: str = "") -> str:
        """Read back a script that has been written.

        Use this when they ask what the scripts say, to hear one, or to hear
        the hooks. Without a reference it reads the most recent finished job.

        Args:
            reference: The job reference given when the work started, like
                '4f2a1b8c'. Leave blank for the latest.
        """
        try:
            job = (self.db.job(reference.strip()) if reference.strip()
                   else _latest_done(self.db))
        except Exception as exc:
            raise ToolError(f"I couldn't read that back: {exc}") from exc

        if job is None:
            raise ToolError("Nothing has finished yet.")
        status = job.get("status")
        if status in (store.STATUS_QUEUED, store.STATUS_RUNNING):
            return (f"That one is still being written - it was queued for "
                    f"{job.get('topic') or 'no topic'}.")
        if status == store.STATUS_FAILED:
            return f"That one failed: {job.get('error') or 'no reason recorded'}."

        result = job.get("result") or {}
        raw_scripts = result.get("scripts") or []
        if not raw_scripts:
            return "That job finished but left no scripts."

        scripts = [ReelScript.from_dict(item) for item in raw_scripts]
        head = (f"{len(scripts)} script(s) on {job.get('topic')}. "
                f"The hooks are: ")
        hooks = " ".join(f"{index}. {script.hook}"
                         for index, script in enumerate(scripts, 1))
        first = scripts[0]
        body = (f" The first one runs about {first.estimated_seconds():.0f} "
                f"seconds and goes: {first.voiceover_text()}")
        return head + hooks + body


# --------------------------------------------------------------------------- #

def _latest_done(db: Any) -> dict[str, Any] | None:
    finished = db.recent_jobs(limit=1, status=store.STATUS_DONE)
    return finished[0] if finished else None


def _describe_job(job: dict[str, Any]) -> str:
    topic = job.get("topic") or "no topic"
    status = job.get("status")
    if status == store.STATUS_DONE:
        count = (job.get("result") or {}).get("count") or 0
        return f"{topic}: {count} script(s) ready, reference {job.get('ref')}."
    if status == store.STATUS_FAILED:
        return f"{topic}: failed - {job.get('error') or 'no reason recorded'}."
    return f"{topic}: {status}."


def describe() -> str:
    """One line for the doctor: which stages are ready, and the style file."""
    state = pipeline.readiness()
    ready = len(state["ready"])
    total = len(state["stages"])
    style = styles.get()
    tail = " (style is still the placeholder)" if style.placeholder else ""
    return f"Content engine: {ready} of {total} stages ready{tail}"
