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

import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]

DEFAULT_WAKE_PHRASE = "hey jarvis"
DEFAULT_WAKE_REPLY = "Hey - what's up? How can I help?"


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


def extra_instructions(memory: Any = None, settings: Any = None,
                       rules: Any = None) -> str:
    """Everything personal, appended to the template's own instructions."""
    blocks: list[str] = []

    blocks.append(wake_block(settings))

    # Whatever the user wrote in the interface. Placed early and stated
    # plainly, because these are their instructions about their own
    # assistant and should outrank the template's defaults.
    if settings is not None:
        try:
            standing = str(settings.get("persona.instructions", "") or "").strip()
            forbidden = str(settings.get("persona.never", "") or "").strip()
        except Exception:
            standing = forbidden = ""
        if standing:
            blocks.append(
                "# Standing instructions from the user\n"
                "These come directly from them and take priority over your "
                "general style guidance.\n\n" + standing)
        if forbidden:
            blocks.append(
                "# Things you must never do\n"
                "Absolute. If a request would require one of these, say you "
                "can't and stop - don't look for a way around it.\n\n"
                + forbidden)

    if rules is not None:
        try:
            standing = rules.instruction_block()
            if standing:
                blocks.append(standing)
        except Exception:
            pass

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
