"""Stitch video and audio together with ffmpeg. No API key, no cost."""

from __future__ import annotations

import asyncio
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from ..core.grants import CAP_FS_WRITE
from ..core.tools import ExecContext
from .base import Plugin, PluginError


class VideoMergePlugin(Plugin):
    NAME = "merge_video_audio"
    DESCRIPTION = ("Combine video clips with voiceover audio into finished files "
                   "using ffmpeg. Pass equal-length lists to pair them up.")
    CAPABILITY = CAP_FS_WRITE
    RESOURCE_KEY = "out_dir"
    PATH_KEYS = ("out_dir",)

    SCHEMA = {
        "type": "object",
        "properties": {
            "videos": {"anyOf": [{"type": "string"},
                                 {"type": "array", "items": {"type": "string"}}]},
            "audios": {"anyOf": [{"type": "string"},
                                 {"type": "array", "items": {"type": "string"}}]},
            "out_dir": {"type": "string"},
            "loop_video": {"type": "boolean",
                           "description": "Loop the clip to match audio length"},
        },
        "required": ["videos", "audios"],
    }

    def available(self) -> bool:
        return shutil.which("ffmpeg") is not None

    def missing_requirements(self) -> list[str]:
        if self.available():
            return []
        return ["ffmpeg (install it and make sure it's on PATH - ffmpeg.org/download)"]

    async def run(self, params: dict[str, Any], context: ExecContext) -> Any:
        if not self.available():
            raise PluginError(
                "ffmpeg isn't installed or isn't on PATH. Get it from ffmpeg.org "
                "and reopen JARVIS.")

        videos = _as_list(params.get("videos"))
        audios = _as_list(params.get("audios"))
        if not videos or not audios:
            raise PluginError("Need at least one video and one audio file.")
        if len(videos) != len(audios) and len(videos) != 1 and len(audios) != 1:
            raise PluginError(
                f"Got {len(videos)} videos and {len(audios)} audio files. Give me "
                f"matching counts, or one of either to reuse.")

        pairs = max(len(videos), len(audios))
        computer = getattr(self.app, "computer", None)
        base = Path(params["out_dir"]) if params.get("out_dir") else (
            computer.output_dir() / "final" if computer else Path.cwd() / "final")
        base.mkdir(parents=True, exist_ok=True)

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        loop = bool(params.get("loop_video", True))
        made: list[str] = []
        failed: list[str] = []

        for index in range(pairs):
            video = videos[index % len(videos)]
            audio = audios[index % len(audios)]
            target = base / f"final-{stamp}-{index + 1:02d}.mp4"
            command = ["ffmpeg", "-y"]
            if loop:
                command += ["-stream_loop", "-1"]
            command += ["-i", str(video), "-i", str(audio),
                        "-map", "0:v:0", "-map", "1:a:0",
                        "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac",
                        "-shortest", str(target)]
            code, error = await _run(command)
            if code == 0 and target.exists():
                made.append(str(target))
                if getattr(self.app, "memory", None):
                    self.app.memory.log_artifact(str(target), "video", context.run_id)
            else:
                failed.append(f"{Path(video).name}: {error[-300:]}")

        if not made:
            raise PluginError("ffmpeg produced nothing - " + "; ".join(failed))
        return {"files": made, "count": len(made), "failed": failed}


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    return [str(item) for item in value if str(item).strip()]


async def _run(command: list[str]) -> tuple[int, str]:
    process = await asyncio.create_subprocess_exec(
        *command, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    _, stderr = await process.communicate()
    return process.returncode or 0, (stderr or b"").decode("utf-8", "replace")
