"""Where JARVIS keeps its stuff.

Every path lives under one root so backup/reset is a single folder operation.
Windows:  %APPDATA%\\JARVIS
macOS:    ~/Library/Application Support/JARVIS
Linux:    ~/.local/share/jarvis   (or $XDG_DATA_HOME/jarvis)

Override the whole tree with the JARVIS_HOME environment variable - handy for
tests, portable USB installs, and running two profiles side by side.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _default_root() -> Path:
    override = os.environ.get("JARVIS_HOME")
    if override:
        return Path(override).expanduser()

    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming"
        return Path(base) / "JARVIS"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "JARVIS"

    base = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(base) / "jarvis"


ROOT = _default_root()

CONFIG_FILE = ROOT / "settings.json"     # non-secret preferences (plain JSON, editable)
VAULT_FILE = ROOT / "keys.enc"           # encrypted API keys
VAULT_SALT_FILE = ROOT / "vault.salt"    # key-derivation salt
DB_FILE = ROOT / "jarvis.db"             # memory, logs, audit trail
LOG_DIR = ROOT / "logs"
MISSION_DIR = ROOT / "missions"          # user missions (built-ins ship with the package)
PLUGIN_DIR = ROOT / "plugins"            # user plugins (drop a .py file here)
OUTPUT_DIR = ROOT / "output"             # generated videos/images/audio
CACHE_DIR = ROOT / "cache"               # model downloads, temp renders

#: Default sandbox JARVIS may freely read and write without asking.
DEFAULT_WORKSPACE = Path.home() / "JARVIS Workspace"

#: Example missions that ship inside the package. Living under the package (not
#: beside it) is what makes them survive `pip install` and a PyInstaller build
#: alike - the user's own missions live in MISSION_DIR and win on an id clash.
PACKAGE_ROOT = Path(__file__).resolve().parent
BUILTIN_MISSION_DIR = PACKAGE_ROOT / "builtin_missions"


def ensure_dirs() -> None:
    """Create every directory JARVIS writes to. Safe to call repeatedly."""
    for directory in (ROOT, LOG_DIR, MISSION_DIR, PLUGIN_DIR, OUTPUT_DIR, CACHE_DIR):
        directory.mkdir(parents=True, exist_ok=True)
    # Private-by-default on POSIX: the vault and DB live here.
    if sys.platform != "win32":
        try:
            ROOT.chmod(0o700)
        except OSError:
            pass


def refresh() -> None:
    """Re-read JARVIS_HOME. Only needed by tests that move the root mid-process."""
    global ROOT, CONFIG_FILE, VAULT_FILE, VAULT_SALT_FILE, DB_FILE
    global LOG_DIR, MISSION_DIR, PLUGIN_DIR, OUTPUT_DIR, CACHE_DIR
    ROOT = _default_root()
    CONFIG_FILE = ROOT / "settings.json"
    VAULT_FILE = ROOT / "keys.enc"
    VAULT_SALT_FILE = ROOT / "vault.salt"
    DB_FILE = ROOT / "jarvis.db"
    LOG_DIR = ROOT / "logs"
    MISSION_DIR = ROOT / "missions"
    PLUGIN_DIR = ROOT / "plugins"
    OUTPUT_DIR = ROOT / "output"
    CACHE_DIR = ROOT / "cache"
