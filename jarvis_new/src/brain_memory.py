"""What Jarvis remembers between one conversation and the next.

Everything lands in the same SQLite file the original JARVIS uses
(%APPDATA%\\JARVIS\\jarvis.db on Windows), so both halves share one brain
rather than each keeping its own half-picture of you.

Two different things are stored, and the difference matters:

* **Facts** - small, durable things worth carrying into every future
  conversation: your name, what you're working on, how you like to be spoken
  to. These are loaded into the agent's instructions at the start of every
  call, which is what makes it *know* you rather than merely having notes it
  could look up.
* **Transcripts** - every turn of every conversation. Too big to load wholesale,
  so they're searchable on demand instead.

Nothing here is allowed to break a call. A missing database, a locked file, a
corrupt row: memory degrades to "remembers nothing this session" and the voice
keeps working. Losing your notes is annoying; losing your assistant mid-sentence
is not acceptable.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from livekit.agents import RunContext, function_tool
from livekit.agents.llm import ToolError

#: The repo root, so `jarvis` (the original package) is importable from here.
REPO = Path(__file__).resolve().parents[2]

_WHY_UNAVAILABLE = ""


def _open_memory() -> Any:
    """The shared memory database, or None with a reason logged."""
    global _WHY_UNAVAILABLE
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    try:
        from jarvis.core.memory import Memory

        return Memory()
    except Exception as exc:                      # pragma: no cover - defensive
        _WHY_UNAVAILABLE = str(exc)
        return None


class JarvisMemory:
    """Facts, transcripts and the context block that makes Jarvis know you."""

    def __init__(self) -> None:
        self.db = _open_memory()
        self.conversation_id: int | None = None
        if self.db is None:
            print(f"  (memory unavailable, continuing without it: {_WHY_UNAVAILABLE})")

    @property
    def available(self) -> bool:
        return self.db is not None

    # ------------------------------------------------------------------ #
    # Recording the conversation
    # ------------------------------------------------------------------ #

    def start_conversation(self, channel: str = "voice") -> None:
        if not self.db:
            return
        try:
            self.conversation_id = self.db.start_conversation(channel)
        except Exception:
            self.conversation_id = None

    def log(self, role: str, text: str) -> None:
        """Record one turn. Silent on failure - never interrupt a call."""
        if not self.db or not text.strip():
            return
        try:
            if self.conversation_id is None:
                self.start_conversation()
            if self.conversation_id is not None:
                self.db.add_message(self.conversation_id, role, text.strip(),
                                    model="realtime")
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # What goes into the instructions
    # ------------------------------------------------------------------ #

    def context_block(self) -> str:
        """Who this person is and where you left off, for the system prompt.

        Deliberately small. The whole history would be both expensive and
        worse: a model given everything answers vaguely, while a model given
        the twenty things that matter sounds like it was paying attention.

        Two things here are hard-won. The *current* conversation is excluded -
        it was created seconds ago and including it would show the model its
        own empty room and call it history. And it reaches back through several
        past conversations rather than only the last one, because the last one
        is very often "hey Jarvis" and nothing else, which would quietly throw
        away the real conversation behind it.
        """
        if not self.db:
            return ""

        parts: list[str] = []
        try:
            facts = self.db.facts_block(limit=40)
            if facts:
                parts.append(
                    "# What you already know about the person you're talking to\n"
                    "These carry over from previous conversations. Use them "
                    "naturally - don't recite them back.\n\n" + facts)
        except Exception:
            pass

        lines = self.recent_turns(limit=24)
        if lines:
            parts.append(
                "# What you and they have said before\n"
                "From earlier conversations, oldest first. Don't bring it up "
                "unprompted, but never claim you can't remember it.\n\n"
                + "\n".join(lines))

        return "\n\n".join(parts)

    def recent_turns(self, limit: int = 24) -> list[str]:
        """The last few real exchanges, across however many conversations."""
        if not self.db:
            return []
        try:
            conversations = list(self.db.recent_conversations(limit=8))
        except Exception:
            return []
        # Sorted here rather than trusted: whether "recent" comes back newest
        # or oldest first is not something to guess at, and getting it wrong
        # means the trimming below throws away the newest turns instead of the
        # stalest ones.
        conversations.sort(key=lambda c: c.get("id") or 0, reverse=True)

        collected: list[str] = []
        for conversation in conversations:
            cid = conversation.get("id")
            if cid is None or cid == self.conversation_id:
                continue                      # skip the one happening right now
            try:
                history = self.db.history(cid, limit=limit)
            except Exception:
                continue
            block: list[str] = []
            for message in history:
                role = getattr(message, "role", "")
                content = (getattr(message, "content", "") or "").strip()
                if content:
                    who = "They" if role == "user" else "You"
                    block.append(f"{who}: {content[:200]}")
            collected = block + collected     # older conversations first
            if len(collected) >= limit:
                break
        return collected[-limit:]

    # ------------------------------------------------------------------ #
    # Tools the model can call
    # ------------------------------------------------------------------ #

    @property
    def tools(self) -> list:
        return [self.remember, self.recall, self.forget, self.search_memory]

    @function_tool()
    async def remember(self, context: RunContext, key: str, value: str) -> str:
        """Save something about the user that should persist forever.

        Use this whenever they tell you something durable about themselves,
        their work, their preferences, or how they want you to behave. Saving
        is cheap; forgetting something they told you is not. Do it silently -
        don't announce that you're writing it down.

        Args:
            key: A short lowercase label, like 'name' or 'current project'.
            value: The thing to remember, in a full sentence.
        """
        if not self.db:
            raise ToolError("Memory isn't available right now.")
        try:
            self.db.remember(key.strip().lower(), value.strip(),
                             category="personal", source="voice")
            return f"Saved: {key} = {value}"
        except Exception as exc:
            raise ToolError(f"Couldn't save that: {exc}") from exc

    @function_tool()
    async def recall(self, context: RunContext, key: str) -> str:
        """Look up one specific thing you were told before.

        Most of what you know is already in your instructions, so use this only
        for something specific you don't see there.

        Args:
            key: The label to look up, like 'name'.
        """
        if not self.db:
            raise ToolError("Memory isn't available right now.")
        value = self.db.recall(key.strip().lower())
        return value or f"Nothing stored under {key!r}."

    @function_tool()
    async def forget(self, context: RunContext, key: str) -> str:
        """Delete something you were told to remember.

        Only when they ask you to forget it.

        Args:
            key: The label to delete.
        """
        if not self.db:
            raise ToolError("Memory isn't available right now.")
        gone = self.db.forget(key.strip().lower())
        return f"Forgot {key}." if gone else f"Nothing was stored under {key!r}."

    @function_tool()
    async def search_memory(self, context: RunContext, query: str) -> str:
        """Search everything ever said in past conversations.

        Use this when they refer to something from a previous conversation that
        you can't see in your instructions - "what did we decide about the
        website", that sort of thing.

        Args:
            query: Words to look for.
        """
        if not self.db:
            raise ToolError("Memory isn't available right now.")
        try:
            hits = self.db.search_messages(query.strip(), limit=8)
        except Exception as exc:
            raise ToolError(f"Couldn't search: {exc}") from exc
        if not hits:
            return f"Nothing in our past conversations about {query!r}."
        lines = []
        for hit in hits:
            who = "They said" if hit.get("role") == "user" else "You said"
            lines.append(f"{who}: {str(hit.get('content', ''))[:220]}")
        return "\n".join(lines)
