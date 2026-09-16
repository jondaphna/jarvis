"""The parts of Jarvis that are yours rather than the template's.

Three things get folded into the agent's instructions at the start of every
call:

* **What it knows about you** - facts carried over from past conversations.
* **Your custom commands** - "from now on when I say X, say Y", kept in the
  same rule book the original JARVIS uses, so a rule you set anywhere applies
  everywhere.
* **The wake phrase** - "hey Jarvis" and the instant answer it should get.

The wake phrase is handled as an instruction rather than as code that races the
model. In a realtime call the model already has the audio and is already
answering; a second greeting spoken locally at the same time gives you two
voices talking over each other. A hard rule in the prompt gets the same
behaviour with one voice, and the model's reply in realtime mode lands in well
under a second.

Everything here is optional. If the settings file or rule book can't be read,
the call still happens - just without your name in it.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]

DEFAULT_WAKE_PHRASE = "hey jarvis"
DEFAULT_WAKE_REPLY = "Hey - what's up? How can I help?"

#: How a custom command is written out so the model will actually follow it.
#: The Commands tab creates "reply" rules, which used to be rendered nowhere
#: at all: the voice agent only put "instruct" rules in the prompt, and unlike
#: the terminal JARVIS it never matches rules locally. Every command written in
#: the settings panel was therefore invisible to the thing meant to obey it.
_RULE_SHAPES = {
    "reply": ('- When they say "{trigger}", answer with exactly this and '
              'nothing else: "{response}"'),
    "run": ('- When they say "{trigger}", treat it as if they had said: '
            '"{response}" - then do it, immediately.'),
    "instruct": "- {response}",
}


def _repo_on_path() -> None:
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))


def load_settings() -> Any:
    """The shared settings file, or None."""
    _repo_on_path()
    try:
        from jarvis.config import Settings

        return Settings.load()
    except Exception:
        return None


def load_rules(settings: Any) -> Any:
    """The shared custom-command rule book, or None."""
    if settings is None:
        return None
    _repo_on_path()
    try:
        from jarvis.core.rules import RuleBook

        return RuleBook(settings)
    except Exception:
        return None


def wake_block(settings: Any) -> str:
    """The hard rule that makes "hey Jarvis" answer instantly."""
    phrase, reply = DEFAULT_WAKE_PHRASE, DEFAULT_WAKE_REPLY
    if settings is not None:
        try:
            phrase = str(settings.get("wake.phrase", phrase) or phrase)
            reply = str(settings.get("wake.reply", reply) or reply)
        except Exception:
            pass

    return (
        "# Wake phrase\n"
        f'- When they say "{phrase}" on its own - just the phrase, nothing '
        "else - answer immediately with exactly this and nothing more:\n"
        f'  "{reply}"\n'
        "- Say it straight away. Don't preface it, don't add to it, and don't "
        "call any tool first.\n"
        f'- If they say "{phrase}" followed by an actual request, skip the '
        "greeting entirely and just do what they asked."
    )


def _setting(settings: Any, key: str, default: str = "") -> str:
    if settings is None:
        return default
    try:
        return str(settings.get(key, default) or default).strip()
    except Exception:
        return default


def rule_lines(rules: Any) -> list[str]:
    """Every enabled custom command, written as an instruction to follow.

    All three kinds, regardless of which one the panel happened to create.
    Guessing which kinds matter is how the last set of commands went missing.
    """
    if rules is None:
        return []
    try:
        everything = list(rules.all())
    except Exception:
        return []

    lines: list[str] = []
    for rule in everything:
        if not getattr(rule, "enabled", True):
            continue
        response = str(getattr(rule, "response", "") or "").strip()
        if not response:
            continue
        trigger = str(getattr(rule, "trigger", "") or "").strip()
        kind = str(getattr(rule, "kind", "reply") or "reply")
        shape = _RULE_SHAPES.get(kind, _RULE_SHAPES["instruct"])
        if not trigger and kind != "instruct":
            shape = _RULE_SHAPES["instruct"]
        lines.append(shape.format(trigger=trigger, response=response))
    return lines


def your_orders(settings: Any = None, rules: Any = None) -> str:
    """Everything the user wrote themselves, as one block that outranks the rest.

    Position and wording are both load-bearing. This goes at the very top,
    ahead of the template's own personality and its "hard rules", because the
    template is a starting point and this is the person who owns the thing
    saying what they want. And it says "override" rather than "take priority
    over your general style guidance", because a rule like "always check my
    calendar first" is behaviour, not style, and the narrower phrasing gave the
    model room to decide it did not apply.
    """
    standing = _setting(settings, "persona.instructions")
    forbidden = _setting(settings, "persona.never")
    commands = rule_lines(rules)

    if not (standing or forbidden or commands):
        return ""

    parts = [
        "# YOUR STANDING ORDERS - from the person you work for\n"
        "These are not suggestions and not style notes. They come from the "
        "person you work for, they apply to every single conversation, and "
        "they OVERRIDE everything else in these instructions - the "
        "personality, the examples, the hard rules, all of it. Follow every "
        "one of them, every time, without being reminded and without being "
        "asked twice. If one of them conflicts with anything else you were "
        "told, this section wins."
    ]
    if standing:
        parts.append("## What they always want\n" + standing)
    if forbidden:
        parts.append(
            "## What they never want\n"
            "Absolute. If a request would require one of these, say you can't "
            "and stop - don't look for a way around it.\n\n" + forbidden)
    if commands:
        parts.append(
            "## Their own commands\n"
            "They set these up themselves and they expect them to work every "
            "time.\n\n" + "\n".join(commands))
    return "\n\n".join(parts)


def closing_reminder(settings: Any = None, rules: Any = None) -> str:
    """The last word in the prompt, when there is anything to have the last
    word about.

    The end of a long prompt carries weight, and without this the last word
    was the template's joke lines - which are the only place it says "you
    **must**".
    """
    if not your_orders(settings, rules):
        return ""
    return ("# Before you answer anything\n"
            "Re-read YOUR STANDING ORDERS at the top. They are the user's own "
            "instructions, they outrank everything between here and there, and "
            "not following one of them is a failure even if everything else "
            "went well.")


def fingerprint(settings: Any = None, rules: Any = None) -> str:
    """A short hash of everything the user controls, for spotting an edit.

    Used to notice a change without a restart. Hashing what the model would
    actually be told - rather than a file modification time - means a save
    that changed nothing doesn't interrupt a conversation to say nothing new.
    """
    material = "\n".join((your_orders(settings, rules),
                           _setting(settings, "wake.phrase"),
                           _setting(settings, "wake.reply")))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def assemble(template: str, memory: Any = None, settings: Any = None,
             rules: Any = None, extra: tuple[str, ...] = ()) -> str:
    """The whole system prompt, in the order that makes the user's rules win.

    Their orders first, the template second, everything situational in the
    middle, and a one-line reminder of their orders last.
    """
    parts = (
        your_orders(settings, rules),
        template,
        extra_instructions(memory, settings, rules),
        *extra,
        closing_reminder(settings, rules),
    )
    return "\n\n".join(part for part in parts if part and part.strip())


def extra_instructions(memory: Any = None, settings: Any = None,
                       rules: Any = None) -> str:
    """Everything personal, appended to the template's own instructions."""
    blocks: list[str] = []

    blocks.append(wake_block(settings))

    # The user's own instructions and commands are NOT here. They go at the
    # very top of the prompt, ahead of the template, via `your_orders` - which
    # is the whole point of that function. Repeating them here would spend
    # tokens saying the same thing twice and weaken both copies.

    if memory is not None and getattr(memory, "available", False):
        try:
            context = memory.context_block()
            if context:
                blocks.append(context)
        except Exception:
            pass

    blocks.append(
        "# Remembering\n"
        "- When they tell you something durable about themselves, their work "
        "or how they want you to behave, save it with the remember tool. Do it "
        "quietly, without saying that you're doing it.\n"
        "- If they refer to something from a past conversation you can't see, "
        "search for it rather than admitting you've forgotten."
    )

    return "\n\n".join(b for b in blocks if b.strip())
