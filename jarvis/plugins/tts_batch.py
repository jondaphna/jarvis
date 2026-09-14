"""Turn text into audio files - one string or a whole list of scripts."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any

from ..core.grants import CAP_FS_WRITE
from ..core.tools import ExecContext
from .base import Plugin, PluginError


class TTSBatchPlugin(Plugin):
    NAME = "text_to_speech"
    DESCRIPTION = ("Generate spoken-audio files from text. Accepts one string or a "
                   "list, and returns the saved file paths. Used for voiceovers.")
    CAPABILITY = CAP_FS_WRITE
    RESOURCE_KEY = "out_dir"
    PATH_KEYS = ("out_dir",)

    SCHEMA = {
        "type": "object",
        "properties": {
            "texts": {"description": "A string, or a list of strings",
                      "anyOf": [{"type": "string"},
                                {"type": "array", "items": {"type": "string"}}]},
            "voice": {"type": "string", "description": "Voice id or name (optional)"},
            "engine": {"type": "string",
                       "enum": ["auto", "edge", "cartesia", "elevenlabs", "pyttsx3"]},
            "out_dir": {"type": "string", "description": "Where to save (optional)"},
        },
        "required": ["texts"],
    }

    def available(self) -> bool:
        return True            # edge-tts and pyttsx3 need no key

    async def run(self, params: dict[str, Any], context: ExecContext) -> Any:
        from ..core.voice_out import SpeechEngine

        raw = params.get("texts")
        texts = [raw] if isinstance(raw, str) else list(raw or [])
        texts = [str(t).strip() for t in texts if str(t).strip()]
        if not texts:
            raise PluginError("No text to speak.")

        computer = getattr(self.app, "computer", None)
        base = Path(params["out_dir"]) if params.get("out_dir") else (
            computer.output_dir() / "audio" if computer else Path.cwd() / "audio")
        base.mkdir(parents=True, exist_ok=True)

        engine = SpeechEngine(self.config)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        written: list[str] = []
        failures: list[str] = []

        for index, text in enumerate(texts, 1):
            target = base / f"voice-{stamp}-{index:02d}.mp3"
            try:
                path = await engine.synthesise_to_file(
                    text, target, voice=params.get("voice"),
                    engine=params.get("engine", "auto"))
                written.append(str(path))
                if self.app is not None and getattr(self.app, "memory", None):
                    self.app.memory.log_artifact(str(path), "audio", context.run_id)
            except Exception as exc:
                # One bad script shouldn't lose the other nine.
                failures.append(f"{index}: {exc}")
                await asyncio.sleep(0)

        if not written:
            raise PluginError("No audio was generated - " + "; ".join(failures))
        return {"files": written, "count": len(written), "failed": failures}
