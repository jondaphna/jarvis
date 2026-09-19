"""COPY THIS FILE to add any new tool or AI service to JARVIS.

Three steps, about five minutes:

  1. Copy this file to `plugins/your_tool.py` (either next to this file, or in
     your personal plugin folder - `jarvis where` prints the path).
  2. Fill in NAME, DESCRIPTION, SCHEMA, and run().
  3. Restart JARVIS. It appears as a voice command AND as a mission step.

Nothing else in JARVIS needs to change. If your service needs an API key, list
it in REQUIRES_KEYS and JARVIS will prompt for it in Settings and keep it in the
encrypted vault.

IMPORTANT - set CAPABILITY correctly. It decides what the user has to approve.
If your plugin spends money, posts publicly, or sends messages, say so. That's
what keeps JARVIS from doing it behind their back at 3am.
"""

from __future__ import annotations

import asyncio
from typing import Any

import requests

from ..core.grants import CAP_WEB_READ  # see grants.py for the full list
from ..core.tools import ExecContext
from .base import Plugin, PluginError


class YourToolPlugin(Plugin):
    NAME = "your_tool"
    DESCRIPTION = ("What this does, written for Claude so it knows when to reach "
                   "for it. One clear sentence beats three vague ones.")
    REQUIRES_KEYS = ["YOUR_API_KEY"]        # [] if none needed
    REQUIRES_PACKAGES = []                  # e.g. ["replicate"]

    SCHEMA = {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "What to send"},
            "count": {"type": "integer", "description": "How many", "default": 1},
        },
        "required": ["prompt"],
    }

    # What permission does this need? None = harmless.
    #   CAP_PAYMENT_SPEND  - costs money
    #   CAP_WEB_PUBLISH    - posts or uploads publicly
    #   CAP_COMMS_SEND     - sends an email or message
    #   CAP_FS_WRITE       - writes files
    CAPABILITY = CAP_WEB_READ
    RESOURCE_KEY = "prompt"     # which argument names the target
    AMOUNT_KEY = None           # which argument holds a dollar amount, if any

    async def run(self, params: dict[str, Any], context: ExecContext) -> Any:
        prompt = params["prompt"]
        api_key = self.key("YOUR_API_KEY")

        def _call() -> dict[str, Any]:
            response = requests.post(
                "https://api.example.com/v1/do-the-thing",
                headers={"Authorization": f"Bearer {api_key}"},
                json={"prompt": prompt},
                timeout=120,
            )
            if response.status_code >= 400:
                raise PluginError(f"example.com said {response.status_code}: "
                                  f"{response.text[:200]}")
            return response.json()

        # Keep blocking calls off the event loop, or voice stutters.
        data = await asyncio.to_thread(_call)

        # Return anything JSON-serialisable. A mission step stores it under the
        # step's output_key, so later steps can reference {your_output}.
        return {"result": data.get("output", ""), "prompt": prompt}
