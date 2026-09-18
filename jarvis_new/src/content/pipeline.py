"""From an idea to a published Reel, and an honest account of how far we are.

The pipeline is six stages. Only the first runs today, and this module says so
out loud rather than pretending: `readiness()` reports every stage, what it
needs, and what is missing, so "why hasn't it posted anything" has an answer
that names a key or a program rather than a shrug.

    idea -> script -> voiceover -> visuals -> render -> caption -> publish
            ^ here

The order of implementation is deliberate. The script is the only stage whose
quality caps every stage after it, it is the only one that is free, and it is
the only one that produces something a human can judge in ten seconds. A
pipeline that renders beautiful video from a weak script is an expensive way
to find out the script was weak.

Two things about the later stages are settled now, because building them the
other way would have to be undone:

* **Publishing goes through the official Instagram Graph API**, on a Business
  or Creator account, which supports Reels publishing and read-back of
  analytics. It is the only path that does not risk the account.
* **Paid services are confined to this pipeline.** The voiceover and the
  visuals are where money buys retention, and the money switch that governs
  the assistant's thinking does not govern them - the permission broker and
  its spend caps do, per run.
"""

from __future__ import annotations

import contextlib
import shutil
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from . import styles
from .models import ReelScript
from .scriptwriter import Scriptwriter
from .store import KIND_SCRIPT, ContentStore, new_ref, store

#: Extra attempts a script job gets. See `queue_scripts` for why it is safe.
SCRIPT_RETRIES = 2


@dataclass
class Stage:
    """One step in the pipeline, and what it cannot run without."""

    key: str
    label: str
    detail: str
    implemented: bool = False
    free: bool = True
    keys: tuple[str, ...] = ()
    packages: tuple[str, ...] = ()
    programs: tuple[str, ...] = ()
    #: An existing plugin in the older generation that already does this work.
    #: Named so the next person wiring the stage reaches for it instead of
    #: writing a second one.
    plugin: str = ""
    notes: str = ""

    def missing(self) -> list[str]:
        """What this stage still needs from outside: keys, packages, programs.

        Whether the stage is written at all is deliberately not in this list.
        They are different problems with different answers - one is a thing to
        install, the other is a thing to build - and running them together is
        how a status line ends up saying "needs the stage isn't built yet".
        """
        gaps: list[str] = []
        for name in self.keys:
            if not _has_key(name):
                gaps.append(f"the {name} key")
        for package in self.packages:
            try:
                __import__(package)
            except ImportError:
                gaps.append(f"the {package} package")
        for program in self.programs:
            if shutil.which(program) is None:
                gaps.append(f"{program} on PATH")
        return gaps

    def ready(self, gaps: list[str] | None = None) -> bool:
        gaps = self.missing() if gaps is None else gaps
        return self.implemented and not gaps

    def blocker(self, gaps: list[str] | None = None) -> str:
        """One phrase saying what stands between here and this stage running."""
        gaps = self.missing() if gaps is None else gaps
        if self.implemented:
            return f"needs {_and_list(gaps)}" if gaps else ""
        if gaps:
            return f"isn't built yet, and will need {_and_list(gaps)}"
        return "isn't built yet"

    def as_dict(self) -> dict[str, Any]:
        """This stage, checked once.

        `missing()` decrypts the key vault, imports packages and searches PATH
        for programs. This used to call it three times - once directly, once
        through `ready`, once through `blocker` - for each of six stages, on
        every status call, and the status call is a voice tool.
        """
        gaps = self.missing()
        return {"key": self.key, "label": self.label, "detail": self.detail,
                "implemented": self.implemented, "free": self.free,
                "ready": self.ready(gaps), "missing": gaps,
                "blocker": self.blocker(gaps), "plugin": self.plugin,
                "notes": self.notes}


PIPELINE: tuple[Stage, ...] = (
    Stage(
        key="script",
        label="Write the script",
        detail="Hook, timed beats, on-screen text, visual prompts, caption and "
               "hashtags, as structured data the later stages can read.",
        implemented=True,
        free=True,
        notes="Runs on the free thinking chooser: your own machine if Ollama "
              "is up, otherwise Google's free tier. Never a paid model unless "
              "paid thinking is switched on.",
    ),
    Stage(
        key="voiceover",
        label="Speak the narration",
        detail="Turn the narration into an audio file, one per script.",
        implemented=False,
        free=True,
        plugin="text_to_speech",
        notes="Free on edge-tts, which is good enough to test the pipeline. "
              "ElevenLabs is the paid upgrade and is where the first pound of "
              "the budget should go: the voice is what people stay for.",
    ),
    Stage(
        key="visuals",
        label="Generate the shots",
        detail="One image or clip per beat, from the beat's visual prompt.",
        implemented=False,
        free=False,
        keys=("REPLICATE_API_TOKEN",),
        plugin="generate_image",
        notes="FLUX on Replicate, about $0.003 an image. A 6-beat Reel is "
              "roughly two pence of stills.",
    ),
    Stage(
        key="render",
        label="Cut it together",
        detail="Assemble the shots, the voiceover and the burned-in captions "
               "into a 9:16 video.",
        implemented=False,
        free=True,
        programs=("ffmpeg",),
        plugin="video_merge",
        notes="ffmpeg does this locally and free. Paid generative video "
              "(Kling, HeyGen) is a different stage, not a replacement.",
    ),
    Stage(
        key="review",
        label="Hold it for approval",
        detail="Put the finished file and its caption somewhere you can watch "
               "it before anything is published.",
        implemented=False,
        free=True,
        notes="Deliberately between render and publish. The first weeks of a "
              "new channel are worth watching by eye.",
    ),
    Stage(
        key="publish",
        label="Publish to Instagram",
        detail="Upload the Reel and its caption through the official Instagram "
               "Graph API, then read the analytics back.",
        implemented=False,
        free=True,
        keys=("INSTAGRAM_ACCESS_TOKEN",),
        plugin="post_to_social",
        notes="Graph API on a Business or Creator account. Reels publishing "
              "is a two-call flow - create a media container pointing at a "
              "publicly reachable video URL, then publish it - so the render "
              "has to be uploaded somewhere reachable first. Nothing here "
              "automates warm-up, follows, likes or comments on other "
              "accounts: that is what gets accounts banned.",
    ),
)

STAGE_BY_KEY = {stage.key: stage for stage in PIPELINE}


def _and_list(items: list[str]) -> str:
    """"a, b and c" - the way a person reads a list out loud."""
    items = [item for item in items if item]
    if not items:
        return "nothing"
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]


def _has_key(name: str) -> bool:
    """Is this API key stored, in the vault or the environment?"""
    try:
        from jarvis.config import Config

        return bool(Config().key(name))
    except Exception:
        import os

        return bool(os.environ.get(name))


def readiness() -> dict[str, Any]:
    """Every stage, what it needs and what is missing. Drives the status tool."""
    stages = [stage.as_dict() for stage in PIPELINE]
    profile = styles.get()
    return {
        "stages": stages,
        "ready": [s["key"] for s in stages if s["ready"]],
        "blocked": [s["key"] for s in stages if not s["ready"]],
        "style": {"name": profile.name, "placeholder": profile.placeholder,
                  "file": str(styles.path())},
    }


# --------------------------------------------------------------------------- #
# The one job that runs today
# --------------------------------------------------------------------------- #

@dataclass
class ScriptJobResult:
    """What a finished script job leaves behind."""

    ref: str
    topic: str
    style: str
    scripts: list[ReelScript] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"ref": self.ref, "topic": self.topic, "style": self.style,
                "count": len(self.scripts),
                "hooks": [script.hook for script in self.scripts],
                "scripts": [script.as_dict() for script in self.scripts]}


def run_script_job(ref: str, topic: str, count: int = 3, style: str = "",
                   extra: str = "", db: ContentStore | None = None,
                   writer: Scriptwriter | None = None) -> dict[str, Any]:
    """Write the scripts for an already-queued job. Runs on a worker thread.

    The job row exists before this is called, so the voice can answer "started,
    reference 4f2a" immediately and this can take as long as it takes. Every
    exit path - success, failure, a model that refused - leaves the row in a
    final state, because a job stuck on "running" forever is indistinguishable
    from a worker that died.
    """
    db = db or store()
    db.start_job(ref, stage="script")
    try:
        scripts = (writer or Scriptwriter()).write(topic, count=count,
                                                   style=style, extra=extra)
    except Exception as exc:
        db.fail_job(ref, f"{type(exc).__name__}: {exc}", stage="script")
        raise

    for script in scripts:
        db.add_asset(ref, kind="script", body=script.as_dict(),
                     meta={"hook": script.hook,
                           "seconds": script.estimated_seconds()})

    result = ScriptJobResult(ref=ref, topic=topic,
                             style=style or styles.get(style).name,
                             scripts=scripts)
    db.finish_job(ref, result=result.as_dict(), stage="script")
    return result.as_dict()


def _script_job(ref: str, topic: str, count: int, style: str, extra: str,
                account: str, db: ContentStore | None) -> dict[str, Any]:
    """Everything a script job does, from the worker thread's side.

    Creating the row lives here rather than in `queue_scripts` on purpose.
    See that function for why.
    """
    db = db or store()
    # The style file is written on first use rather than at install time, so
    # it appears the moment there is something to edit it for.
    with contextlib.suppress(Exception):
        styles.write_starter_file()
    try:
        db.create_job(topic=topic, kind=KIND_SCRIPT, style=style,
                      account=account, ref=ref)
    except sqlite3.IntegrityError:
        # This is a retry of a job whose row already exists. Creating it again
        # is the one thing here that is not repeatable, so it is the one thing
        # that has to be allowed to have already happened - otherwise the
        # second attempt fails on the bookkeeping rather than on the work.
        if db.job(ref) is None:
            raise
    return run_script_job(ref, topic, count, style, extra, db=db)


def recover_interrupted(db: ContentStore | None = None) -> list[str]:
    """Close off content jobs abandoned by a process that died.

    Runs on a worker when the agent starts. Without it, a job that was in
    flight when the machine was shut down stays "running" forever: the status
    tool reports work in progress that nothing is doing, and the reference
    somebody was given never resolves into anything.
    """
    try:
        orphans = (db or store()).recover_interrupted()
    except Exception as exc:
        print(f"  [content] couldn't check for interrupted jobs: {exc}")
        return []
    if orphans:
        print(f"  Content engine: closed {len(orphans)} job(s) interrupted by "
              f"a previous shutdown.")
    return orphans


def queue_scripts(topic: str, count: int = 3, style: str = "", extra: str = "",
                  account: str = "", db: ContentStore | None = None) -> str:
    """Hand the job to a background worker and return its reference at once.

    There is no database work on this path, and that is the point rather than
    an optimisation. The reference is a random string, not a row id, so it can
    be invented here and the row written on the worker - which means the voice
    never waits on SQLite's write lock.

    It is worth being precise about why that matters, because the obvious
    version (write the row, return its reference) profiles at well under a
    millisecond and looks perfectly fine. The cost is not the write, it is the
    *lock*: one busy writer - a batch of jobs finishing, a render logging its
    progress - and the voice thread joins the queue behind it. Measured under
    a saturated host, writing the row here put the tool at 1.4 seconds at the
    95th percentile and occasionally failed outright with "database is
    locked", raised at the person talking. Off the write path it is tens of
    microseconds and cannot fail that way at all.

    The error path below does touch the database, which is fine: nobody is
    waiting on a fast answer to a job that is not going to run.
    """
    import workers

    ref = new_ref()
    queued = workers.host().submit(
        _script_job, ref, topic, count, style, extra, account, db,
        name=f"scripts: {topic[:40]}",
        # Writing a script is one call to somebody else's model, and the
        # usual way that fails is a rate limit or a 503 that would have
        # worked thirty seconds later. Two extra attempts costs nothing when
        # the first succeeds and saves the whole job when it doesn't. It is
        # safe to repeat: the work is a model call and a set of rows keyed by
        # this reference, all of which the retry overwrites.
        retries=SCRIPT_RETRIES)
    if queued is None:
        with contextlib.suppress(Exception):
            failed = db or store()
            failed.create_job(topic=topic, kind=KIND_SCRIPT, style=style,
                              account=account, ref=ref)
            failed.fail_job(ref, "the background worker wouldn't start",
                            stage="script")
    return ref
