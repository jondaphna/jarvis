"""Check everything, and say plainly what is wrong.

Written because two things broke on a machine I can't see - memory that
wouldn't carry over and a settings panel that did nothing - and neither left a
message anywhere. Guessing across a chat window is slow. This looks at every
part that can fail and prints what it found.

  butler-doctor.bat
"""

from __future__ import annotations

import contextlib
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
    web = {
        line.split("=", 1)[0].strip(): line.split("=", 1)[1].strip()
        for line in web_env.read_text(encoding="utf-8").splitlines()
        if "=" in line and not line.strip().startswith("#")
    }

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
        with contextlib.suppress(Exception):
            turns += len(db.history(conversation["id"], limit=200))
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


def check_thinking() -> None:
    print("\nThe thinking half")
    sys.path.insert(0, str(HERE))
    try:
        import thinker
    except Exception as exc:
        fail(f"can't load the thinking module: {exc}", "Tell me this error")
        return

    if thinker.available():
        print(OK + f"thinking with {thinker.describe()}")
    else:
        print(WARN + "no brain is reachable, so it answers everything off the cuff")
        problems.append(
            "Add your Google key so thinking works - it's free: jarvis keys "
            "set GEMINI_API_KEY (or the AI tab in settings)")

    try:
        from jarvis.config import Settings

        settings = Settings.load()
        chosen = str(settings.get("thinking.model", thinker.DEFAULT_MODEL))
        allowed = thinker.paid_allowed(settings)
    except Exception:
        return

    print(OK + f"setting: {chosen}"
               f" at {settings.get('thinking.effort', thinker.DEFAULT_EFFORT)} effort")

    if not allowed:
        print(OK + "paid AI is off - nothing here can spend money")
        if thinker.is_paid(chosen):
            print(WARN + f"{chosen} costs money and paid AI is off, so a free "
                         "brain answers instead")
            problems.append(
                f"Either switch paid AI on in the AI tab, or pick a free "
                f"model - {chosen} is currently being ignored")
    else:
        print(WARN + "paid AI is ON - hard questions may be billed")
        if thinker.is_paid(chosen) and not thinker._api_key():
            fail("paid AI is on but no Claude key is stored",
                 "Add a Claude key in the AI tab, or switch paid AI back off")


def check_learning() -> None:
    """What it has been taught, and whether it will reach the next call."""
    print("\nWhat you have taught it")
    sys.path.insert(0, str(HERE))
    try:
        from learning import Lessons
    except Exception as exc:
        fail(f"can't load the learning module: {exc}", "Tell me this error")
        return

    lessons = Lessons()
    if not lessons.available:
        fail("the lesson store isn't reachable", "Tell me this error")
        return

    try:
        rows = lessons.db.lessons(limit=200)
    except Exception as exc:
        fail(f"can't read the lessons: {exc}", "Tell me this error")
        return

    if not rows:
        print(WARN + "nothing taught yet - say \"when I say X, do Y\" and it "
                     "will remember")
        return

    taught = [r for r in rows if r["source"] in ("taught", "corrected")]
    watched = [r for r in rows if r["source"] == "watched"]
    print(OK + f"{len(taught)} taught by you, {len(watched)} picked up by watching")

    block = lessons.block()
    if block.strip():
        print(OK + f"{block.count(chr(10) + '- ') + 1} of them go into the "
                   "next call's instructions")
    else:
        fail("lessons exist but none reach the prompt",
             "Tell me this - it means teaching it does nothing")

    for row in rows[:6]:
        print(f"      \"{row['said'] or row['trigger']}\" -> {str(row['steps'])[:58]}")


def check_long_conversations() -> None:
    """The reason it used to go quiet partway through."""
    print("\nStaying alive in a long conversation")
    try:
        source = (HERE / "agent.py").read_text(encoding="utf-8")
    except OSError as exc:
        fail(f"can't read agent.py: {exc}", "Try: git pull")
        return

    if "session_resumption=" in source:
        print(OK + "session resumption on - a dropped session comes back")
    else:
        fail("session resumption is off - the conversation will die after a "
             "while and lose every tool",
             "Run: git pull")

    if "context_window_compression=" in source:
        print(OK + "context compression on - long conversations don't fill up")
    else:
        fail("context compression is off", "Run: git pull")


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
        fail("Chrome not found - Jarvis needs it to open and control sites",
             "Install Chrome from google.com/chrome")
        return

    try:
        sys.path.insert(0, str(HERE))
        import live_browser

        print(OK + f"its browser profile: {live_browser.profile_dir()}")
        if live_browser.port_is_open():
            print(OK + "its Chrome window is running and attachable")
        else:
            print(WARN + "its Chrome window isn't open yet (normal when idle)")
        copied = ", ".join(live_browser.SESSION_FILES)
        print(OK + f"copies from your Chrome: {copied} - never saved passwords")
    except Exception as exc:
        fail(f"browser module problem: {exc}", "Tell me this error")


def check_settings_routes() -> None:
    """Five ways in, because a floating button can always be covered."""
    print("\nSettings")
    routes = 0
    if (REPO / "butler-settings.bat").exists():
        print(OK + "butler-settings.bat")
        routes += 1
    if (REPO / "jarvis_new" / "frontend" / "app" / "settings" / "page.tsx").exists():
        print(OK + "its own page at localhost:3000/settings")
        routes += 1
    panel = REPO / "jarvis_new" / "frontend" / "components" / "app" / "control-panel.tsx"
    try:
        text = panel.read_text(encoding="utf-8")
        if "z-[120]" in text:
            print(OK + "gear button, raised above the call screen")
            routes += 1
        if "shiftKey" in text:
            print(OK + "keyboard: Ctrl+Shift+S")
            routes += 1
    except OSError:
        pass
    try:
        if "open_settings" in (REPO / "desktop_orb.py").read_text(encoding="utf-8"):
            print(OK + "right-click the orb")
            routes += 1
    except OSError:
        pass

    if routes < 2:
        fail("almost no way into settings", "Run: git pull")
    else:
        print(OK + f"{routes} independent ways in")


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


def main() -> int:
    print("\n  Jarvis - checking everything\n" + "  " + "-" * 44)
    for check in (check_env, check_thinking, check_long_conversations,
                  check_memory, check_learning, check_browser, check_services,
                  check_settings_routes):
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
