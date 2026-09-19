"""Fetch and read a web page as text. Read-only by design."""

from __future__ import annotations

import asyncio
import html
import re
from typing import Any

import requests

from ..core.grants import CAP_WEB_READ
from ..core.tools import ExecContext
from .base import Plugin, PluginError


class WebFetchPlugin(Plugin):
    NAME = "read_webpage"
    DESCRIPTION = ("Fetch a web page and return its readable text. Use after "
                   "web_search when you need the full article rather than a snippet.")
    CAPABILITY = CAP_WEB_READ
    RESOURCE_KEY = "url"

    SCHEMA = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Page to read"},
            "max_chars": {"type": "integer", "description": "Default 12000"},
        },
        "required": ["url"],
    }

    def available(self) -> bool:
        return True

    async def run(self, params: dict[str, Any], context: ExecContext) -> Any:
        url = str(params.get("url", "")).strip()
        if not url:
            raise PluginError("No URL given.")
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        limit = int(params.get("max_chars", 12000))

        def _fetch() -> dict[str, Any]:
            response = requests.get(
                url,
                headers={"User-Agent": "Mozilla/5.0 (compatible; JARVIS/1.0)"},
                timeout=45,
                allow_redirects=True,
            )
            if response.status_code >= 400:
                raise PluginError(f"{url} returned {response.status_code}.")

            content_type = response.headers.get("content-type", "")
            if "html" not in content_type and "text" not in content_type:
                raise PluginError(
                    f"{url} is {content_type or 'not text'}, so there's nothing to read.")

            text = _readable_text(response.text)
            title_match = re.search(r"<title[^>]*>(.*?)</title>", response.text,
                                    re.IGNORECASE | re.DOTALL)
            return {
                "url": response.url,
                "title": html.unescape(title_match.group(1)).strip() if title_match else "",
                "text": text[:limit],
                "truncated": len(text) > limit,
                "chars": len(text),
            }

        return await asyncio.to_thread(_fetch)


def _readable_text(raw: str) -> str:
    """Strip a page down to its prose. Crude, dependency-free, good enough."""
    body = re.sub(r"(?is)<(script|style|noscript|svg|head|nav|footer|form)[^>]*>.*?</\1>",
                  " ", raw)
    body = re.sub(r"(?i)<br\s*/?>", "\n", body)
    body = re.sub(r"(?i)</(p|div|li|h[1-6]|tr|section|article)>", "\n", body)
    body = re.sub(r"<[^>]+>", " ", body)
    body = html.unescape(body)
    body = re.sub(r"[ \t\xa0]+", " ", body)
    body = re.sub(r"\n\s*\n\s*\n+", "\n\n", body)
    return body.strip()
