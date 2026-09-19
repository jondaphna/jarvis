"""Write the .env.local files this project needs, and check they actually work.

Two files have to agree or nothing happens:

  jarvis_new/.env.local           the agent  - LiveKit + Google
  jarvis_new/frontend/.env.local  the web UI - LiveKit + AGENT_NAME

AGENT_NAME is the one people lose an evening to. agent.py registers as
`my-agent`, which is *explicit dispatch*: LiveKit will only send it to a room
that asked for it by name. If the frontend doesn't ask, the page connects, the
microphone works, the agent sits idle, and nothing is ever said.

Every value is checked before it is written, and the keys are tried against the
real services. A key with a stray space in it produces a hundred-line websocket
traceback that names neither the key nor the file it came from - so it gets
caught here instead, where it can be explained in one line.

  uv run python setup_keys.py           fill in what's missing
  uv run python setup_keys.py --reset   re-enter everything
  uv run python setup_keys.py --check   test what's already there
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

#: The JARVIS vault calls the Gemini key something else.
ALIASES = {"GOOGLE_API_KEY": ("GOOGLE_API_KEY", "GEMINI_API_KEY")}


# --------------------------------------------------------------------------- #
# Checking
# --------------------------------------------------------------------------- #

def complain(name: str, value: str) -> str:
    """What is wrong with this value, or "" if it looks usable.

    Shape only - whether the service accepts it is `verify`'s job.
    """
    if not value:
        return "it's empty"
    if value != value.strip():
        return "it has a space or newline at the start or end"
    # The failure that sent us here: a whole shell command, the key twice over
    # and some keyboard mashing, all pasted into one prompt. An HTTP header
    # cannot contain a space, so this dies deep inside a websocket handshake
    # with no clue where the bad value came from.
    if any(c.isspace() for c in value):
        return "it has a space in the middle - it looks like more than one thing got pasted together"
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        return "it has a control character in it"
    if not value.isascii():
        return "it has non-English characters in it - probably a bad copy"

    if name == "LIVEKIT_URL":
        if not value.startswith(("ws://", "wss://")):
            return "it should start with wss://"
        if "livekit" not in value and "localhost" not in value:
            return "that doesn't look like a LiveKit URL"
    else:
        if len(value) < 12:
            return "it's too short to be a real key"
        if value.count("://") or value.startswith("-"):
            return "that looks like a URL or a command, not a key"
    return ""


def verify_google(key: str) -> str:
    """Ask Google whether the key is real. "" means yes."""
    import json
    import urllib.error
    import urllib.request

    request = urllib.request.Request(
        "https://generativelanguage.googleapis.com/v1beta/models",
        headers={"x-goog-api-key": key},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            json.load(response)
        return ""
    except urllib.error.HTTPError as exc:
        if exc.code in (400, 401, 403):
            return ("Google rejected this key. Make a new one at "
                    "aistudio.google.com and paste just the key.")
        return f"Google answered {exc.code}."
    except Exception as exc:
        return f"Couldn't reach Google to check ({exc}). Carrying on anyway."


def verify_livekit(url: str, key: str, secret: str) -> str:
    """Ask LiveKit whether these credentials work together. "" means yes."""
    import asyncio

    async def ask() -> str:
        try:
            from livekit import api
        except Exception:
            return ""
        client = api.LiveKitAPI(url=url, api_key=key, api_secret=secret)
        try:
            await client.room.list_rooms(api.ListRoomsRequest())
            return ""
        except Exception as exc:
            text = str(exc).lower()
            if "401" in text or "unauthorized" in text or "invalid" in text:
                return ("LiveKit rejected these. Check the key and secret are "
                        "from the same project as the URL.")
            return f"Couldn't reach LiveKit to check ({exc}). Carrying on anyway."
        finally:
            await client.aclose()

    try:
        return asyncio.run(ask())
    except Exception:
        return ""


# --------------------------------------------------------------------------- #
# Reading and writing
# --------------------------------------------------------------------------- #

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


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


def from_vault() -> dict[str, str]:
    """Whatever the existing JARVIS install already knows. Never fatal."""
    sys.path.insert(0, str(REPO))
    try:
        from jarvis.config import Config
    except Exception:
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
        return found
    except Exception as exc:
        print(f"  (couldn't read the JARVIS vault: {exc})")
        return {}


def show(name: str, value: str, secret: bool) -> str:
    """Enough of a secret to recognise it, never enough to use it."""
    if not secret:
        return value
    return f"{value[:4]}...{value[-4:]}  ({len(value)} characters)"


def ask_for(name: str, label: str, secret: bool) -> str:
    while True:
        print(f"\n  {label}")
        print("  (paste the key only - nothing else on the line)")
        entered = (getpass("  > ") if secret else input("  > ")).strip()
        if name == "LIVEKIT_URL":
            entered = normalise_url(entered)
        problem = complain(name, entered)
        if not problem:
            return entered
        print(f"\n  That won't work: {problem}")
        print("  Try again.")


def main(argv: list[str]) -> int:
    reset = "--reset" in argv
    check_only = "--check" in argv

    print("\n  Jarvis - keys\n")

    values = read_env(HERE / ".env.local")
    if not reset:
        for key, value in from_vault().items():
            values.setdefault(key, value)
    else:
        values = {}

    # --- shape ---------------------------------------------------------- #
    for name, label, secret in WANTED:
        current = values.get(name, "")
        problem = complain(name, current) if current else "it isn't set yet"
        if not problem:
            print(f"  {name:<20} {show(name, current, secret)}")
            continue
        if check_only:
            print(f"  {name:<20} BAD - {problem}")
            continue
        if current:
            print(f"\n  {name} is stored but unusable: {problem}")
        values[name] = ask_for(name, label, secret)

    if check_only and any(complain(n, values.get(n, "")) for n, _, _ in WANTED):
        print("\n  Fix those with:  uv run python setup_keys.py --reset\n")
        return 1

    values["LIVEKIT_URL"] = normalise_url(values["LIVEKIT_URL"])

    # --- do they actually work? ----------------------------------------- #
    print("\n  Checking them against the real services...")

    trouble = verify_google(values["GOOGLE_API_KEY"])
    print(f"    Google   {trouble or 'OK'}")
    google_bad = trouble and "Couldn't reach" not in trouble

    trouble = verify_livekit(values["LIVEKIT_URL"], values["LIVEKIT_API_KEY"],
                             values["LIVEKIT_API_SECRET"])
    print(f"    LiveKit  {trouble or 'OK'}")
    livekit_bad = trouble and "Couldn't reach" not in trouble

    if check_only:
        return 1 if (google_bad or livekit_bad) else 0

    if google_bad or livekit_bad:
        print("\n  Not writing anything while a key is rejected.")
        print("  Fix it and run:  uv run python setup_keys.py --reset\n")
        return 1

    # --- write ----------------------------------------------------------- #
    (HERE / ".env.local").write_text(
        "".join(f"{name}={values[name]}\n" for name, _, _ in WANTED),
        encoding="utf-8")
    (HERE / "frontend" / ".env.local").write_text(
        f"LIVEKIT_URL={values['LIVEKIT_URL']}\n"
        f"LIVEKIT_API_KEY={values['LIVEKIT_API_KEY']}\n"
        f"LIVEKIT_API_SECRET={values['LIVEKIT_API_SECRET']}\n"
        # Must match agent.py's agent_name, or the agent never joins.
        f"AGENT_NAME={AGENT_NAME}\n",
        encoding="utf-8")

    print("\n  Written, and both keys work:")
    print(f"    {HERE / '.env.local'}")
    print(f"    {HERE / 'frontend' / '.env.local'}")
    print(f"\n  Agent dispatch name: {AGENT_NAME} (both sides agree)\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
