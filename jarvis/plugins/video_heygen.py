"""Talking-avatar video via HeyGen - the AI-presenter format."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

from ..core.grants import CAP_PAYMENT_SPEND
from ..core.tools import ExecContext
from .base import Plugin, PluginError

API = "https://api.heygen.com"


class HeyGenPlugin(Plugin):
    NAME = "generate_avatar_video"
    DESCRIPTION = ("Create a talking-head video where an AI presenter speaks your "
                   "script. Good for faceless channels and AI-influencer content.")
    REQUIRES_KEYS = ["HEYGEN_API_KEY"]
    CAPABILITY = CAP_PAYMENT_SPEND
    RESOURCE_KEY = "avatar_id"
    SERVICE = "heygen"
    PATH_KEYS = ("out_dir",)

    SCHEMA = {
        "type": "object",
        "properties": {
            "script": {"type": "string", "description": "What the presenter says"},
            "avatar_id": {"type": "string", "description": "HeyGen avatar id"},
            "voice_id": {"type": "string", "description": "HeyGen voice id"},
            "aspect": {"type": "string", "enum": ["portrait", "landscape"]},
            "out_dir": {"type": "string"},
        },
        "required": ["script"],
    }

    def _headers(self) -> dict[str, str]:
        return {"X-Api-Key": self.key("HEYGEN_API_KEY") or "",
                "Content-Type": "application/json"}

    async def run(self, params: dict[str, Any], context: ExecContext) -> Any:
        script = str(params.get("script", "")).strip()
        if not script:
            raise PluginError("No script given for the avatar to read.")

        portrait = str(params.get("aspect", "portrait")) == "portrait"
        body = {
            "video_inputs": [{
                "character": {
                    "type": "avatar",
                    "avatar_id": params.get("avatar_id", "Daisy-inskirt-20220818"),
                    "avatar_style": "normal",
                },
                "voice": {
                    "type": "text",
                    "input_text": script[:3000],
                    "voice_id": params.get("voice_id", "2d5b0e6cf36f460aa7fc47e3eee4ba54"),
                },
            }],
            "dimension": {"width": 720, "height": 1280} if portrait
                         else {"width": 1280, "height": 720},
        }

        computer = getattr(self.app, "computer", None)
        base = Path(params["out_dir"]) if params.get("out_dir") else (
            computer.output_dir() / "video" if computer else Path.cwd() / "video")
        base.mkdir(parents=True, exist_ok=True)

        url = await asyncio.to_thread(self._generate, body)
        target = base / f"avatar-{datetime.now():%Y%m%d-%H%M%S}.mp4"
        await asyncio.to_thread(_download, url, target)

        if getattr(self.app, "memory", None):
            self.app.memory.log_artifact(str(target), "video", context.run_id)
        return {"file": str(target), "script": script[:200], "source_url": url}

    def _generate(self, body: dict[str, Any]) -> str:
        response = requests.post(f"{API}/v2/video/generate", headers=self._headers(),
                                 json=body, timeout=120)
        if response.status_code >= 400:
            raise PluginError(f"HeyGen returned {response.status_code}: "
                              f"{response.text[:300]}")
        video_id = (response.json().get("data") or {}).get("video_id")
        if not video_id:
            raise PluginError(f"HeyGen didn't return a video id: {response.text[:200]}")

        deadline = time.time() + 1800
        while time.time() < deadline:
            time.sleep(10)
            poll = requests.get(f"{API}/v1/video_status.get?video_id={video_id}",
                                headers=self._headers(), timeout=60)
            if poll.status_code >= 400:
                raise PluginError(f"HeyGen poll failed: {poll.text[:200]}")
            data = poll.json().get("data") or {}
            status = data.get("status")
            if status == "completed":
                return data.get("video_url") or ""
            if status in ("failed", "error"):
                raise PluginError(f"HeyGen failed: {data.get('error') or 'no detail'}")

        raise PluginError("HeyGen took more than 30 minutes; gave up waiting.")


def _download(url: str, target: Path) -> None:
    response = requests.get(url, timeout=900, stream=True)
    response.raise_for_status()
    with open(target, "wb") as handle:
        for chunk in response.iter_content(1 << 20):
            handle.write(chunk)
