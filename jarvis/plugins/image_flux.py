"""Image generation via Replicate (FLUX by default, any model on request)."""

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

API = "https://api.replicate.com/v1"
DEFAULT_MODEL = "black-forest-labs/flux-schnell"


class ImagePlugin(Plugin):
    NAME = "generate_image"
    DESCRIPTION = ("Generate images from a text prompt using FLUX on Replicate. "
                   "Returns saved file paths. Costs roughly $0.003 per image.")
    REQUIRES_KEYS = ["REPLICATE_API_TOKEN"]
    #: Pay-per-image, so it's spending the user's money.
    CAPABILITY = CAP_PAYMENT_SPEND
    RESOURCE_KEY = "model"
    SERVICE = "replicate"
    PATH_KEYS = ("out_dir",)
    AMOUNT_KEY = "estimated_cost_usd"

    SCHEMA = {
        "type": "object",
        "properties": {
            "prompt": {"type": "string"},
            "count": {"type": "integer", "description": "How many images (default 1)"},
            "aspect_ratio": {"type": "string", "description": "e.g. 16:9, 9:16, 1:1"},
            "model": {"type": "string", "description": f"Default {DEFAULT_MODEL}"},
            "out_dir": {"type": "string"},
        },
        "required": ["prompt"],
    }

    async def run(self, params: dict[str, Any], context: ExecContext) -> Any:
        token = self.key("REPLICATE_API_TOKEN")
        if not token:
            raise PluginError("No Replicate token configured.")

        prompt = str(params.get("prompt", "")).strip()
        if not prompt:
            raise PluginError("No image prompt given.")
        count = max(1, min(int(params.get("count", 1)), 10))
        model = params.get("model") or DEFAULT_MODEL

        computer = getattr(self.app, "computer", None)
        base = Path(params["out_dir"]) if params.get("out_dir") else (
            computer.output_dir() / "images" if computer else Path.cwd() / "images")
        base.mkdir(parents=True, exist_ok=True)

        payload = {
            "input": {
                "prompt": prompt,
                "num_outputs": count,
                "aspect_ratio": params.get("aspect_ratio", "1:1"),
                "output_format": "png",
            }
        }

        urls = await asyncio.to_thread(self._run_model, token, model, payload)

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        saved: list[str] = []
        for index, url in enumerate(urls, 1):
            target = base / f"image-{stamp}-{index:02d}.png"
            try:
                await asyncio.to_thread(_download, url, target)
                saved.append(str(target))
                if getattr(self.app, "memory", None):
                    self.app.memory.log_artifact(str(target), "image", context.run_id)
            except Exception as exc:
                raise PluginError(f"Generated the image but couldn't save it: {exc}") from exc

        return {"files": saved, "count": len(saved), "prompt": prompt, "model": model}

    # ------------------------------------------------------------------ #

    def _run_model(self, token: str, model: str, payload: dict[str, Any]) -> list[str]:
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                   "Prefer": "wait"}
        response = requests.post(f"{API}/models/{model}/predictions", headers=headers,
                                 json=payload, timeout=180)
        if response.status_code >= 400:
            raise PluginError(f"Replicate returned {response.status_code}: "
                              f"{response.text[:300]}")
        prediction = response.json()

        # `Prefer: wait` usually returns a finished prediction; poll if not.
        deadline = time.time() + 600
        while prediction.get("status") in ("starting", "processing"):
            if time.time() > deadline:
                raise PluginError("Replicate took longer than 10 minutes; gave up.")
            time.sleep(2.5)
            poll = requests.get(prediction["urls"]["get"], headers=headers, timeout=60)
            poll.raise_for_status()
            prediction = poll.json()

        if prediction.get("status") != "succeeded":
            raise PluginError(f"Image generation {prediction.get('status')}: "
                              f"{prediction.get('error') or 'no detail given'}")

        output = prediction.get("output")
        if isinstance(output, str):
            return [output]
        return [url for url in (output or []) if isinstance(url, str)]


def _download(url: str, target: Path) -> None:
    response = requests.get(url, timeout=300, stream=True)
    response.raise_for_status()
    with open(target, "wb") as handle:
        for chunk in response.iter_content(65536):
            handle.write(chunk)
