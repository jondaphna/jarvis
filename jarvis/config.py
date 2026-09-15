"""Configuration: an encrypted key vault plus plain-JSON preferences.

Two stores, deliberately separate:

* ``Vault``    - API keys, encrypted at rest with Fernet (AES-128-CBC + HMAC).
* ``Settings`` - everything else, plain JSON you can open in Notepad and edit.

Threat model, stated honestly: with no passphrase set, the encryption key is
derived from a machine-local salt file readable only by your user account. That
stops casual snooping, cloud-sync leaks, and keys landing in a screenshot or a
git repo - it does NOT stop someone who already has your logged-in account.
Set a passphrase (``jarvis setup --passphrase``) if you want protection that
survives that; JARVIS will then ask for it once per launch.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from . import paths

_KDF_ITERATIONS = 390_000


class VaultLocked(Exception):
    """Raised when the vault needs a passphrase that has not been supplied."""


# --------------------------------------------------------------------------- #
# Known API keys - drives the setup wizard and the settings panel.
# Plugins add their own via Plugin.REQUIRES_KEYS; these are the built-ins.
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class KeySpec:
    name: str
    label: str
    where: str
    free_tier: str
    required: bool = False
    note: str = ""


KEY_SPECS: tuple[KeySpec, ...] = (
    KeySpec("ANTHROPIC_API_KEY", "Claude (main brain)", "console.anthropic.com",
            "pay-as-you-go, ~$5 goes a long way", required=True,
            note="The one key JARVIS genuinely needs. Everything else is optional."),
    KeySpec("OPENAI_API_KEY", "OpenAI (optional brain/images)", "platform.openai.com",
            "$5 trial credit"),
    KeySpec("GEMINI_API_KEY", "Gemini (optional cheap brain)", "aistudio.google.com",
            "generous free tier"),
    KeySpec("DEEPGRAM_API_KEY", "Deepgram (optional faster speech-to-text)",
            "console.deepgram.com", "$200 credit",
            note="Only needed if local Whisper is too slow on your machine."),
    KeySpec("FISH_API_KEY", "Fish Audio (custom / cloned voice)", "fish.audio",
            "free credit on signup",
            note="Use this for a specific JARVIS voice. Paste the voice model id "
                 "from the fish.audio model page into Settings as well."),
    KeySpec("CARTESIA_API_KEY", "Cartesia (optional ultra-low-latency voice)",
            "play.cartesia.ai", "free monthly characters"),
    KeySpec("ELEVENLABS_API_KEY", "ElevenLabs (optional premium voice)",
            "elevenlabs.io", "10k chars/month free"),
    KeySpec("EXA_API_KEY", "Exa (optional research-grade web search)", "exa.ai",
            "$10 credit",
            note="Claude's built-in web search works without this."),
    KeySpec("TAVILY_API_KEY", "Tavily (optional news search)", "tavily.com",
            "1,000 searches/month free"),
    KeySpec("REPLICATE_API_TOKEN", "Replicate (image + video models)", "replicate.com",
            "pay-per-use, ~$0.003/image"),
    KeySpec("KLING_ACCESS_KEY", "Kling (video generation)", "klingai.com",
            "66 credits/day free"),
    KeySpec("KLING_SECRET_KEY", "Kling secret", "klingai.com", "same account"),
    KeySpec("HEYGEN_API_KEY", "HeyGen (talking-avatar video)", "heygen.com",
            "free trial"),
    KeySpec("SMTP_PASSWORD", "Email password / app password",
            "your mail provider", "free",
            note="For the notify plugin. Gmail needs an App Password, not your login."),
)

KEY_SPECS_BY_NAME = {spec.name: spec for spec in KEY_SPECS}


# --------------------------------------------------------------------------- #
# Vault
# --------------------------------------------------------------------------- #

class Vault:
    """Encrypted key/value store for secrets."""

    def __init__(self, passphrase: str | None = None) -> None:
        self._passphrase = passphrase
        self._lock = threading.RLock()
        self._cache: dict[str, str] | None = None
        self._fernet: Fernet | None = None

    # -- key derivation ----------------------------------------------------- #

    def _salt(self) -> bytes:
        paths.ensure_dirs()
        if paths.VAULT_SALT_FILE.exists():
            return paths.VAULT_SALT_FILE.read_bytes()
        salt = secrets.token_bytes(32)
        paths.VAULT_SALT_FILE.write_bytes(salt)
        _harden(paths.VAULT_SALT_FILE)
        return salt

    def _machine_secret(self) -> str:
        """Stable per-machine, per-user string used when no passphrase is set."""
        import getpass
        import platform

        parts = [platform.node(), platform.machine()]
        try:
            parts.append(getpass.getuser())
        except Exception:
            parts.append(os.environ.get("USERNAME") or os.environ.get("USER") or "user")
        return "|".join(parts)

    def _get_fernet(self) -> Fernet:
        with self._lock:
            if self._fernet is not None:
                return self._fernet
            material = self._passphrase or self._machine_secret()
            kdf = PBKDF2HMAC(
                algorithm=hashes.SHA256(),
                length=32,
                salt=self._salt(),
                iterations=_KDF_ITERATIONS,
            )
            key = base64.urlsafe_b64encode(kdf.derive(material.encode("utf-8")))
            self._fernet = Fernet(key)
            return self._fernet

    def unlock(self, passphrase: str | None) -> None:
        """Supply (or clear) the passphrase and drop cached crypto state."""
        with self._lock:
            self._passphrase = passphrase
            self._fernet = None
            self._cache = None

    @property
    def needs_passphrase(self) -> bool:
        """True when a vault exists that the current credentials cannot open."""
        if not paths.VAULT_FILE.exists():
            return False
        try:
            self._load()
            return False
        except VaultLocked:
            return True

    # -- storage ------------------------------------------------------------ #

    def _load(self) -> dict[str, str]:
        with self._lock:
            if self._cache is not None:
                return self._cache
            if not paths.VAULT_FILE.exists():
                self._cache = {}
                return self._cache
            blob = paths.VAULT_FILE.read_bytes()
            try:
                plaintext = self._get_fernet().decrypt(blob)
            except InvalidToken as exc:
                raise VaultLocked(
                    "Could not decrypt the key vault. If you set a passphrase, "
                    "JARVIS needs it; if you moved this file from another machine, "
                    "re-enter your keys with `jarvis setup`."
                ) from exc
            self._cache = json.loads(plaintext.decode("utf-8"))
            return self._cache

    def _save(self, data: dict[str, str]) -> None:
        with self._lock:
            paths.ensure_dirs()
            blob = self._get_fernet().encrypt(json.dumps(data, indent=0).encode("utf-8"))
            tmp = paths.VAULT_FILE.with_suffix(".tmp")
            tmp.write_bytes(blob)
            _harden(tmp)
            tmp.replace(paths.VAULT_FILE)
            self._cache = data

    # -- public API --------------------------------------------------------- #

    def get(self, name: str, default: str | None = None) -> str | None:
        """Look up a key. Environment variables win, so CI and .env still work."""
        env = os.environ.get(name)
        if env:
            return env
        try:
            return self._load().get(name) or default
        except VaultLocked:
            return default

    def set(self, name: str, value: str) -> None:
        data = dict(self._load())
        if value:
            data[name] = value
        else:
            data.pop(name, None)
        self._save(data)

    def set_many(self, values: dict[str, str]) -> None:
        data = dict(self._load())
        for name, value in values.items():
            if value:
                data[name] = value
            else:
                data.pop(name, None)
        self._save(data)

    def delete(self, name: str) -> None:
        self.set(name, "")

    def names(self) -> list[str]:
        """Names of stored keys (never the values)."""
        try:
            stored = set(self._load())
        except VaultLocked:
            stored = set()
        stored |= {spec.name for spec in KEY_SPECS if os.environ.get(spec.name)}
        return sorted(stored)

    def has(self, *names: str) -> bool:
        """True only if every named key resolves to a non-empty value."""
        return all(bool(self.get(name)) for name in names)

    def missing(self, names: Iterable[str]) -> list[str]:
        return [name for name in names if not self.get(name)]


def _harden(path: Path) -> None:
    """Best-effort owner-only permissions."""
    if sys.platform != "win32":
        try:
            path.chmod(0o600)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #

DEFAULT_SETTINGS: dict[str, Any] = {
    "assistant_name": "JARVIS",
    "user_name": "",
    "personality": (
        "Warm, relaxed and genuinely friendly - like a sharp mate who happens to "
        "run your computer. Talk the way a person actually talks: contractions, "
        "easy rhythm, a bit of humour when it fits. Interested in what they're "
        "doing, not just processing requests. Never stiff, never corporate, never "
        "formal for the sake of it."
    ),
    "timezone": "",                      # blank = system timezone

    # --- models ---------------------------------------------------------- #
    "models": {
        "voice": "claude-haiku-4-5",     # fast conversational replies
        "general": "claude-opus-5",      # default thinking model
        "deep": "claude-opus-5",         # overnight autonomous missions
        "vision": "claude-opus-5",       # screenshots / image understanding
    },
    "effort": {"voice": "low", "general": "high", "deep": "xhigh"},
    "provider_order": ["anthropic", "openai", "gemini", "ollama"],

    # --- which brain answers ---------------------------------------------- #
    "brain": {
        # "auto" = use the free local model when Ollama is running, else Claude.
        # true = always local, false = always Claude.
        "local_first": "auto",
        "local_model": "llama3.1",
        # Say any of these and Claude handles that request, however you're set up.
        "escalate_phrases": ["heavy guns", "big guns", "full power", "max power",
                             "use claude", "bring in claude", "serious mode"],
        "escalate_tier": "deep",
    },
    "ollama_host": "http://127.0.0.1:11434",
    "server_side_fallbacks": True,       # auto-route refusals on Opus 5

    # --- voice ----------------------------------------------------------- #
    "voice": {
        "stt_engine": "auto",            # auto | faster-whisper | deepgram | none
        "tts_engine": "auto",            # auto | fish | edge | cartesia | elevenlabs | pyttsx3 | none
        "whisper_model": "base.en",      # tiny.en | base.en | small.en | medium.en
        "edge_voice": "en-GB-RyanNeural",
        "fish_voice_id": "",             # voice model id from fish.audio
        "fish_model": "s1",              # Fish TTS model: s1 or speech-1.6
        "cartesia_voice_id": "a0e99841-438c-4a64-b679-ae501e7d6091",
        "elevenlabs_voice_id": "21m00Tcm4TlvDq8ikWAM",
        "speaking_rate": "+8%",
        "input_device": None,
        "output_device": None,
        "wake_word": "jarvis",
        # Start listening the moment JARVIS opens - no button press needed.
        "always_listening": True,
        # Respond only to the enrolled voice. Needs `jarvis voiceprint enrol`
        # first; harmless to switch on before that (it just does nothing).
        "only_my_voice": False,
        "speaker_threshold": 0.70,
        # After a reply, keep listening this long without needing the wake word.
        "follow_up_seconds": 12,
        "hotkey": "ctrl+space",
        "vad_silence_ms": 700,
        "enabled": True,
    },

    # --- autonomy -------------------------------------------------------- #
    "autonomy": {
        "workspace": str(paths.DEFAULT_WORKSPACE),
        "extra_workspaces": [],
        "allowed_apps": [],              # e.g. ["chrome", "excel", "photoshop"]
        "allowed_domains": [],           # blank = any domain for READ-ONLY browsing
        "allowed_shell": [],             # exact commands allowed without approval
        "auto_approve_low_risk": True,   # workspace file writes, app launches
        "require_grant_for_high_risk": True,   # money / posting / email - always
        "unattended_mode": True,         # missions may run with nobody watching
        "max_mission_minutes": 120,
        "daily_spend_cap_usd": 5.0,      # LLM spend guard; 0 disables the cap
    },

    # --- missions / runtime ---------------------------------------------- #
    "scheduler": {"enabled": True, "misfire_grace_seconds": 3600, "max_parallel": 2},
    "notifications": {
        "desktop": True,
        "email_to": "",
        "smtp_host": "smtp.gmail.com",
        "smtp_port": 587,
        "smtp_user": "",
    },
    "ui": {
        "theme": "dark",
        "start_minimized": False,
        "launch_on_login": False,
        # The small always-on-top circle left behind when you close the window.
        "floating_orb": True,
        "orb_x": None,
        "orb_y": None,
    },
    "telemetry": False,                  # JARVIS never phones home; kept for clarity
}


class Settings:
    """Plain-JSON preferences with dotted-path access and deep-merged defaults."""

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self._lock = threading.RLock()
        self._data = _deep_merge(DEFAULT_SETTINGS, data or {})

    #: Old default values that should be replaced rather than preserved. A
    #: saved settings file always wins over a default, so shipping a better
    #: default would otherwise never reach anyone who has already run setup.
    _STALE_DEFAULTS = {
        "personality": [
            "Dry, precise, quietly amused. Brief by default - one or two sentences "
            "unless asked for detail. Address the user as 'sir' only sparingly. "
            "Never pad answers with filler or restate the question."
        ],
    }

    @classmethod
    def load(cls) -> "Settings":
        if paths.CONFIG_FILE.exists():
            try:
                raw = json.loads(paths.CONFIG_FILE.read_text("utf-8"))
            except (json.JSONDecodeError, OSError):
                raw = {}
        else:
            raw = {}

        for key, stale in cls._STALE_DEFAULTS.items():
            if str(raw.get(key, "")).strip() in {s.strip() for s in stale}:
                raw.pop(key, None)

        return cls(raw)

    def save(self) -> None:
        with self._lock:
            paths.ensure_dirs()
            tmp = paths.CONFIG_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
            tmp.replace(paths.CONFIG_FILE)

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, dotted: str, value: Any) -> None:
        with self._lock:
            parts = dotted.split(".")
            node = self._data
            for part in parts[:-1]:
                node = node.setdefault(part, {})
                if not isinstance(node, dict):
                    raise TypeError(f"{dotted!r} traverses a non-object value")
            node[parts[-1]] = value

    def update(self, values: dict[str, Any]) -> None:
        for dotted, value in values.items():
            self.set(dotted, value)

    def as_dict(self) -> dict[str, Any]:
        return json.loads(json.dumps(self._data))

    # convenience -------------------------------------------------------- #

    @property
    def workspace(self) -> Path:
        return Path(self.get("autonomy.workspace", str(paths.DEFAULT_WORKSPACE))).expanduser()

    def model_for(self, tier: str) -> str:
        return self.get(f"models.{tier}") or self.get("models.general") or "claude-opus-5"

    def effort_for(self, tier: str) -> str:
        return self.get(f"effort.{tier}") or "high"


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = json.loads(json.dumps(base))
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


@dataclass
class Config:
    """Bundle passed around the app: settings + vault together."""

    settings: Settings = field(default_factory=Settings.load)
    vault: Vault = field(default_factory=Vault)

    def key(self, name: str, default: str | None = None) -> str | None:
        return self.vault.get(name, default)

    def has_keys(self, *names: str) -> bool:
        return self.vault.has(*names)

    def save(self) -> None:
        self.settings.save()
