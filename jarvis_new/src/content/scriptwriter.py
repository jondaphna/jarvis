"""The agent that writes the Reel.

This is the first real piece of the business engine, and the one every later
stage depends on: a voiceover is only as good as the line it reads, and a
render is only as good as the shot list it was given. So the script is not
prose - it is a structured object with a hook, timed beats, a visual prompt
per beat, a caption and hashtags, which is exactly what the voiceover, visual,
render and publish stages each need a slice of.

It costs nothing to run. Script writing goes through the same chooser the
voice's `think` tool uses, which prefers a model on your own machine, then
Google's free tier, and refuses a paid model unless you have switched paid
thinking on. Generating a hundred scripts a week should never be the line item
that makes the business unprofitable - the money belongs in the voiceover and
the video, where it buys retention.

Two failure modes get real handling rather than an exception:

* **The model returns prose, or JSON wrapped in chatter.** Extracted rather
  than rejected. Models do this often enough that treating it as an error
  would mean a business that stops on a formatting habit.
* **The model returns something structurally useless** - no hook, one beat,
  no visuals. Asked again, once, with the problem named. A second failure is
  reported honestly instead of being published.
"""

from __future__ import annotations

import json
import re
from typing import Any

from . import styles
from .models import ReelScript

#: How many scripts one request may ask for. More than this in a single call
#: makes every one of them worse - the model starts padding - and a batch that
#: large should be several jobs anyway.
MAX_PER_BATCH = 5

SYSTEM = """You are the content director of a faceless short-form video channel.
You write Instagram Reels that hold attention to the last frame.

You are writing for a production pipeline, not for a person. Another program
reads your output: the narration is sent to a voice model, each visual line is
sent to an image model, and the caption is posted as written. So every field
has to stand on its own with no other context.

How a Reel earns its watch time:

- The hook is the product. It is the first thing heard and the only thing that
  decides whether the rest is watched. It states a specific, surprising claim,
  or opens a loop the viewer needs closed. It never introduces, greets, or
  explains what the video will cover.
- Every beat pays off the beat before it and sets up the next one. No beat
  exists to fill time.
- The last beat closes the loop the hook opened. A Reel that resolves early is
  a Reel people leave early.
- Concrete beats abstract. Numbers, names, objects, and things a camera could
  actually point at.

Rules you do not break:

- Output valid JSON and nothing else. No markdown fences, no commentary.
- Never state a fact you were not given and cannot be confident of. If a claim
  would need checking, write it as the question it raises instead.
- Never write a medical, legal, or financial instruction.
- Write the narration as it is to be spoken: no markdown, no emoji, no
  parentheses, no stage directions.
"""

SHAPE = """Return a JSON array of {count} object(s). Each object:

{{
  "title": "a short internal name for this script",
  "hook": "the spoken first line, under 15 words",
  "beats": [
    {{
      "at": 2.5,
      "voiceover": "the line spoken over this shot",
      "on_screen": "3-5 words burned on screen",
      "visual": "a prompt for an image model: subject, setting, lighting, motion"
    }}
  ],
  "caption": "the Instagram caption, 1-2 lines",
  "call_to_action": "one short line",
  "hashtags": ["#example"],
  "seconds": {seconds}
}}

"at" is seconds from the start and must increase down the list."""


class Scriptwriter:
    """Turns a topic into finished Reel scripts, using a free brain by default."""

    def __init__(self, backend: Any = None) -> None:
        #: Injected by the tests, and by anything that wants a specific brain.
        self._backend = backend

    # ------------------------------------------------------------------ #

    def backend(self) -> tuple[Any, str]:
        """The brain that will write, and a line explaining the choice."""
        if self._backend is not None:
            return self._backend, ""
        import thinker

        return thinker.resolve()

    def write(self, topic: str, count: int = 3, style: str = "",
              extra: str = "") -> list[ReelScript]:
        """Write `count` scripts about `topic`. Raises only when it cannot at all."""
        topic = (topic or "").strip()
        if not topic:
            raise ValueError("There's no topic to write about.")
        count = max(1, min(MAX_PER_BATCH, int(count or 1)))

        profile = styles.get(style)
        backend, note = self.backend()
        if note:
            print(f"  [content] {note}")

        # Built once. `as_prompt_block` reads the style file, and the retry
        # below would otherwise read and re-render the whole thing again.
        system = SYSTEM + "\n\n" + profile.as_prompt_block()
        prompt = self._prompt(topic, count, profile, extra)

        scripts = self._parse(backend.ask(system, prompt, "medium"),
                              topic, profile.name)
        kept = _usable(scripts)

        # One honest retry. A model that ignored the shape once usually obeys
        # when the problem is named; a model that ignores it twice is not
        # going to be argued into it, and pretending otherwise burns the
        # user's quota on a loop.
        if len(kept) < count:
            short = count - len(kept)
            retry = self._retry_prompt(topic, short, profile, extra,
                                       scripts, count, kept)
            second = self._parse(backend.ask(system, retry, "medium"),
                                 topic, profile.name)
            # Merged, not replaced. The old version swapped the whole batch
            # for the second attempt's, so two good scripts from the first
            # pass and two from the second came out as two - the user asked
            # for three, the model wrote four usable ones between them, and
            # the code threw half of them away.
            kept = _merge(kept, _usable(second), count)

        if not kept:
            raise RuntimeError(
                "The model didn't produce a usable script - "
                + (self._problems(scripts, count) or "it returned nothing"))
        return kept

    # ------------------------------------------------------------------ #

    def _prompt(self, topic: str, count: int, profile: Any, extra: str) -> str:
        parts = [f"Topic: {topic}"]
        if extra.strip():
            parts.append(f"Also take into account: {extra.strip()}")
        parts.append("")
        parts.append(SHAPE.format(count=count, seconds=profile.seconds))
        if count > 1:
            parts.append(
                "\nThe scripts must open differently from one another - "
                "different hook, different angle on the topic. Three variations "
                "of one sentence is one script, not three.")
        return "\n".join(parts)

    def _retry_prompt(self, topic: str, short: int, profile: Any, extra: str,
                      scripts: list[ReelScript], wanted: int,
                      kept: list[ReelScript]) -> str:
        """Ask again for what is actually missing, not for the whole batch.

        If two of three came back fine, asking for three more is asking the
        model to redo work that was already good - it costs a longer answer,
        a longer wait, and usually a worse result, because the model has to
        find five angles on the topic instead of one.
        """
        base = self._prompt(topic, short, profile, extra)
        problems = self._problems(scripts, wanted)
        lines = [base, "", f"Your previous answer was rejected: {problems}."]
        if kept:
            already = "; ".join(f'"{script.hook}"' for script in kept)
            lines.append(
                f"{len(kept)} script(s) from that answer were kept, opening "
                f"with: {already}. Write {short} more that open differently "
                f"from those and from each other.")
        lines.append("Return ONLY the JSON array described above.")
        return "\n".join(lines)

    def _problems(self, scripts: list[ReelScript], wanted: int) -> str:
        if not scripts:
            return "no JSON array of scripts could be read from it"
        issues: list[str] = []
        if len(scripts) < wanted:
            issues.append(f"only {len(scripts)} of {wanted} scripts")
        for index, script in enumerate(scripts, 1):
            found = script.problems()
            if found:
                issues.append(f"script {index} had {', '.join(found)}")
        return "; ".join(issues)

    def _parse(self, raw: str, topic: str, style: str) -> list[ReelScript]:
        data = extract_json(raw)
        if data is None:
            return []
        if isinstance(data, dict):
            # A single object, or an object wrapping the list under some key.
            for key in ("scripts", "reels", "items", "results"):
                if isinstance(data.get(key), list):
                    data = data[key]
                    break
            else:
                data = [data]
        if not isinstance(data, list):
            return []

        scripts: list[ReelScript] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            script = ReelScript.from_dict(item, topic=topic, style=style)
            scripts.append(_tidy(script))
        return scripts


def _usable(scripts: list[ReelScript]) -> list[ReelScript]:
    return [script for script in scripts if script.is_usable()]


def _merge(first: list[ReelScript], second: list[ReelScript],
           limit: int) -> list[ReelScript]:
    """Everything usable from both attempts, in order, without repeats.

    Repeats are real: a model asked again for what it got wrong will often
    hand back one of the ones it already got right. Matching on the hook
    catches that without rejecting two scripts that merely cover the same
    topic, which is what was asked for.
    """
    out: list[ReelScript] = []
    seen: set[str] = set()
    for script in [*first, *second]:
        mark = " ".join((script.hook or "").lower().split())
        if mark and mark in seen:
            continue
        seen.add(mark)
        out.append(script)
        if len(out) >= limit:
            break
    return out


def _tidy(script: ReelScript) -> ReelScript:
    """Fix what is worth fixing rather than rejecting the whole script.

    Beat timings come back out of order or absent often enough to be normal.
    Re-deriving them from the target length is better than either trusting a
    wrong number, which desynchronises the captions from the audio, or
    throwing away an otherwise good script over it.
    """
    if not script.beats:
        return script
    ordered = sorted(script.beats, key=lambda beat: beat.at)
    if any(beat.at <= 0 for beat in ordered[1:]) or ordered[0].at < 0:
        span = float(script.seconds or 30)
        step = span / (len(ordered) + 1)
        for index, beat in enumerate(ordered, 1):
            beat.at = round(step * index, 1)
    script.beats = ordered
    return script


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(raw: str) -> Any:
    """Pull the JSON out of whatever the model actually said.

    Tried in order: the whole string, the contents of a code fence, and the
    widest bracketed span in the text. Returns None when there is nothing
    parseable, which the caller treats as a failed generation rather than an
    exception - a chatty model is a formatting problem, not a crash.
    """
    text = (raw or "").strip()
    if not text:
        return None

    for candidate in _candidates(text):
        try:
            return json.loads(candidate)
        except (TypeError, ValueError):
            continue
    return None


def _candidates(text: str) -> list[str]:
    found = [text]
    fence = _FENCE.search(text)
    if fence:
        found.append(fence.group(1).strip())
    # Order matters. The widest [ ... ] span first, then a salvaged array,
    # and only then the widest { ... } span: a truncated batch whose first
    # element happens to parse on its own would otherwise come back as one
    # script, silently, when the rest were recoverable.
    for opener, closer in (("[", "]"),):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end > start:
            found.append(text[start:end + 1])
    salvaged = _salvage_truncated(text)
    if salvaged:
        found.append(salvaged)
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        found.append(text[start:end + 1])
    return found


def _salvage_truncated(text: str) -> str:
    """Close an array the model was cut off in the middle of writing.

    This is the most expensive failure the scriptwriter has, and the one most
    likely to happen on exactly the batches worth having: five scripts is a
    long answer, a long answer is the one that hits the output token limit,
    and the result is four perfectly good scripts thrown away because the
    fifth stops mid-sentence and the whole thing fails to parse.

    So: walk the array, remember where each complete element ended, and take
    everything up to the last one. Nothing is invented - a half-written
    script is dropped, not guessed at.
    """
    start = text.find("[")
    if start == -1:
        return ""

    depth = 0
    in_string = False
    escaped = False
    last_complete = -1

    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
            if depth == 1:
                # An element of the outer array just closed.
                last_complete = index
            elif depth == 0:
                return ""            # it was complete after all; nothing to do

    if last_complete == -1:
        return ""
    return text[start:last_complete + 1] + "]"
