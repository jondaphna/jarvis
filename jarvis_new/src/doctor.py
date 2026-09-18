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


def check_content_engine() -> None:
    """How far the business half can actually get, and what stops it.

    Worth its own section because "why hasn't it posted anything" has a
    different answer every week - a switch, a key, a missing program - and
    each one is invisible until something names it.
    """
    print("\nThe content engine")
    sys.path.insert(0, str(HERE))
    try:
        import permissions
        from content import pipeline, styles
    except Exception as exc:
        fail(f"can't load the content engine: {exc}", "Tell me this error")
        return

    if not permissions.allowed("content"):
        print(WARN + "the content engine is switched off, so the voice "
                     "can't reach it")
        problems.append(
            "Switch the content engine on in the Permissions tab if you want "
            "to ask for Reel scripts by voice")

    state = pipeline.readiness()
    for stage in state["stages"]:
        if stage["ready"]:
            print(OK + f"{stage['key']}: ready")
        else:
            print(WARN + f"{stage['key']}: {stage['blocker']}")

    profile = styles.get()
    if profile.placeholder:
        print(WARN + "the house style is still the shipped placeholder")
        problems.append(
            f"Describe your reference reels in {styles.path()} - until then "
            f"every script follows generic best practice rather than your look")
    else:
        print(OK + f"house style: {profile.name}")


def check_wiring() -> None:
    """Are the pieces actually joined up?

    Almost every real failure here has been a joint rather than a part: a
    button running a renamed script, a settings panel calling a route the
    service does not have, a prompt naming a tool that was consolidated away.
    None of those raise. They present as "it just doesn't do anything".
    """
    import re

    print("\nWiring")
    root = HERE.parents[1]
    panel = (root / "jarvis_new" / "frontend" / "components" / "app"
             / "control-panel.tsx")

    # -- the buttons -------------------------------------------------------- #
    launchers = sorted(root.glob("butler-*.bat"))
    broken = []
    for launcher in launchers:
        text = launcher.read_text(encoding="utf-8", errors="ignore")
        for target in re.findall(r"(?:python|uv run)\s+(?:--\S+\s+)*"
                                 r"([\w./\\-]+\.py)", text):
            path = target.replace("\\", "/")
            if not any((base / path).exists() for base in (root, root / "jarvis_new")):
                broken.append(f"{launcher.name} runs {target}, which isn't there")
    if broken:
        for line in broken:
            fail(line, "Tell me this - that button does nothing")
    else:
        print(OK + f"{len(launchers)} button(s), every one of them runs a real file")

    # -- the orb ------------------------------------------------------------ #
    orb = root / "desktop_orb.py"
    if orb.exists():
        text = orb.read_text(encoding="utf-8", errors="ignore")
        missing = [name for name in
                   sorted(set(re.findall(r'["\'](butler-[\w-]+\.bat)["\']', text)))
                   if not (root / name).exists()]
        if missing:
            fail(f"the orb launches {', '.join(missing)}, which are missing",
                 "Tell me this - those orb menu items do nothing")
        else:
            print(OK + "every orb menu item launches something real")

    # -- the settings panel and the service --------------------------------- #
    if not panel.exists():
        print(WARN + "the settings panel source isn't here to check")
        return
    panel_text = panel.read_text(encoding="utf-8")
    service = (HERE / "control_api.py").read_text(encoding="utf-8")
    routes = {p.strip("/").split("/")[0] for p in
              re.findall(r"api\(\s*['\"`]([^'\"`$]+)", panel_text) if p.strip("/")}
    absent = sorted(r for r in routes if f'head == "{r}"' not in service)
    if absent:
        fail(f"the panel calls {', '.join(absent)}, which the service can't answer",
             "Tell me this - those parts of the settings panel are dead")
    else:
        print(OK + f"{len(routes)} settings route(s), all answered")

    try:
        import control_api

        state = control_api.Control().state()
        wanted = set(re.findall(r"state\.(\w+)", panel_text)) - {"settings"}
        gone = sorted(w for w in wanted if w not in state)
        if gone:
            fail(f"the panel expects {', '.join(gone)}, which isn't sent",
                 "Tell me this - those tabs will look empty")
        else:
            print(OK + "the panel and the service agree on every field")
    except Exception as exc:
        fail(f"couldn't ask the service what it sends: {exc}", "Tell me this error")

    # -- the prompt and the tools ------------------------------------------- #
    try:
        import permissions
        import prompts

        named = set(re.findall(r"\b(?:use |calls )([a-z_]+_[a-z_]+)\b",
                               prompts.AGENT_INSTRUCTIONS))
        unreal = sorted(n for n in named if n not in permissions.GOVERNED)
        if unreal:
            fail(f"the prompt tells it to use {', '.join(unreal)}, which don't exist",
                 "Tell me this - it will try and fail")
        else:
            print(OK + f"{len(permissions.GOVERNED)} tool(s), every one named in "
                       "the prompt is real")
    except Exception as exc:
        fail(f"couldn't check the prompt: {exc}", "Tell me this error")


def check_your_rules() -> None:
    """Your own instructions and commands, and whether they reach the model.

    The bug this exists to catch: commands written in the settings panel are
    created as "reply" rules, and for a while the voice agent rendered only
    "instruct" rules into its prompt. Every command written in the panel was
    invisible - which looks exactly like being ignored.
    """
    print("\nYour rules and commands")
    sys.path.insert(0, str(HERE))
    try:
        import personalise

        settings = personalise.load_settings()
        book = personalise.load_rules(settings)
    except Exception as exc:
        fail(f"can't load your rules: {exc}", "Tell me this error")
        return

    standing = str((settings.get("persona.instructions", "") if settings else "") or "").strip()
    forbidden = str((settings.get("persona.never", "") if settings else "") or "").strip()
    try:
        commands = [r for r in (book.all() if book else []) if r.enabled]
    except Exception:
        commands = []

    if not (standing or forbidden or commands):
        print(WARN + "you haven't written any rules or commands yet")
        return

    if standing:
        print(OK + f"{len(standing.splitlines())} standing instruction(s)")
    if forbidden:
        print(OK + f"{len(forbidden.splitlines())} thing(s) it must never do")
    if commands:
        kinds: dict[str, int] = {}
        for rule in commands:
            kinds[rule.kind] = kinds.get(rule.kind, 0) + 1
        print(OK + f"{len(commands)} command(s): "
                   + ", ".join(f"{n} {k}" for k, n in sorted(kinds.items())))

    block = personalise.your_orders(settings, book)
    if not block.strip():
        fail("you have rules but none of them reach Jarvis",
             "Tell me this - it means everything you wrote is being ignored")
        return

    missing = [r for r in commands if r.response.strip()
               and r.response.strip() not in block]
    if missing:
        fail(f"{len(missing)} command(s) never reach Jarvis",
             "Tell me this - those commands are invisible to it")
    else:
        print(OK + "every rule and command reaches Jarvis, ahead of everything else")

    full = personalise.assemble("TEMPLATE", None, settings, book)
    if full.index(block) != 0:
        fail("your rules are not first in the instructions",
             "Tell me this - buried rules get overruled by the template")
    else:
        print(OK + "they are the first thing it reads, and the last thing it is reminded of")


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
    reaching = sum(1 for line in block.splitlines() if line.startswith("- "))
    if reaching:
        print(OK + f"{reaching} of them go into the next call's instructions")
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
                  check_memory, check_your_rules, check_learning,
                  check_browser, check_content_engine, check_wiring,
                  check_services, check_settings_routes):
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
