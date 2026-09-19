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

import contextlib
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

#: How many lessons go into the prompt at the start of a call. A budget, not
#: a capacity: everything beyond this is still stored and still reachable with
#: `how_do_i`, so teaching it more never stops working - it just stops being
#: free of a lookup.
BLOCK_LIMIT = 25

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
        #: The last few things said, with when. Not just the latest: with
        #: preemptive generation the next thing you say can arrive before the
        #: tools for the last thing have reported, and attributing a recipe to
        #: the wrong sentence teaches it that "turn it up" means "open
        #: Netflix".
        self._said: list[tuple[float, str]] = []
        #: Tool calls seen so far for the request being carried out. A real
        #: job runs in several rounds - search, look, click - and each round
        #: arrives as its own event. Keeping only the last one learns "click
        #: the play button" with no search in front of it.
        self._turn_trigger = ""
        self._turn_calls: list[tuple[str, Any, bool]] = []
        self._turn_failed = False
        self._taught_this_turn = False

    @property
    def available(self) -> bool:
        return self.db is not None

    # ------------------------------------------------------------------ #
    # What goes into the instructions
    # ------------------------------------------------------------------ #

    def block(self, limit: int = BLOCK_LIMIT) -> str:
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
            "This is the most recent and most used of what you know, not all "
            "of it. If they ask for a job that sounds like something they have "
            "had you do before and it isn't listed here, call how_do_i before "
            "guessing.\n"
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
            "lessons back at them unless they ask what you know.\n"
            "- If they ask for one of their own jobs and you can't see the "
            "steps for it, call how_do_i before guessing. You know more than "
            "fits in these instructions.")

    # ------------------------------------------------------------------ #
    # Watching
    # ------------------------------------------------------------------ #

    def heard(self, said: str, at: float | None = None) -> None:
        """Remember the words of a request, and when they arrived."""
        import time

        text = " ".join((said or "").split())
        if not text:
            return
        self._said.append((time.time() if at is None else at, text))
        del self._said[:-8]                    # a few is plenty
        self._taught_this_turn = False

        try:
            # Counts towards "this lesson is useful" only when one exists.
            if self.db is not None and self.db.lesson_for(text):
                self.db.lesson_used(text)
        except Exception:
            pass

    def _trigger_for(self, started_at: float | None) -> str:
        """Which sentence asked for the tools that started at this moment.

        The newest thing said BEFORE the work began. Anything said after is a
        different request that happens to overlap - an interruption, a
        follow-up, a change of mind - and hanging this recipe on it is how the
        store fills up with nonsense.
        """
        if not self._said:
            return ""
        if started_at is None:
            return self._said[-1][1]
        earlier = [text for when, text in self._said if when <= started_at]
        return earlier[-1] if earlier else ""

    def watched(self, calls: list[tuple[str, Any, bool]],
                started_at: float | None = None) -> str:
        """Record what has been done for this request so far.

        Called once per round of tool calls, and a real job takes several. The
        rounds accumulate into one recipe rather than replacing each other, so
        "play Daft Punk on Spotify" is learned as search-then-click and not as
        a bare click.
        """
        if not self.db or self._taught_this_turn:
            return ""

        trigger = self._trigger_for(started_at)
        if not trigger or not worth_learning(trigger):
            return ""

        if trigger != self._turn_trigger:      # a new request: start again
            self._turn_trigger = trigger
            self._turn_calls = []
            self._turn_failed = False

        self._turn_calls.extend(calls)
        # One failure anywhere and the whole recipe goes: half a job is not a
        # job, and a lesson that reproduces a mistake is worse than having to
        # work it out again. That includes the part already written down from
        # the rounds before the failure.
        if any(f for _, _, f in calls):
            self._turn_failed = True
            self._forget_partial(trigger)
        if self._turn_failed:
            return ""

        steps = [describe_call(name, args) for name, args, _ in self._turn_calls
                 if name not in NOT_A_STEP]
        if not steps:
            return ""
        try:
            return self.db.learn(trigger, ", then ".join(steps),
                                 said=trigger, source="watched")
        except Exception:
            return ""

    def _closest(self, wanted: str) -> str:
        """What to say when nothing matches outright.

        Near misses beat a flat "never taught me", because the words people
        use for the same job drift - "put my Spotify on" one week, "open my
        Spotify" the next - and the model is better placed than any overlap
        score to tell whether two phrasings mean the same thing. Offered as
        guesses, never as the answer: a near miss followed as though it were
        exact is how it does the wrong job confidently.
        """
        try:
            near = self.db.near_lessons(wanted, limit=5)
        except Exception:
            near = []
        if not near:
            return (f"You've never shown me how to {wanted}. Do it the best "
                    f"way you can, and if they correct you, call remember_how.")
        lines = "\n".join(
            f'- when they say "{row["said"] or row["trigger"]}": {row["steps"]}'
            for row in near)
        return (f"Nothing matches {wanted!r} exactly. The closest things they "
                f"have taught you are below - if one of them is the same job "
                f"said differently, follow it; if none of them is, do it your "
                f"own way and let them correct you.\n" + lines)

    def _forget_partial(self, trigger: str) -> None:
        """Drop the half-recipe written before a later step failed.

        Only ever removes something it wrote itself by watching: a lesson you
        taught out loud is not collateral for a tool that misfired.
        """
        try:
            existing = self.db.lesson_for(trigger)
            if existing and existing.get("source") == "watched":
                self.db.unlearn(existing["trigger"])
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # The tool
    # ------------------------------------------------------------------ #

    @property
    def tools(self) -> list:
        return [self.remember_how, self.how_do_i]

    @function_tool()
    async def how_do_i(self, context: RunContext, task: str) -> str:
        """Look up how they told you to do a job you can't see in your notes.

        Your instructions carry the lessons they use most and most recently,
        which is not all of them. Use this when they ask for something that
        sounds like one of their own jobs - a name only they would use, a
        routine, a piece of their work - and you don't already have the steps.

        Faster than guessing and far better than getting it wrong. One call,
        then do what it says.

        Don't use it for ordinary requests you already know how to do, and
        don't use it for general knowledge - it only knows what they taught it.

        Args:
            task: What they asked for, in their words.
        """
        if not self.db:
            raise ToolError("I can't get at my notes right now.")
        wanted = (task or "").strip()
        if not wanted:
            raise ToolError("Look up how to do what?")
        try:
            found = self.db.lesson_for(wanted)
        except Exception as exc:
            raise ToolError(f"I couldn't check my notes: {exc}") from exc
        if not found:
            return self._closest(wanted)
        with contextlib.suppress(Exception):
            # Counts as used, which is what brings it back into the prompt for
            # next time - so asking twice in a week only costs a lookup once.
            self.db.lesson_used(found["trigger"])
        return (f'They taught you: when they say "{found["said"] or found["trigger"]}", '
                f'{found["steps"]}')

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
        self._turn_trigger, self._turn_calls = "", []
        return f"Learned: when they say {key!r}, {steps}"
