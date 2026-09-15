"""The JARVIS command line - everything works from here, no GUI required."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

from . import __version__, paths
from .config import KEY_SPECS, Config, Settings, Vault, VaultLocked
from .core import events
from .core.events import bus, log, setup_logging
from .core.permissions import Request, Risk

# --------------------------------------------------------------------------- #
# Terminal styling - no dependency, degrades to plain text when piped
# --------------------------------------------------------------------------- #

_COLOUR = sys.stdout.isatty() and sys.platform != "emscripten"


def _paint(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOUR else text


def cyan(t: str) -> str: return _paint(t, "96")
def dim(t: str) -> str: return _paint(t, "90")
def bold(t: str) -> str: return _paint(t, "1")
def green(t: str) -> str: return _paint(t, "92")
def yellow(t: str) -> str: return _paint(t, "93")
def red(t: str) -> str: return _paint(t, "91")


BANNER = cyan(r"""
      _   _    ____  __     __ ___  ____
     | | / \  |  _ \ \ \   / /|_ _|/ ___|
  _  | |/ _ \ | |_) | \ \ / /  | | \___ \
 | |_| / ___ \|  _ <   \ V /   | |  ___) |
  \___/_/   \_\_| \_\   \_/   |___||____/
""") + dim(f"  personal AI - v{__version__}\n")


# --------------------------------------------------------------------------- #
# Setup wizard
# --------------------------------------------------------------------------- #

def cmd_setup(args: argparse.Namespace) -> int:
    print(BANNER)
    print(bold("Setup\n"))

    settings = Settings.load()
    vault = Vault()

    if vault.needs_passphrase:
        passphrase = _prompt_passphrase("Vault passphrase: ")
        vault.unlock(passphrase)

    # --- name and workspace ------------------------------------------- #
    current_name = settings.get("user_name") or ""
    name = input(f"What should I call you? {dim(f'[{current_name}]') if current_name else ''} ").strip()
    if name:
        settings.set("user_name", name)

    workspace = settings.workspace
    print(f"\nWorkspace - the folder I can work in freely:\n  {cyan(str(workspace))}")
    answer = input("Press Enter to keep it, or type another path: ").strip()
    if answer:
        workspace = Path(answer).expanduser()
        settings.set("autonomy.workspace", str(workspace))
    workspace.mkdir(parents=True, exist_ok=True)

    # --- keys ---------------------------------------------------------- #
    print(bold("\nAPI keys"))
    print(dim("Only the first one is required. Everything else is optional - add\n"
              "them later with `jarvis keys set NAME`. Press Enter to skip any.\n"))

    updates: dict[str, str] = {}
    for spec in KEY_SPECS:
        if args.minimal and not spec.required:
            continue
        existing = vault.get(spec.name)
        marker = green(" [set]") if existing else ""
        required = red(" (required)") if spec.required else ""
        print(f"{bold(spec.label)}{required}{marker}")
        print(dim(f"  {spec.where} - {spec.free_tier}"))
        if spec.note:
            print(dim(f"  {spec.note}"))
        try:
            value = input("  key> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nStopped.")
            break
        if value:
            updates[spec.name] = value
        print()

    if updates:
        vault.set_many(updates)
        print(green(f"Stored {len(updates)} key(s), encrypted.\n"))

    # --- autonomy ------------------------------------------------------ #
    print(bold("Permissions"))
    print("Inside your workspace I work freely. Outside it, I ask.")
    print(red("Spending money, posting publicly, sending messages and changing system\n"
              "settings are blocked unless you authorise them in the request itself."))
    apps = input("\nApps I'm allowed to open (comma separated, or Enter to skip): ").strip()
    if apps:
        settings.set("autonomy.allowed_apps",
                     [a.strip().lower() for a in apps.split(",") if a.strip()])

    settings.save()

    print(green("\nSetup complete."))
    print(f"  Settings: {dim(str(paths.CONFIG_FILE))}")
    print(f"  Workspace: {dim(str(workspace))}")
    print(f"\nTry: {cyan('jarvis chat')}   or   {cyan('jarvis doctor')}")
    return 0


def _prompt_passphrase(prompt: str) -> str:
    import getpass
    try:
        return getpass.getpass(prompt)
    except (EOFError, KeyboardInterrupt):
        return ""


# --------------------------------------------------------------------------- #
# Chat
# --------------------------------------------------------------------------- #

async def _build_app(with_scheduler: bool = False):
    from .core.assistant import Assistant

    config = Config()
    if config.vault.needs_passphrase:
        config.vault.unlock(_prompt_passphrase("Vault passphrase: "))
    app = Assistant(config)
    await app.start(with_scheduler=with_scheduler)
    return app


def _install_cli_approvals(app) -> None:
    """When someone's at the keyboard, ask instead of queueing."""

    async def ask(request: Request, risk: Risk, reason: str) -> bool:
        print()
        print(yellow(f"  Permission needed: {request.describe()}"))
        print(dim(f"  {reason}"))
        try:
            answer = await asyncio.to_thread(input, "  Allow? [y/N] ")
        except (EOFError, KeyboardInterrupt):
            return False
        return answer.strip().lower() in ("y", "yes")

    app.set_approval_handler(ask)


async def _chat_loop(app, speak: bool) -> int:
    print(BANNER)
    if not app.brain.ready():
        print(red("No AI provider configured. Run `jarvis setup` and add your "
                  "Claude API key.\n"))
        return 1

    print(dim("Type what you want. 'exit' to quit, 'help' for commands.\n"))
    _install_cli_approvals(app)

    unsubscribe = bus.subscribe(_print_activity)
    try:
        while True:
            try:
                text = await asyncio.to_thread(input, cyan("you> "))
            except (EOFError, KeyboardInterrupt):
                print()
                return 0

            text = text.strip()
            if not text:
                continue
            lowered = text.lower()
            if lowered in ("exit", "quit", "bye"):
                return 0
            if lowered == "help":
                _print_chat_help()
                continue
            if lowered in ("approvals", "pending"):
                _print_approvals(app)
                continue
            if lowered.startswith("approve "):
                _decide(app, lowered.split()[1], approve=True)
                continue
            if lowered.startswith("deny "):
                _decide(app, lowered.split()[1], approve=False)
                continue

            print(bold("jarvis> "), end="", flush=True)
            printed = False

            def on_text(delta: str) -> None:
                nonlocal printed
                printed = True
                sys.stdout.write(delta)
                sys.stdout.flush()

            reply = await app.ask(text, speak=speak, on_text=on_text)
            if not printed:
                print(reply.text, end="")
            print("\n")
            if reply.cost_usd:
                print(dim(f"  ({reply.iterations} step(s), ${reply.cost_usd:.4f})\n"))
    finally:
        unsubscribe()


def _print_activity(event: events.Event) -> None:
    """Show tool use and permission decisions as they happen."""
    if event.kind == events.TOOL_CALL:
        print(dim(f"\n  · {event.message}"))
    elif event.kind == events.PERMISSION and event.data.get("decision") != "allow":
        print(yellow(f"\n  ! {event.message}"))
    elif event.kind in (events.MISSION_START, events.MISSION_DONE, events.MISSION_FAILED):
        print(cyan(f"\n  {event.message}"))


def _print_chat_help() -> None:
    print(dim("""
  Things to try:
    "what can you do?"                       - JARVIS explains itself
    "open chrome and search for X"           - it drives the machine
    "you can use Photoshop"                  - widens the app allowlist
    "make a mission that does X every day"   - it writes the mission
    "what needs my approval?"                - reads the queue

  Commands: approvals | approve <id> | deny <id> | help | exit
"""))


def _print_approvals(app) -> None:
    pending = app.pending_approvals()
    if not pending:
        print(dim("  Nothing waiting.\n"))
        return
    print()
    for item in pending:
        risk = red(item["risk"]) if item["risk"] == "high" else yellow(item["risk"])
        print(f"  {bold('#' + str(item['id']))} [{risk}] {item['summary']}")
        print(dim(f"      asked by {item['requested_by']} at {item['at']}"))
    print(dim("\n  approve <id>   deny <id>\n"))


def _decide(app, raw: str, approve: bool) -> None:
    try:
        approval_id = int(raw)
    except ValueError:
        print(red("  That's not an approval id.\n"))
        return
    ok = app.approve(approval_id) if approve else app.deny(approval_id)
    verb = "Approved" if approve else "Denied"
    print(green(f"  {verb} #{approval_id}.\n") if ok
          else red(f"  #{approval_id} isn't pending.\n"))


def cmd_chat(args: argparse.Namespace) -> int:
    async def run() -> int:
        app = await _build_app()
        try:
            return await _chat_loop(app, speak=args.speak)
        finally:
            app.shutdown()
    return asyncio.run(run())


def cmd_ask(args: argparse.Namespace) -> int:
    async def run() -> int:
        app = await _build_app()
        try:
            if not app.brain.ready():
                print(red("No AI provider configured. Run `jarvis setup`."))
                return 1
            _install_cli_approvals(app)
            reply = await app.ask(" ".join(args.text), speak=args.speak)
            print(reply.text)
            return 0 if reply.ok else 1
        finally:
            app.shutdown()
    return asyncio.run(run())


# --------------------------------------------------------------------------- #
# Voice
# --------------------------------------------------------------------------- #

def cmd_voice(args: argparse.Namespace) -> int:
    async def run() -> int:
        app = await _build_app(with_scheduler=not args.no_scheduler)
        try:
            print(BANNER)
            if not app.brain.ready():
                print(red("No AI provider configured. Run `jarvis setup`."))
                return 1
            if not app.listener.available():
                print(red(app.listener.why_unavailable()))
                return 1

            print(f"  Ears: {cyan(app.listener.transcriber.describe())}")
            print(f"  Voice: {cyan(app.speech.describe())}")
            word = app.settings.get("voice.wake_word", "jarvis")
            print(dim(f"\n  Say '{word}' to get my attention. Ctrl-C to stop.\n")
                  if not args.always_on else dim("\n  Always listening. Ctrl-C to stop.\n"))

            bus.subscribe(_print_voice_activity)
            await app.say("Online.")
            await app.voice_loop(wake_word=not args.always_on)
            return 0
        except KeyboardInterrupt:
            print("\nStanding by.")
            return 0
        finally:
            app.shutdown()
    return asyncio.run(run())


def _print_voice_activity(event: events.Event) -> None:
    if event.kind == events.TRANSCRIPT:
        print(cyan(f"  you> {event.message}"))
    elif event.kind == events.REPLY:
        print(bold(f"  jarvis> {event.message}\n"))
    elif event.kind == events.TOOL_CALL:
        print(dim(f"    · {event.message}"))
    elif event.kind == events.PERMISSION and event.data.get("decision") != "allow":
        print(yellow(f"    ! {event.message}"))


# --------------------------------------------------------------------------- #
# Missions
# --------------------------------------------------------------------------- #

def cmd_missions(args: argparse.Namespace) -> int:
    async def run() -> int:
        app = await _build_app()
        try:
            rows = app.scheduler.next_runs()
            if not rows:
                print(dim("No missions yet. Copy missions/TEMPLATE_MISSION.json, or "
                          "ask JARVIS to write one for you."))
                return 0
            print()
            for row in rows:
                state = green("on") if row["enabled"] else dim("off")
                running = yellow(" RUNNING") if row["running"] else ""
                print(f"  {bold(row['id']):28} [{state}] {row['name']}{running}")
                print(dim(f"      {row['steps']} steps · {row['schedule']}"
                          f"{' · next ' + row['next_run'] if row['next_run'] else ''}"
                          f"{' · last ' + str(row['last_status']) if row['last_status'] else ''}"))
                if row["authorizations"]:
                    for auth in row["authorizations"]:
                        print(yellow(f"      authorised: {auth}"))
            if app.scheduler.load_errors:
                print(red("\n  Files I couldn't load:"))
                for name, error in app.scheduler.load_errors.items():
                    print(red(f"    {name}: {error}"))
            print()
            return 0
        finally:
            app.shutdown()
    return asyncio.run(run())


def cmd_run(args: argparse.Namespace) -> int:
    async def run() -> int:
        app = await _build_app()
        try:
            mission = app.scheduler.get(args.mission)
            if mission is None:
                print(red(f"No mission called '{args.mission}'."))
                print(dim("  Run `jarvis missions` to see what's available."))
                return 1

            problems = app.scheduler.check(mission)
            if problems:
                print(yellow("Heads up:"))
                for problem in problems:
                    print(yellow(f"  - {problem}"))
                if not args.force:
                    print(dim("\nFix those, or re-run with --force."))
                    return 1

            if not args.unattended:
                _install_cli_approvals(app)
            bus.subscribe(_print_activity)

            print(cyan(f"\nRunning {mission.name}...\n"))
            result = await app.run_mission(args.mission, trigger="cli",
                                           unattended=args.unattended)
            colour = {"success": green, "partial": yellow}.get(result.status, red)
            print(colour(f"\n{result.status.upper()}: {result.summary()}"))
            print(dim(f"  took {result.seconds}s · run #{result.run_id}\n"))
            return 0 if result.status != "failed" else 1
        finally:
            app.shutdown()
    return asyncio.run(run())


# --------------------------------------------------------------------------- #
# Daemon
# --------------------------------------------------------------------------- #

def cmd_daemon(args: argparse.Namespace) -> int:
    async def run() -> int:
        app = await _build_app(with_scheduler=True)
        try:
            print(BANNER)
            rows = [r for r in app.scheduler.next_runs() if r["enabled"] and r["next_run"]]
            print(bold(f"  Scheduler running - {len(rows)} mission(s) armed\n"))
            for row in rows:
                print(f"    {row['name']:34} next {cyan(row['next_run'])}")
            print(dim("\n  Ctrl-C to stop.\n"))
            bus.subscribe(_print_activity)
            while True:
                await asyncio.sleep(3600)
        except KeyboardInterrupt:
            print("\nScheduler stopped.")
            return 0
        finally:
            app.shutdown()
    return asyncio.run(run())


# --------------------------------------------------------------------------- #
# Approvals
# --------------------------------------------------------------------------- #

def cmd_approvals(args: argparse.Namespace) -> int:
    async def run() -> int:
        app = await _build_app()
        try:
            if args.approve:
                _decide(app, args.approve, approve=True)
            elif args.deny:
                _decide(app, args.deny, approve=False)
            else:
                _print_approvals(app)
            return 0
        finally:
            app.shutdown()
    return asyncio.run(run())


# --------------------------------------------------------------------------- #
# Audio devices and voice testing
# --------------------------------------------------------------------------- #

def cmd_devices(args: argparse.Namespace) -> int:
    """List audio devices, and set which ones JARVIS uses."""
    from .core.audio import describe_selection, list_devices

    settings = Settings.load()

    if args.set_input or args.set_output:
        if args.set_input:
            settings.set("voice.input_device", args.set_input)
        if args.set_output:
            settings.set("voice.output_device", args.set_output)
        settings.save()
        print(green("\nSaved."))

    if not list_devices():
        print(red("\nNo audio devices found."))
        print(dim("  Install the audio support: pip install sounddevice soundfile numpy\n"))
        return 1

    print(bold("\nMicrophones (input)"))
    for row in list_devices("input"):
        mark = green(" (system default)") if row["default_input"] else ""
        print(f"  [{row['index']:>2}] {row['name']}{mark}")

    print(bold("\nSpeakers (output)"))
    for row in list_devices("output"):
        mark = green(" (system default)") if row["default_output"] else ""
        print(f"  [{row['index']:>2}] {row['name']}{mark}")

    print(bold("\nJARVIS is using"))
    print(f"  microphone: {cyan(describe_selection(settings.get('voice.input_device'), True))}")
    print(f"  speakers  : {cyan(describe_selection(settings.get('voice.output_device'), False))}")

    print(dim("""
  Set them by name - part of the name is enough:
    jarvis devices --set-output "NVIDIA"
    jarvis devices --set-input "Lenovo"

  Then check it actually works:
    jarvis say "testing, one two three"
    jarvis listen
"""))
    return 0


def cmd_say(args: argparse.Namespace) -> int:
    """Speak a line out loud - the fastest way to test audio output."""
    async def run() -> int:
        from .core.audio import describe_selection
        from .core.voice_out import SpeechEngine

        config = Config()
        speech = SpeechEngine(config)
        engine = speech.choose_engine()

        print(f"\n  engine  : {cyan(speech.describe())}")
        print(f"  speakers: {cyan(describe_selection(config.settings.get('voice.output_device'), False))}")

        if engine == "none":
            print(red("\n  No speech engine installed. Run: pip install edge-tts\n"))
            return 1

        text = " ".join(args.text) or "Systems online. Can you hear me?"
        print(dim(f'\n  speaking: "{text}"\n'))
        await speech.say(text)
        print(dim("  Done. Heard nothing? Try a different device:"))
        print(dim("    jarvis devices\n"))
        return 0
    return asyncio.run(run())


def cmd_listen(args: argparse.Namespace) -> int:
    """Record one sentence and print the transcript - tests the microphone."""
    async def run() -> int:
        from .core.audio import describe_selection
        from .core.voice_in import VoiceListener

        config = Config()
        listener = VoiceListener(config)

        print(f"\n  engine    : {cyan(listener.transcriber.describe())}")
        print(f"  microphone: {cyan(describe_selection(config.settings.get('voice.input_device'), True))}")

        if not listener.available():
            print(red(f"\n  {listener.why_unavailable()}\n"))
            return 1

        print(dim("\n  Loading the speech model (first run downloads it)..."))
        listener.transcriber.warm_up()
        print(green("\n  Listening - say something.\n"))

        utterance = await listener.listen(max_seconds=15, start_timeout=12)
        if not utterance:
            print(red("  I didn't hear anything."))
            print(dim("  Check the microphone with: jarvis devices\n"))
            return 1

        print(f"  {bold('heard:')} {cyan(utterance.text)}")
        print(dim(f"  ({utterance.seconds:.1f}s via {utterance.engine})\n"))
        return 0
    return asyncio.run(run())


def cmd_config(args: argparse.Namespace) -> int:
    """Read and write settings without opening the JSON file."""
    settings = Settings.load()

    if not args.key:
        import json as _json
        print(_json.dumps(settings.as_dict(), indent=2))
        return 0

    if args.value is None:
        value = settings.get(args.key)
        if value is None:
            print(red(f"No setting called '{args.key}'."))
            return 1
        print(value if isinstance(value, str) else repr(value))
        return 0

    raw = args.value
    parsed: Any = raw
    if raw.lower() in ("true", "false"):
        parsed = raw.lower() == "true"
    elif raw.replace(".", "", 1).replace("-", "", 1).isdigit():
        parsed = float(raw) if "." in raw else int(raw)
    elif "," in raw:
        parsed = [part.strip() for part in raw.split(",") if part.strip()]

    settings.set(args.key, parsed)
    settings.save()
    print(green(f"{args.key} = {parsed!r}"))
    return 0


# --------------------------------------------------------------------------- #
# Diagnostics, plugins, keys
# --------------------------------------------------------------------------- #

def cmd_doctor(args: argparse.Namespace) -> int:
    async def run() -> int:
        app = await _build_app()
        try:
            info = app.diagnostics()
            print(BANNER)

            print(bold("Brain"))
            ready = info["brain"]["ready"]
            print(f"  {green('ready') if ready else red('not configured')}")
            for name, ok in info["brain"]["providers"].items():
                print(f"    {green('+') if ok else dim('-')} {name}")
            for tier, model in info["brain"]["models"].items():
                print(dim(f"      {tier}: {model}"))

            print(bold("\nVoice"))
            voice = info["voice"]
            print(f"  in : {voice['input']}")
            if voice["input_problem"]:
                print(yellow(f"       {voice['input_problem']}"))
            print(f"  out: {voice['output']}")

            print(bold("\nPlugins"))
            for row in info["plugins"]:
                mark = green("+") if row["available"] else dim("-")
                missing = dim(f"  needs: {', '.join(row['missing'])}") if row["missing"] else ""
                print(f"  {mark} {row['name']:24}{missing}")
            for name, error in info["plugin_errors"].items():
                print(red(f"  ! {name}: {error}"))

            print(bold("\nMissions"))
            print(f"  {info['missions']} loaded, {info['tools']} tools available")
            for name, error in info["mission_errors"].items():
                print(red(f"  ! {name}: {error}"))

            print(bold("\nPermissions"))
            perms = info["permissions"]
            print(f"  workspace: {cyan(info['workspace'])}")
            print(f"  apps: {', '.join(perms['allowed_apps']) or dim('none yet')}")
            print(f"  pending approvals: {perms['pending_approvals']}")

            print(dim(f"\n  config: {info['home']}\n"))
            return 0 if ready else 1
        finally:
            app.shutdown()
    return asyncio.run(run())


def cmd_plugins(args: argparse.Namespace) -> int:
    async def run() -> int:
        app = await _build_app()
        try:
            print(bold("\nPlugins") + dim("  (use these names as a mission's \"plugin\")\n"))
            for row in app.plugins.catalogue():
                mark = green("+") if row["available"] else dim("-")
                print(f"  {mark} {bold(row['name']):26} {row['description'][:70]}")
                if row["missing"]:
                    print(dim(f"      needs: {', '.join(row['missing'])}"))
            print(bold("\nBuilt-in tools") + dim("  (also usable as mission steps)\n"))
            names = [n for n in app.tools.names() if n not in app.plugins.plugins]
            for i in range(0, len(names), 4):
                print("  " + "  ".join(f"{n:22}" for n in names[i:i + 4]))
            print(dim(f"\n  Add your own: drop a .py file in {paths.PLUGIN_DIR}"))
            print(dim(f"  Copy the template from {Path(__file__).parent / 'plugins' / 'TEMPLATE.py'}\n"))
            return 0
        finally:
            app.shutdown()
    return asyncio.run(run())


def cmd_keys(args: argparse.Namespace) -> int:
    vault = Vault()
    if vault.needs_passphrase:
        vault.unlock(_prompt_passphrase("Vault passphrase: "))

    if args.action == "list":
        stored = set(vault.names())
        print(bold("\nAPI keys\n"))
        for spec in KEY_SPECS:
            mark = green("set") if spec.name in stored else dim("not set")
            print(f"  {spec.name:24} {mark:14} {dim(spec.where)}")
        extra = stored - {s.name for s in KEY_SPECS}
        for name in sorted(extra):
            print(f"  {name:24} {green('set'):14} {dim('(plugin key)')}")
        print()
    elif args.action == "set":
        if not args.name:
            print(red("Which key? e.g. jarvis keys set ANTHROPIC_API_KEY"))
            return 1
        value = args.value or _prompt_passphrase(f"{args.name}: ")
        if not value:
            print(dim("Nothing entered; no change."))
            return 0
        vault.set(args.name.upper(), value)
        print(green(f"Stored {args.name.upper()}."))
    elif args.action == "remove":
        if not args.name:
            print(red("Which key?"))
            return 1
        vault.delete(args.name.upper())
        print(green(f"Removed {args.name.upper()}."))
    return 0



def cmd_voiceprint(args: argparse.Namespace) -> int:
    """Teach JARVIS your voice, so it ignores everyone else."""
    async def run() -> int:
        from .core.speaker_id import RECOMMENDED_SAMPLES, SpeakerVerifier
        from .core.voice_in import VoiceListener

        config = Config()
        settings = config.settings
        verifier = SpeakerVerifier(settings)
        action = (args.action or "status").lower()

        # --- simple state changes ------------------------------------- #
        if action == "forget":
            removed = verifier.forget()
            print(green("\n  Voiceprint deleted; matching switched off.\n")
                  if removed else dim("\n  There was nothing stored.\n"))
            return 0

        if action in ("on", "off"):
            if action == "on" and not verifier.enrolled:
                print(red("\n  Nothing enrolled yet. Run: jarvis voiceprint enrol\n"))
                return 1
            settings.set("voice.only_my_voice", action == "on")
            settings.save()
            print(green(f"\n  Voice matching {action}.\n"))
            return 0

        if action == "status":
            print(bold("\nVoiceprint"))
            print(f"  {verifier.describe()}")
            print(f"  model: {cyan('ready') if verifier.available() else red('not installed')}")
            if not verifier.available():
                print(dim(f"  {verifier.why_unavailable().splitlines()[0]}"))
            print(dim("""
  jarvis voiceprint enrol    teach it your voice
  jarvis voiceprint test     check whether it recognises you
  jarvis voiceprint on|off   only respond to you
  jarvis voiceprint forget   delete the stored voiceprint

  Stored on this machine only, as numbers - never the audio, never uploaded.
"""))
            return 0

        # --- the ones that need a microphone --------------------------- #
        if not verifier.available():
            print(red(f"\n  {verifier.why_unavailable()}\n"))
            return 1

        listener = VoiceListener(config)
        if not listener.available():
            print(red(f"\n  {listener.why_unavailable()}\n"))
            return 1

        if action == "test":
            if not verifier.enrolled:
                print(red("\n  Nothing enrolled yet. Run: jarvis voiceprint enrol\n"))
                return 1
            print(dim("\n  Say a sentence...\n"))
            pcm = await listener.microphone.record_utterance(max_seconds=8)
            if not pcm:
                print(red("  I didn't hear anything.\n"))
                return 1
            score = verifier.score(pcm)
            threshold = verifier.threshold()
            verdict = (green("that's you") if score >= threshold
                       else red("not recognised"))
            print(f"  similarity {bold(f'{score:.2f}')} vs threshold "
                  f"{threshold:.2f} - {verdict}\n")
            if score < threshold:
                print(dim("  If that was you, add more samples:\n"
                          "    jarvis voiceprint enrol\n"
                          "  or loosen it:\n"
                          "    jarvis config voice.speaker_threshold 0.62\n"))
            return 0

        if action == "enrol":
            count = max(1, int(args.samples or RECOMMENDED_SAMPLES))
            print(bold("\nTeaching JARVIS your voice"))
            print(dim(f"  {count} recordings, a full sentence each. Speak normally,\n"
                      "  from where you usually sit.\n"))

            prompts = [
                "Jarvis, open my workspace and tell me what's in it.",
                "What's the weather looking like this afternoon?",
                "Remind me what I asked you to do last night.",
                "Let's get the videos ready for tomorrow morning.",
            ]
            samples = []
            for index in range(count):
                phrase = prompts[index % len(prompts)]
                print(f'  {index + 1}/{count}  say: "{cyan(phrase)}"')
                try:
                    await asyncio.to_thread(input, dim("       press Enter when ready "))
                except (EOFError, KeyboardInterrupt):
                    print("\n  Cancelled.\n")
                    return 1
                pcm = await listener.microphone.record_utterance(max_seconds=12)
                seconds = len(pcm) / (16000 * 2)
                if seconds < 2.0:
                    print(red(f"       only got {seconds:.1f}s - let's try that one again"))
                    continue
                samples.append(pcm)
                print(green(f"       got it ({seconds:.1f}s)\n"))

            if not samples:
                print(red("  Nothing usable recorded.\n"))
                return 1

            try:
                profile = verifier.enrol(samples)
            except ValueError as exc:
                print(red(f"  {exc}\n"))
                return 1

            settings.set("voice.only_my_voice", True)
            settings.save()
            print(green(f"  Done - {profile.sample_count} sample(s) stored, "
                        f"matching switched on."))
            print(dim("  Check it with: jarvis voiceprint test\n"))
            return 0

        print(red(f"  Unknown action {action!r}."))
        return 1

    return asyncio.run(run())


def cmd_where(args: argparse.Namespace) -> int:
    print(f"""
  {bold('Config')}      {paths.ROOT}
  {bold('Settings')}    {paths.CONFIG_FILE}
  {bold('Database')}    {paths.DB_FILE}
  {bold('Logs')}        {paths.LOG_DIR}
  {bold('Missions')}    {paths.MISSION_DIR}
  {bold('Plugins')}     {paths.PLUGIN_DIR}
  {bold('Workspace')}   {Settings.load().workspace}
""")
    return 0


def cmd_ui(args: argparse.Namespace) -> int:
    try:
        from .ui.app import run_ui
    except ImportError as exc:
        print(red(f"The desktop interface needs PyQt6: pip install PyQt6  ({exc})"))
        return 1
    return run_ui()


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jarvis",
        description="JARVIS - a personal AI that can actually operate your computer.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  jarvis setup                     first-run wizard
  jarvis                           open the desktop app (or chat if PyQt6 is missing)
  jarvis chat                      talk to it in the terminal
  jarvis voice                     talk to it out loud
  jarvis ask "tidy my downloads"   one-shot command
  jarvis missions                  what's scheduled
  jarvis run morning_brief         run a mission now
  jarvis daemon                    just the scheduler, for overnight work
  jarvis doctor                    what's working and what isn't
  jarvis devices                   list microphones and speakers
  jarvis say "hello"               test the voice out loud
  jarvis listen                    test the microphone
  jarvis config models.general claude-haiku-4-5    change a setting
  jarvis voiceprint enrol          teach it your voice, ignore everyone else
""")
    parser.add_argument("--version", action="version", version=f"JARVIS {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command")

    setup = sub.add_parser("setup", help="first-run wizard")
    setup.add_argument("--minimal", action="store_true",
                       help="only ask for the required key")
    setup.set_defaults(func=cmd_setup)

    chat = sub.add_parser("chat", help="talk to JARVIS in the terminal")
    chat.add_argument("--speak", action="store_true", help="also reply out loud")
    chat.set_defaults(func=cmd_chat)

    ask = sub.add_parser("ask", help="one command, then exit")
    ask.add_argument("text", nargs="+")
    ask.add_argument("--speak", action="store_true")
    ask.set_defaults(func=cmd_ask)

    voice = sub.add_parser("voice", help="hands-free voice mode")
    voice.add_argument("--always-on", action="store_true",
                       help="skip the wake word and listen continuously")
    voice.add_argument("--no-scheduler", action="store_true")
    voice.set_defaults(func=cmd_voice)

    missions = sub.add_parser("missions", help="list saved missions")
    missions.set_defaults(func=cmd_missions)

    run = sub.add_parser("run", help="run a mission now")
    run.add_argument("mission")
    run.add_argument("--unattended", action="store_true",
                     help="queue approvals instead of asking")
    run.add_argument("--force", action="store_true", help="run despite warnings")
    run.set_defaults(func=cmd_run)

    daemon = sub.add_parser("daemon", help="run the scheduler in the foreground")
    daemon.set_defaults(func=cmd_daemon)

    approvals = sub.add_parser("approvals", help="see and clear pending approvals")
    approvals.add_argument("--approve", metavar="ID")
    approvals.add_argument("--deny", metavar="ID")
    approvals.set_defaults(func=cmd_approvals)

    approve = sub.add_parser("approve", help="approve a pending request")
    approve.add_argument("id")
    approve.set_defaults(func=lambda a: cmd_approvals(
        argparse.Namespace(approve=a.id, deny=None)))

    deny = sub.add_parser("deny", help="deny a pending request")
    deny.add_argument("id")
    deny.set_defaults(func=lambda a: cmd_approvals(
        argparse.Namespace(approve=None, deny=a.id)))

    doctor = sub.add_parser("doctor", help="check what's working")
    doctor.set_defaults(func=cmd_doctor)

    plugins = sub.add_parser("plugins", help="list plugins and tools")
    plugins.set_defaults(func=cmd_plugins)

    keys = sub.add_parser("keys", help="manage API keys")
    keys.add_argument("action", choices=["list", "set", "remove"], nargs="?",
                      default="list")
    keys.add_argument("name", nargs="?")
    keys.add_argument("value", nargs="?")
    keys.set_defaults(func=cmd_keys)

    devices = sub.add_parser("devices", help="list audio devices and choose them")
    devices.add_argument("--set-input", metavar="NAME",
                         help='microphone to use, e.g. "Lenovo"')
    devices.add_argument("--set-output", metavar="NAME",
                         help='speakers to use, e.g. "NVIDIA"')
    devices.set_defaults(func=cmd_devices)

    say = sub.add_parser("say", help="speak a line out loud (tests audio output)")
    say.add_argument("text", nargs="*")
    say.set_defaults(func=cmd_say)

    listen = sub.add_parser("listen", help="record one sentence (tests the microphone)")
    listen.set_defaults(func=cmd_listen)

    config = sub.add_parser("config", help="read or change a setting")
    config.add_argument("key", nargs="?", help="e.g. models.general")
    config.add_argument("value", nargs="?", help="omit to read it")
    config.set_defaults(func=cmd_config)

    voiceprint = sub.add_parser(
        "voiceprint", help="teach JARVIS your voice so it ignores others")
    voiceprint.add_argument(
        "action", nargs="?", default="status",
        choices=["status", "enrol", "enroll", "test", "on", "off", "forget"])
    voiceprint.add_argument("--samples", type=int, help="how many recordings")
    voiceprint.set_defaults(func=lambda a: cmd_voiceprint(
        argparse.Namespace(action=("enrol" if a.action == "enroll" else a.action),
                           samples=a.samples)))

    where = sub.add_parser("where", help="print where JARVIS keeps its files")
    where.set_defaults(func=cmd_where)

    ui = sub.add_parser("ui", help="open the desktop app")
    ui.set_defaults(func=cmd_ui)

    return parser


def main(argv: list[str] | None = None) -> int:
    import logging

    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(logging.DEBUG if args.verbose else logging.INFO)

    if not getattr(args, "command", None):
        # Bare `jarvis`: the desktop app if it's installed, otherwise chat.
        try:
            from .ui.app import run_ui
            return run_ui()
        except ImportError:
            return cmd_chat(argparse.Namespace(speak=False))

    try:
        return int(args.func(args) or 0)
    except VaultLocked as exc:
        print(red(f"\n{exc}"))
        return 1
    except KeyboardInterrupt:
        print("\nStopped.")
        return 130
    except Exception as exc:
        log.exception("command failed")
        print(red(f"\nSomething went wrong: {exc}"))
        print(dim(f"Details in {paths.LOG_DIR / 'jarvis.log'}"))
        return 1


if __name__ == "__main__":
    sys.exit(main())
