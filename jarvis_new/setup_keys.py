"""Write the .env.local files this project needs.

Two files have to agree or nothing happens:

  jarvis_new/.env.local           the agent  - LiveKit + Google
  jarvis_new/frontend/.env.local  the web UI - LiveKit + AGENT_NAME

AGENT_NAME is the one people lose an evening to. agent.py registers as
`my-agent`, which is *explicit dispatch*: LiveKit will only send it to a room
that asked for it by name. If the frontend doesn't ask, the page connects, the
microphone works, the agent sits idle, and nothing is ever said.

Keys come from the JARVIS vault when this repo already has them, and from you
when it doesn't. Run it as often as you like; it only rewrites what changed.
"""

from __future__ import annotations

import sys
from getpass import getpass
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent

#: agent.py registers under this name, so the frontend must dispatch to it.
AGENT_NAME = "my-agent"

WANTED = [
    ("LIVEKIT_URL", "LiveKit project URL (wss://....livekit.cloud)", False),
    ("LIVEKIT_API_KEY", "LiveKit API key", False),
    ("LIVEKIT_API_SECRET", "LiveKit API secret", True),
    ("GOOGLE_API_KEY", "Google AI key (aistudio.google.com)", True),
]

#: The vault calls the Gemini key something else.
ALIASES = {"GOOGLE_API_KEY": ("GOOGLE_API_KEY", "GEMINI_API_KEY")}


def from_vault() -> dict[str, str]:
    """Whatever the existing JARVIS install already knows. Never fatal."""
    sys.path.insert(0, str(REPO))
    try:
        from jarvis.config import Config
    except Exception as exc:
        print(f"  (no JARVIS vault available here: {exc})")
        return {}

    try:
        config = Config()
        if config.vault.needs_passphrase:
            config.vault.unlock(getpass("  JARVIS vault passphrase: "))
        found = {}
        for name, _, _ in WANTED:
            for candidate in ALIASES.get(name, (name,)):
                value = config.key(candidate)
                if value:
                    found[name] = str(value)
                    break
        if found:
            print(f"  Found in your vault: {', '.join(sorted(found))}")
        return found
    except Exception as exc:
        print(f"  (couldn't read the vault: {exc})")
        return {}


def normalise_url(url: str) -> str:
    """The project page shows an https:// address; LiveKit speaks WebSocket."""
    url = url.strip().rstrip("/")
    if url.startswith("https://"):
        return "wss://" + url[len("https://"):]
    if url.startswith("http://"):
        return "ws://" + url[len("http://"):]
    if url and not url.startswith(("ws://", "wss://")):
        return "wss://" + url
    return url


def existing(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


def main() -> int:
    print("\n  Setting up Jarvis\n")

    values = existing(HERE / ".env.local")
    for key, value in from_vault().items():
        values.setdefault(key, value)

    for name, label, secret in WANTED:
        if values.get(name):
            shown = "*" * 8 if secret else values[name]
            print(f"  {name} = {shown}")
            continue
        print(f"\n  {label}")
        entered = (getpass("  > ") if secret else input("  > ")).strip()
        if not entered:
            print(f"\n  {name} is required - nothing will work without it.")
            return 1
        values[name] = entered

    values["LIVEKIT_URL"] = normalise_url(values["LIVEKIT_URL"])

    agent_env = "\n".join(
        f"{name}={values[name]}" for name, _, _ in WANTED) + "\n"
    (HERE / ".env.local").write_text(agent_env, encoding="utf-8")

    web_env = (
        f"LIVEKIT_URL={values['LIVEKIT_URL']}\n"
        f"LIVEKIT_API_KEY={values['LIVEKIT_API_KEY']}\n"
        f"LIVEKIT_API_SECRET={values['LIVEKIT_API_SECRET']}\n"
        # Must match agent.py's agent_name, or the agent never joins.
        f"AGENT_NAME={AGENT_NAME}\n"
    )
    (HERE / "frontend" / ".env.local").write_text(web_env, encoding="utf-8")

    print("\n  Written:")
    print(f"    {HERE / '.env.local'}")
    print(f"    {HERE / 'frontend' / '.env.local'}")
    print(f"\n  Agent dispatch name: {AGENT_NAME} (both sides agree)\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
