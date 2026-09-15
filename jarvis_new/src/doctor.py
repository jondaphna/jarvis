"""Check everything, and say plainly what is wrong.

Written because two things broke on a machine I can't see - memory that
wouldn't carry over and a settings panel that did nothing - and neither left a
message anywhere. Guessing across a chat window is slow. This looks at every
part that can fail and prints what it found.

  butler-doctor.bat
"""

from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

OK, BAD, WARN = "  [ok]  ", "  [--]  ", "  [??]  "
problems: list[str] = []


def fail(line: str, fix: str) -> None:
    print(BAD + line)
    problems.append(fix)


def opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def check_env() -> None:
    print("\nKeys and configuration")
    agent_env = HERE.parent / ".env.local"
    web_env = HERE.parent / "frontend" / ".env.local"

    if not agent_env.exists():
        fail(f"{agent_env.name} is missing", "Run butler-setup.bat")
        return
    print(OK + f"{agent_env.name} exists")

    values = {}
    for line in agent_env.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, _, v = line.partition("=")
            values[k.strip()] = v.strip()

    for name in ("GOOGLE_API_KEY", "LIVEKIT_URL", "LIVEKIT_API_KEY",
                 "LIVEKIT_API_SECRET"):
        value = values.get(name, "")
        if not value:
            fail(f"{name} is not set", "Run butler-keys.bat")
        elif any(c.isspace() for c in value):
            fail(f"{name} has a space in it - something extra got pasted in",
                 "Run butler-keys.bat and paste only the key")
        else:
            print(OK + f"{name} looks well formed")

    # The mismatch that makes the page connect and nothing ever speak.
    if not web_env.exists():
        fail("frontend/.env.local is missing", "Run butler-setup.bat")
        return
    web = dict(
        (line.split("=", 1)[0].strip(), line.split("=", 1)[1].strip())
        for line in web_env.read_text(encoding="utf-8").splitlines()
        if "=" in line and not line.strip().startswith("#"))

    agent_name = ""
    for line in (HERE / "agent.py").read_text(encoding="utf-8").splitlines():
        if "agent_name=" in line:
            agent_name = line.split('agent_name="')[1].split('"')[0]
            break
    if web.get("AGENT_NAME") == agent_name:
        print(OK + f"AGENT_NAME matches the agent ({agent_name})")
    else:
        fail(f"AGENT_NAME is {web.get('AGENT_NAME')!r} but the agent registers "
             f"as {agent_name!r} - the page will connect and nobody will speak",
             "Run butler-keys.bat, which writes both sides")


def check_memory() -> None:
    print("\nMemory")
    try:
        from jarvis import paths
        from jarvis.core.memory import Memory
    except Exception as exc:
        fail(f"can't load the memory module: {exc}",
             "The jarvis folder may be missing. Try: git pull")
        return

    print(OK + f"database: {paths.DB_FILE}")
    if not paths.DB_FILE.exists():
        print(WARN + "no database yet - it appears the first time you talk")
        return

    try:
        db = Memory()
        facts = db.all_facts()
        conversations = db.recent_conversations(limit=50)
    except Exception as exc:
        fail(f"can't read the database: {exc}", "Tell me this error")
        return

    print(OK + f"{len(conversations)} conversation(s) recorded")
    print(OK + f"{len(facts)} fact(s) remembered")

    turns = 0
    for conversation in conversations[:5]:
        try:
            turns += len(db.history(conversation["id"], limit=200))
        except Exception:
            pass
    print(OK + f"{turns} turn(s) stored in the last few conversations")

    if conversations and turns == 0:
        fail("conversations exist but no words were saved in them",
             "Tell me this - it means the agent isn't recording what is said")

    if facts:
        print("\n  What it knows about you:")
        for fact in facts[:10]:
            print(f"      {fact['key']}: {str(fact['value'])[:60]}")
    else:
        print(WARN + "no saved facts yet - it builds these as you talk")

    try:
        sys.path.insert(0, str(HERE))
        from brain_memory import JarvisMemory

        block = JarvisMemory().context_block()
        if block.strip():
            print(OK + f"it will start the next call knowing "
                       f"{len(block.splitlines())} lines of context")
        else:
            print(WARN + "nothing to carry into the next call yet")
    except Exception as exc:
        fail(f"the memory layer itself failed: {exc}", "Tell me this error")


def check_services() -> None:
    print("\nServices")
    from jarvis import paths

    token = paths.ROOT / "control.token"
    print((OK if token.exists() else WARN) +
          f"settings token: {'present' if token.exists() else 'not created yet'}")

    try:
        with opener().open("http://127.0.0.1:8765/api/state", timeout=2):
            pass
        print(OK + "settings service is answering")
    except Exception as exc:
        if "403" in str(exc):
            print(OK + "settings service is running (refused without a token, "
                       "which is correct)")
        else:
            fail("settings service is not running - this is why the settings "
                 "panel does nothing",
                 "Start butler-agent.bat; it runs the settings service too")

    try:
        with opener().open("http://127.0.0.1:3000/", timeout=3):
            pass
        print(OK + "web app is up on http://localhost:3000")
    except Exception:
        print(WARN + "web app is not running (start butler-web.bat)")


def check_browser() -> None:
    print("\nBrowser")
    sys.path.insert(0, str(REPO))
    try:
        from chrome_finder import find_chrome
    except Exception:
        print(WARN + "couldn't check for Chrome")
        return
    chrome = find_chrome()
    if chrome:
        print(OK + f"Chrome: {chrome}")
    else:
        fail("Chrome not found - the voice client does not work in Edge",
             "Install Chrome from google.com/chrome")


def main() -> int:
    print("\n  Jarvis - checking everything\n" + "  " + "-" * 44)
    for check in (check_env, check_memory, check_services, check_browser):
        try:
            check()
        except Exception as exc:
            fail(f"{check.__name__} crashed: {exc}", "Tell me this error")

    print("\n" + "  " + "-" * 44)
    if not problems:
        print("\n  Everything checks out.\n")
        return 0
    print(f"\n  {len(problems)} thing(s) to fix:\n")
    for i, fix in enumerate(dict.fromkeys(problems), 1):
        print(f"    {i}. {fix}")
    print()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
