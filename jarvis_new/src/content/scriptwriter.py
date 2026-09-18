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

        prompt = self._prompt(topic, count, profile, extra)
        raw = backend.ask(SYSTEM + "\n\n" + profile.as_prompt_block(), prompt,
                          "medium")
        scripts = self._parse(raw, topic, profile.name)

        # One honest retry. A model that ignored the shape once usually obeys
        # when the problem is named; a model that ignores it twice is not
        # going to be argued into it, and pretending otherwise burns the
        # user's quota on a loop.
        if not scripts or not all(script.is_usable() for script in scripts):
            problems = self._problems(scripts, count)
            retry = (f"{prompt}\n\nYour previous answer was rejected: "
                     f"{problems}. Return ONLY the JSON array described above.")
            raw = backend.ask(SYSTEM + "\n\n" + profile.as_prompt_block(), retry,
                              "medium")
            second = self._parse(raw, topic, profile.name)
            if second and sum(s.is_usable() for s in second) >= sum(
                    s.is_usable() for s in scripts):
                scripts = second

        usable = [script for script in scripts if script.is_usable()]
        if not usable:
            raise RuntimeError(
                "The model didn't produce a usable script - "
                + (self._problems(scripts, count) or "it returned nothing"))
        return usable

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
    for opener, closer in (("[", "]"), ("{", "}")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end > start:
            found.append(text[start:end + 1])
    return found
