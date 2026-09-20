"""Actually operating the computer: apps, files, screen, keyboard, shell.

Raw capability only - this module does NOT check permissions. Everything here
is reached through `jarvis/core/actions.py`, which declares the capability each
operation exercises so the broker can rule on it first. Keeping the two apart
means there is exactly one gate, and it cannot be bypassed by adding a tool.

Windows is the primary target; macOS and Linux work too so the same missions
run anywhere (and so the test suite can exercise this on a build server).
"""

from __future__ import annotations

import asyncio
import os
import platform
import re
import shutil
import subprocess
import sys
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .events import log

IS_WINDOWS = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
IS_LINUX = not IS_WINDOWS and not IS_MAC

#: Anything that could change the meaning of a command line, a path, or a
#: shell word. An unrecognised app name is handed to the operating system as a
#: target, so these are refused at the door rather than escaped per platform.
_SHELL_METACHARACTERS = re.compile(r"""[&|;<>^"'`$()\[\]{}!*?\n\r\t\x00]""")

#: Friendly name -> what to actually launch, per platform.
APP_ALIASES: dict[str, dict[str, str]] = {
    "chrome": {"win32": "chrome", "darwin": "Google Chrome", "linux": "google-chrome"},
    "edge": {"win32": "msedge", "darwin": "Microsoft Edge", "linux": "microsoft-edge"},
    "firefox": {"win32": "firefox", "darwin": "Firefox", "linux": "firefox"},
    "notepad": {"win32": "notepad", "darwin": "TextEdit", "linux": "gedit"},
    "explorer": {"win32": "explorer", "darwin": "Finder", "linux": "nautilus"},
    "files": {"win32": "explorer", "darwin": "Finder", "linux": "nautilus"},
    "terminal": {"win32": "cmd", "darwin": "Terminal", "linux": "x-terminal-emulator"},
    "powershell": {"win32": "powershell", "darwin": "Terminal", "linux": "x-terminal-emulator"},
    "calculator": {"win32": "calc", "darwin": "Calculator", "linux": "gnome-calculator"},
    "word": {"win32": "winword", "darwin": "Microsoft Word", "linux": "libreoffice --writer"},
    "excel": {"win32": "excel", "darwin": "Microsoft Excel", "linux": "libreoffice --calc"},
    "powerpoint": {"win32": "powerpnt", "darwin": "Microsoft PowerPoint",
                   "linux": "libreoffice --impress"},
    "outlook": {"win32": "outlook", "darwin": "Microsoft Outlook", "linux": "thunderbird"},
    "photoshop": {"win32": "photoshop", "darwin": "Adobe Photoshop", "linux": "gimp"},
    "premiere": {"win32": "premiere", "darwin": "Adobe Premiere Pro", "linux": "kdenlive"},
    "vscode": {"win32": "code", "darwin": "Visual Studio Code", "linux": "code"},
    "code": {"win32": "code", "darwin": "Visual Studio Code", "linux": "code"},
    "spotify": {"win32": "spotify", "darwin": "Spotify", "linux": "spotify"},
    "discord": {"win32": "discord", "darwin": "Discord", "linux": "discord"},
    "slack": {"win32": "slack", "darwin": "Slack", "linux": "slack"},
    "obs": {"win32": "obs64", "darwin": "OBS", "linux": "obs"},
    "capcut": {"win32": "capcut", "darwin": "CapCut", "linux": ""},
    "settings": {"win32": "ms-settings:", "darwin": "System Settings", "linux": "gnome-control-center"},
}

WEB_APPS = {
    "gmail": "https://mail.google.com",
    "youtube": "https://youtube.com",
    "youtube studio": "https://studio.youtube.com",
    "tiktok": "https://www.tiktok.com",
    "instagram": "https://instagram.com",
    "x": "https://x.com",
    "twitter": "https://x.com",
    "linkedin": "https://linkedin.com",
    "chatgpt": "https://chat.openai.com",
    "claude": "https://claude.ai",
    "drive": "https://drive.google.com",
    "google drive": "https://drive.google.com",
    "calendar": "https://calendar.google.com",
    "sheets": "https://sheets.google.com",
    "docs": "https://docs.google.com",
    "notion": "https://notion.so",
    "canva": "https://canva.com",
    "shopify": "https://admin.shopify.com",
    "amazon": "https://amazon.com",
}


class ComputerError(RuntimeError):
    """An operation on the machine failed in a way worth telling the user about."""


@dataclass
class ShellResult:
    command: str
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def summary(self, limit: int = 4000) -> str:
        parts = [f"exit code {self.returncode}" + (" (timed out)" if self.timed_out else "")]
        if self.stdout.strip():
            parts.append("stdout:\n" + self.stdout[:limit])
        if self.stderr.strip():
            parts.append("stderr:\n" + self.stderr[:limit])
        return "\n".join(parts)


class Computer:
    """Everything JARVIS can do to the machine itself."""

    def __init__(self, settings) -> None:
        self.settings = settings

    # ------------------------------------------------------------------ #
    # Apps and the browser
    # ------------------------------------------------------------------ #

    def resolve_app(self, name: str) -> tuple[str, str]:
        """(kind, target) where kind is 'web', 'alias' or 'raw'."""
        key = (name or "").strip().lower()
        if not key:
            raise ComputerError("No application name given.")
        if key in WEB_APPS:
            return "web", WEB_APPS[key]
        if key.startswith("http://") or key.startswith("https://"):
            return "web", key
        alias = APP_ALIASES.get(key)
        if alias:
            platform_key = "win32" if IS_WINDOWS else "darwin" if IS_MAC else "linux"
            target = alias.get(platform_key) or ""
            if target:
                return "alias", target
        if _SHELL_METACHARACTERS.search(key):
            # An unrecognised name goes to the OS as-is. Anything that could
            # change the meaning of a command line stops here rather than at
            # the launcher, so there is one refusal instead of one per platform.
            raise ComputerError(
                f"'{name}' isn't an application name I recognise, and it "
                f"contains characters I won't pass to the system.")
        return "raw", key

    async def open_app(self, name: str) -> str:
        kind, target = self.resolve_app(name)

        if kind == "web":
            await asyncio.to_thread(webbrowser.open, target)
            return f"Opened {target}"

        def _launch() -> str:
            if IS_WINDOWS:
                # `os.startfile` is ShellExecute: it resolves App Paths registry
                # entries, registered URI schemes and .lnk shortcuts, exactly as
                # `start` did. What it does not have is a command line, so there
                # is nothing for a quote or an `&` in the name to break out of.
                # The previous version built `start "" "{target}"` and ran it
                # through cmd with shell=True.
                os.startfile(target)          # Windows-only; no shell involved
                return f"Opened {target}"
            if IS_MAC:
                subprocess.Popen(["open", "-a", target])
                return f"Opened {target}"
            executable = shutil.which(target.split()[0])
            if executable is None:
                raise ComputerError(
                    f"I couldn't find '{target}' installed on this machine.")
            subprocess.Popen(target.split(), start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return f"Opened {target}"

        try:
            return await asyncio.to_thread(_launch)
        except ComputerError:
            raise
        except Exception as exc:
            raise ComputerError(f"Couldn't open '{name}': {exc}") from exc

    async def open_url(self, url: str) -> str:
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        await asyncio.to_thread(webbrowser.open, url)
        return f"Opened {url}"

    async def search_web_in_browser(self, query: str) -> str:
        from urllib.parse import quote_plus
        return await self.open_url(f"https://www.google.com/search?q={quote_plus(query)}")

    # ------------------------------------------------------------------ #
    # Files
    # ------------------------------------------------------------------ #

    def workspace(self) -> Path:
        root = self.settings.workspace
        root.mkdir(parents=True, exist_ok=True)
        return root

    def output_dir(self) -> Path:
        """Where generated files go - inside the workspace, so the user finds them."""
        target = self.workspace() / "output"
        target.mkdir(parents=True, exist_ok=True)
        return target

    def resolve(self, path: str | Path) -> Path:
        """Interpret a path, treating bare names as workspace-relative."""
        text = str(path or "").strip()
        if not text or text == ".":
            return self.workspace()
        raw = Path(text).expanduser()
        if not raw.is_absolute():
            raw = self.workspace() / raw
        return raw

    def list_dir(self, path: str | Path = "", pattern: str = "*") -> list[dict[str, Any]]:
        target = self.resolve(path or self.workspace())
        if not target.exists():
            raise ComputerError(f"{target} doesn't exist.")
        if target.is_file():
            target = target.parent
        entries = []
        for item in sorted(target.glob(pattern)):
            try:
                stat = item.stat()
                entries.append({
                    "name": item.name,
                    "path": str(item),
                    "is_dir": item.is_dir(),
                    "size": stat.st_size,
                    "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
                })
            except OSError:
                continue
        return entries

    def read_file(self, path: str | Path, max_chars: int = 200_000) -> str:
        target = self.resolve(path)
        if not target.exists():
            raise ComputerError(f"{target} doesn't exist.")
        if target.is_dir():
            raise ComputerError(f"{target} is a folder, not a file.")
        if target.stat().st_size > 50_000_000:
            raise ComputerError(f"{target.name} is too large to read into context.")
        try:
            text = target.read_text("utf-8", errors="replace")
        except OSError as exc:
            raise ComputerError(f"Couldn't read {target.name}: {exc}") from exc
        if len(text) > max_chars:
            return text[:max_chars] + f"\n\n... (truncated, {len(text)} chars total)"
        return text

    def write_file(self, path: str | Path, content: str, append: bool = False) -> str:
        target = self.resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        mode = "a" if append else "w"
        try:
            with open(target, mode, encoding="utf-8") as handle:
                handle.write(content)
        except OSError as exc:
            raise ComputerError(f"Couldn't write {target.name}: {exc}") from exc
        verb = "Appended to" if append else "Wrote"
        return f"{verb} {target} ({len(content)} chars)"

    def delete_path(self, path: str | Path) -> str:
        target = self.resolve(path)
        if not target.exists():
            return f"{target} was already gone."
        try:
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
        except OSError as exc:
            raise ComputerError(f"Couldn't delete {target.name}: {exc}") from exc
        return f"Deleted {target}"

    def move_path(self, source: str | Path, destination: str | Path) -> str:
        src, dst = self.resolve(source), self.resolve(destination)
        if not src.exists():
            raise ComputerError(f"{src} doesn't exist.")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        return f"Moved {src.name} to {dst}"

    def copy_path(self, source: str | Path, destination: str | Path) -> str:
        src, dst = self.resolve(source), self.resolve(destination)
        if not src.exists():
            raise ComputerError(f"{src} doesn't exist.")
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dst)
        return f"Copied {src.name} to {dst}"

    def find_files(self, pattern: str, root: str | Path = "", limit: int = 200) -> list[str]:
        base = self.resolve(root or self.workspace())
        if base.is_file():
            base = base.parent
        found: list[str] = []
        try:
            for item in base.rglob(pattern):
                found.append(str(item))
                if len(found) >= limit:
                    break
        except (OSError, ValueError) as exc:
            raise ComputerError(f"Search failed: {exc}") from exc
        return found

    def search_in_files(self, needle: str, root: str | Path = "",
                        pattern: str = "*.txt", limit: int = 50) -> list[dict[str, Any]]:
        base = self.resolve(root or self.workspace())
        hits: list[dict[str, Any]] = []
        for item in base.rglob(pattern):
            if not item.is_file() or item.stat().st_size > 5_000_000:
                continue
            try:
                for number, line in enumerate(item.read_text("utf-8", errors="ignore").splitlines(), 1):
                    if needle.lower() in line.lower():
                        hits.append({"file": str(item), "line": number, "text": line.strip()[:200]})
                        if len(hits) >= limit:
                            return hits
            except OSError:
                continue
        return hits

    # ------------------------------------------------------------------ #
    # Shell
    # ------------------------------------------------------------------ #

    async def run_shell(self, command: str, timeout: int = 120,
                        cwd: str | Path | None = None) -> ShellResult:
        work_dir = str(self.resolve(cwd)) if cwd else str(self.workspace())

        def _run() -> ShellResult:
            try:
                completed = subprocess.run(
                    command, shell=True, capture_output=True, text=True,
                    timeout=timeout, cwd=work_dir, errors="replace",
                )
                return ShellResult(command, completed.returncode,
                                   completed.stdout or "", completed.stderr or "")
            except subprocess.TimeoutExpired:
                return ShellResult(command, -1, "", f"Timed out after {timeout}s", True)

        return await asyncio.to_thread(_run)

    # ------------------------------------------------------------------ #
    # Screen, keyboard, clipboard
    # ------------------------------------------------------------------ #

    async def screenshot(self, path: str | Path | None = None,
                         region: tuple[int, int, int, int] | None = None) -> Path:
        target = Path(path) if path else (
            self.output_dir() / f"screen-{datetime.now():%Y%m%d-%H%M%S}.png")
        target.parent.mkdir(parents=True, exist_ok=True)

        def _grab() -> Path:
            try:
                import mss  # lighter and faster than pyautogui for pure capture
                with mss.mss() as sct:
                    monitor = ({"left": region[0], "top": region[1],
                                "width": region[2], "height": region[3]}
                               if region else sct.monitors[0])
                    shot = sct.grab(monitor)
                    import mss.tools
                    mss.tools.to_png(shot.rgb, shot.size, output=str(target))
                return target
            except ImportError:
                pass
            try:
                import pyautogui
                image = pyautogui.screenshot(region=region)
                image.save(target)
                return target
            except ImportError as exc:
                raise ComputerError(
                    "Screen capture needs one of `mss` or `pyautogui` installed. "
                    "Run: pip install mss"
                ) from exc

        return await asyncio.to_thread(_grab)

    async def type_text(self, text: str, interval: float = 0.01) -> str:
        def _type() -> str:
            pyautogui = _require_pyautogui()
            pyautogui.write(text, interval=interval)
            return f"Typed {len(text)} characters"
        return await asyncio.to_thread(_type)

    async def press_keys(self, keys: Iterable[str]) -> str:
        def _press() -> str:
            pyautogui = _require_pyautogui()
            sequence = list(keys)
            pyautogui.hotkey(*sequence)
            return f"Pressed {'+'.join(sequence)}"
        return await asyncio.to_thread(_press)

    async def click(self, x: int | None = None, y: int | None = None,
                    button: str = "left", clicks: int = 1) -> str:
        def _click() -> str:
            pyautogui = _require_pyautogui()
            if x is not None and y is not None:
                pyautogui.click(x=x, y=y, button=button, clicks=clicks)
                return f"Clicked at ({x}, {y})"
            pyautogui.click(button=button, clicks=clicks)
            return "Clicked at the current cursor position"
        return await asyncio.to_thread(_click)

    def clipboard_get(self) -> str:
        try:
            import pyperclip
            return pyperclip.paste()
        except ImportError as exc:
            raise ComputerError("Clipboard access needs `pyperclip`.") from exc

    def clipboard_set(self, text: str) -> str:
        try:
            import pyperclip
            pyperclip.copy(text)
            return f"Copied {len(text)} characters to the clipboard"
        except ImportError as exc:
            raise ComputerError("Clipboard access needs `pyperclip`.") from exc

    # ------------------------------------------------------------------ #
    # Notifications and machine facts
    # ------------------------------------------------------------------ #

    async def notify(self, title: str, message: str) -> str:
        def _notify() -> str:
            if IS_WINDOWS:
                try:
                    from win10toast import ToastNotifier
                    ToastNotifier().show_toast(title, message, duration=8, threaded=True)
                    return "Notification shown"
                except Exception:
                    pass
                try:
                    # PowerShell toast: no extra dependency on Windows 10/11.
                    script = (
                        '[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications,'
                        ' ContentType = WindowsRuntime] > $null;'
                        f'$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(0);'
                        f'$t.GetElementsByTagName("text").Item(0).AppendChild($t.CreateTextNode("{title}")) > $null;'
                        f'[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("JARVIS").Show('
                        '[Windows.UI.Notifications.ToastNotification]::new($t))'
                    )
                    subprocess.run(["powershell", "-NoProfile", "-Command", script],
                                   capture_output=True, timeout=10)
                    return "Notification shown"
                except Exception:
                    pass
            elif IS_MAC:
                try:
                    subprocess.run(
                        ["osascript", "-e",
                         f'display notification "{message}" with title "{title}"'],
                        capture_output=True, timeout=10)
                    return "Notification shown"
                except Exception:
                    pass
            else:
                if shutil.which("notify-send"):
                    subprocess.run(["notify-send", title, message], capture_output=True,
                                   timeout=10)
                    return "Notification shown"
            log.info("NOTIFY %s: %s", title, message)
            return "Logged the notification (no desktop notifier available)"

        return await asyncio.to_thread(_notify)

    def system_info(self) -> dict[str, Any]:
        info: dict[str, Any] = {
            "os": f"{platform.system()} {platform.release()}",
            "machine": platform.machine(),
            "python": platform.python_version(),
            "hostname": platform.node(),
            "workspace": str(self.workspace()),
            "time": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        try:
            usage = shutil.disk_usage(str(self.workspace()))
            info["disk_free_gb"] = round(usage.free / 1e9, 1)
        except OSError:
            pass
        try:
            import psutil
            info["cpu_percent"] = psutil.cpu_percent(interval=0.2)
            info["memory_percent"] = psutil.virtual_memory().percent
            info["battery"] = (getattr(psutil.sensors_battery(), "percent", None)
                               if hasattr(psutil, "sensors_battery") else None)
        except Exception:
            pass
        return info


def _require_pyautogui():
    try:
        import pyautogui
        pyautogui.FAILSAFE = True   # slam the mouse into a corner to abort
        return pyautogui
    except ImportError as exc:
        raise ComputerError(
            "Keyboard and mouse control needs `pyautogui`. Run: pip install pyautogui"
        ) from exc
    except Exception as exc:   # headless machine, no display
        raise ComputerError(f"No desktop session available for input control: {exc}") from exc
