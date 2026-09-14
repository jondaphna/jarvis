"""Video generation via Kling.

Kling authenticates with a JWT signed from an access/secret key pair rather
than a bearer token, which is why this plugin needs two keys and PyJWT.
"""

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

API_BASE = "https://api.klingai.com"


class KlingVideoPlugin(Plugin):
    NAME = "generate_video"
    DESCRIPTION = ("Generate a short video clip from a text prompt using Kling. "
                   "Returns the saved file path. Consumes Kling credits.")
    REQUIRES_KEYS = ["KLING_ACCESS_KEY", "KLING_SECRET_KEY"]
    REQUIRES_PACKAGES = ["jwt"]
    CAPABILITY = CAP_PAYMENT_SPEND
    RESOURCE_KEY = "prompt"
    SERVICE = "kling"
    PATH_KEYS = ("out_dir",)

    SCHEMA = {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "What the clip should show"},
            "duration": {"type": "integer", "description": "Seconds (5 or 10)"},
            "aspect_ratio": {"type": "string", "description": "16:9, 9:16 or 1:1"},
            "model": {"type": "string", "description": "Kling model name"},
            "out_dir": {"type": "string"},
        },
        "required": ["prompt"],
    }

    # ------------------------------------------------------------------ #

    def _token(self) -> str:
        try:
            import jwt
        except ImportError as exc:
            raise PluginError("Kling needs PyJWT. Run: pip install PyJWT") from exc

        access = self.key("KLING_ACCESS_KEY")
        secret = self.key("KLING_SECRET_KEY")
        if not (access and secret):
            raise PluginError("Kling needs both an access key and a secret key.")

        now = int(time.time())
        return jwt.encode(
            {"iss": access, "exp": now + 1800, "nbf": now - 5},
            secret,
            algorithm="HS256",
            headers={"alg": "HS256", "typ": "JWT"},
        )

    async def run(self, params: dict[str, Any], context: ExecContext) -> Any:
        prompt = str(params.get("prompt", "")).strip()
        if not prompt:
            raise PluginError("No video prompt given.")

        body = {
            "model_name": params.get("model", "kling-v1"),
            "prompt": prompt,
            "duration": str(params.get("duration", 5)),
            "aspect_ratio": params.get("aspect_ratio", "9:16"),
            "mode": "std",
        }

        computer = getattr(self.app, "computer", None)
        base = Path(params["out_dir"]) if params.get("out_dir") else (
            computer.output_dir() / "video" if computer else Path.cwd() / "video")
        base.mkdir(parents=True, exist_ok=True)

        url = await asyncio.to_thread(self._generate, body)
        target = base / f"clip-{datetime.now():%Y%m%d-%H%M%S}.mp4"
        await asyncio.to_thread(_download, url, target)

        if getattr(self.app, "memory", None):
            self.app.memory.log_artifact(str(target), "video", context.run_id)
        return {"file": str(target), "prompt": prompt, "source_url": url}

    def _generate(self, body: dict[str, Any]) -> str:
        headers = {"Authorization": f"Bearer {self._token()}",
                   "Content-Type": "application/json"}
        response = requests.post(f"{API_BASE}/v1/videos/text2video", headers=headers,
                                 json=body, timeout=120)
        if response.status_code >= 400:
            raise PluginError(f"Kling returned {response.status_code}: {response.text[:300]}")

        data = response.json().get("data") or {}
        task_id = data.get("task_id")
        if not task_id:
            raise PluginError(f"Kling didn't return a task id: {response.text[:200]}")

        # Rendering commonly takes several minutes.
        deadline = time.time() + 1800
        while time.time() < deadline:
            time.sleep(10)
            poll = requests.get(f"{API_BASE}/v1/videos/text2video/{task_id}",
                                headers={"Authorization": f"Bearer {self._token()}"},
                                timeout=60)
            if poll.status_code >= 400:
                raise PluginError(f"Kling poll failed: {poll.text[:200]}")
            payload = poll.json().get("data") or {}
            status = payload.get("task_status")
            if status == "succeed":
                videos = (payload.get("task_result") or {}).get("videos") or []
                if not videos:
                    raise PluginError("Kling reported success but returned no video.")
                return videos[0]["url"]
            if status == "failed":
                raise PluginError(f"Kling failed: {payload.get('task_status_msg', '')}")

        raise PluginError("Kling took more than 30 minutes; gave up waiting.")


def _download(url: str, target: Path) -> None:
    response = requests.get(url, timeout=900, stream=True)
    response.raise_for_status()
    with open(target, "wb") as handle:
        for chunk in response.iter_content(1 << 20):
            handle.write(chunk)
