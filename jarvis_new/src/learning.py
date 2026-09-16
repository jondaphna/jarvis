"""Teach it once. It remembers, and it gets better at your work.

This is the answer to a specific complaint: it can do the basic things, but it
can't manage to do *your* things, and explaining them again every time is
exactly as tiring as doing them yourself.

The obvious-sounding fix - train a custom model on your conversations - is the
wrong one, and worth saying why. A realtime voice model can't be fine-tuned at
all. Even where fine-tuning is possible it needs thousands of examples before
it shifts behaviour, takes hours per round, costs money per round, and produces
a black box: when it gets something wrong you cannot open it up and see what it
thinks you meant, you can only feed it more examples and hope. None of that
describes a person teaching their assistant a habit.

What actually works is remembering. When you explain how you want something
done, that explanation is written down as a **lesson** - the words you say, and
the steps that satisfy them. Every later call starts with your lessons already
in front of it, so the next time those words arrive the recipe is there to be
followed rather than worked out. Teaching is instant, costs nothing, and you
can read the whole thing back and edit it.

Three ways a lesson gets made:

* **You teach it.** "When I say put music on, open Spotify and hit play." One
  sentence, saved immediately.
* **You correct it.** "No - my Spotify, not the web player." The old recipe is
  replaced, not kept alongside the new one.
* **It watches.** When a request works first time, the recipe that worked is
  written down by itself. Watched lessons are held more loosely than taught
  ones and never overwrite something you said out loud.

Nothing here is allowed to break a call. Every path degrades to "learned
nothing this session" rather than raising.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from livekit.agents import RunContext, function_tool
from livekit.agents.llm import ToolError

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

#: Tools that are bookkeeping rather than doing. A recipe made of these
#: teaches nothing: "when they say open Spotify, call remember" is worse than
#: no lesson at all, because it will be followed.
NOT_A_STEP = frozenset({
    "remember", "forget", "search_memory", "recall", "remember_how",
    "think", "end_call", "confirm_browser_action", "confirm_power_action",
})

#: How many words a request can have before it stops being a habit and starts
#: being a one-off. "Open my Spotify" recurs; a rambling paragraph does not,
#: and learning it just fills the prompt with noise.
MAX_TRIGGER_WORDS = 14

#: Openings that mean a question rather than a job. Questions have answers,
#: not recipes, and an answer learned today is wrong tomorrow.
QUESTION_WORDS = (
    "what", "who", "when", "where", "why", "how", "is", "are", "was", "were",
    "do", "does", "did", "can", "could", "should", "would", "will", "tell me",
)


def _open_memory() -> Any:
    try:
        from jarvis.core.memory import Memory

        return Memory()
    except Exception:
        return None


def describe_call(name: str, arguments: Any) -> str:
    """One tool call, written the way a person would read it back."""
    try:
        args = json.loads(arguments) if isinstance(arguments, str) else arguments
    except Exception:
        args = None
    if not isinstance(args, dict):
        return f"{name}"
    parts = []
    for key, value in args.items():
        if isinstance(value, bool):
            parts.append(f"{key}={'yes' if value else 'no'}")
        elif isinstance(value, (int, float)):
            parts.append(f"{key}={value}")
        elif isinstance(value, str) and value.strip():
            parts.append(f'{key}="{value.strip()[:60]}"')
    return f"{name} with {', '.join(parts)}" if parts else name


def worth_learning(said: str) -> bool:
    """Is this phrase a habit worth writing down?

    Deliberately strict. A store full of things said once is a store nobody
    trusts, and every useless lesson costs prompt space that a useful one
    could have had.
    """
    text = " ".join((said or "").split())
    if not text:
        return False
    words = text.split()
    if len(words) < 2 or len(words) > MAX_TRIGGER_WORDS:
        return False
    lowered = text.lower().lstrip(",. ")
    for opener in ("hey jarvis", "ok jarvis", "okay jarvis", "jarvis"):
        if lowered.startswith(opener):
            lowered = lowered[len(opener):].lstrip(",. ")
    if not lowered:
        return False
    return not lowered.startswith(QUESTION_WORDS)


class Lessons:
    """What Jarvis has been taught, and the tool for teaching it more."""

    def __init__(self, memory: Any = None) -> None:
        #: Reuse the caller's database handle when there is one - the shared
        #: SQLite file is happier with one connection than with two.
        self.db = getattr(memory, "db", None) or _open_memory()
        #: The last thing the user said, kept so a successful turn can be
        #: attributed to the words that asked for it.
        self._last_said = ""
        self._taught_this_turn = False

    @property
    def available(self) -> bool:
        return self.db is not None

    # ------------------------------------------------------------------ #
    # What goes into the instructions
    # ------------------------------------------------------------------ #

    def block(self, limit: int = 25) -> str:
        """Your lessons, for the top of the system prompt.

        This is the whole mechanism. Having the recipe already in front of it
        is what turns "work out how to open his Spotify" into "do the thing it
        says here", which is both faster and right more often.
        """
        if not self.db:
            return ""
        try:
            body = self.db.lessons_block(limit=limit)
        except Exception:
            return ""
        if not body:
            return ""
        return (
            "# What they have taught you\n"
            "These are their own instructions for their own jobs, learned from "
            "earlier conversations. When something they say matches one of "
            "these, follow it exactly and immediately - don't reason it out "
            "again, and don't mention that you were taught it.\n"
            "If they correct you, call remember_how with the corrected steps "
            "so it is right from now on.\n\n" + body)

    def guidance(self) -> str:
        """The rule that makes teaching happen without being asked for."""
        return (
            "# Learning\n"
            "- When they explain how they want something done - \"when I say X, "
            "do Y\", \"always open it like this\", \"not that one, the other "
            "one\" - call remember_how straight away, with their words and the "
            "exact steps. Do it quietly. One sentence of confirmation at most.\n"
            "- When they correct something you just did, call remember_how with "
            "the corrected version. Being told twice is a failure.\n"
            "- Never ask permission to learn something, and never read your "
            "lessons back at them unless they ask what you know.")

    # ------------------------------------------------------------------ #
    # Watching
    # ------------------------------------------------------------------ #

    def heard(self, said: str) -> None:
        """Remember the words of the request currently being carried out."""
        text = " ".join((said or "").split())
        if text:
            self._last_said = text
            self._taught_this_turn = False
        try:
            # Counts towards "this lesson is useful" only when one exists.
            if text and self.db is not None and self.db.lesson_for(text):
                self.db.lesson_used(text)
        except Exception:
            pass

    def watched(self, calls: list[tuple[str, Any, bool]]) -> str:
        """Record the recipe that just worked. Returns the trigger, or "".

        `calls` is (tool name, arguments, failed) per call, in order.
        """
        if not self.db or not self._last_said or self._taught_this_turn:
            return ""
        if not worth_learning(self._last_said):
            return ""
        # One failure and the recipe is not worth keeping: a lesson that
        # reproduces a mistake is worse than having to think it through again.
        if any(failed for _, _, failed in calls):
            return ""
        steps = [describe_call(name, args) for name, args, _ in calls
                 if name not in NOT_A_STEP]
        if not steps:
            return ""
        try:
            return self.db.learn(
                self._last_said, ", then ".join(steps),
                said=self._last_said, source="watched")
        except Exception:
            return ""

    # ------------------------------------------------------------------ #
    # The tool
    # ------------------------------------------------------------------ #

    @property
    def tools(self) -> list:
        return [self.remember_how]

    @function_tool()
    async def remember_how(self, context: RunContext, when_they_say: str,
                           do_this: str) -> str:
        """Learn how they want a job done, so you never have to be told twice.

        Call this the moment they teach or correct you. Three situations, all
        of them this tool:

        1. They explain a job: "when I say put music on, open Spotify and press
           play", "my email means Gmail, not Outlook".
        2. They correct something you just did: "no, my Spotify not the web
           player", "next time go straight to the search box".
        3. They state a standing preference about how a task is carried out:
           "always use the current tab", "never open a new window for that".

        Teaching the same trigger again replaces the old steps, which is what
        makes correction work. Write do_this as the actual steps, naming the
        tools and the values - "open_url with site spotify, then click the
        play button" - because the future you reading it cannot see this
        conversation.

        Do it silently. Do not ask whether you should, do not read it back,
        and do not say "I'll remember that" at length - carry on with what
        they asked for.

        Args:
            when_they_say: Their words for the job, as close to how they say
                it as possible. Short - "open my spotify", not a paragraph.
            do_this: The exact steps that satisfy it, naming tools and values.
        """
        if not self.db:
            raise ToolError("Learning isn't available right now.")
        trigger = (when_they_say or "").strip()
        steps = (do_this or "").strip()
        if not trigger or not steps:
            raise ToolError("Teach me what, exactly?")
        try:
            existing = self.db.lesson_for(trigger)
            key = self.db.learn(
                trigger, steps, said=trigger,
                source="corrected" if existing else "taught")
        except Exception as exc:
            raise ToolError(f"Couldn't learn that: {exc}") from exc
        if not key:
            raise ToolError("That didn't give me anything to learn.")
        # Stops the watcher overwriting what was just said out loud with
        # whatever tools happen to run for the rest of this turn.
        self._taught_this_turn = True
        return f"Learned: when they say {key!r}, {steps}"
