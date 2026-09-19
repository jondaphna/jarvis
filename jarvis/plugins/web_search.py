"""Web search with graceful degradation - works with no API key at all.

Order of preference: Exa (best for research), Tavily (best for news), then a
keyless DuckDuckGo fallback so search never simply stops working. Whichever
backend answers, the result shape is identical, so missions don't care.
"""

from __future__ import annotations

import asyncio
import html
import re
from typing import Any
from urllib.parse import quote_plus

import requests

from ..core.events import log
from ..core.grants import CAP_WEB_SEARCH
from ..core.tools import ExecContext
from .base import Plugin, PluginError


class WebSearchPlugin(Plugin):
    NAME = "web_search"
    DESCRIPTION = (
        "Search the web and get back titles, URLs and text snippets. Use this for "
        "anything you don't already know, anything that changed recently, and "
        "before making claims about prices, trends or current events.")
    REQUIRES_KEYS: list[str] = []          # keys improve it; none are required
    CAPABILITY = CAP_WEB_SEARCH
    RESOURCE_KEY = "query"

    SCHEMA = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to search for"},
            "num_results": {"type": "integer", "description": "How many (default 6)"},
            "depth": {"type": "string", "enum": ["quick", "deep"],
                      "description": "'deep' pulls full page text where available"},
        },
        "required": ["query"],
    }

    def available(self) -> bool:
        return True                        # the keyless fallback always works

    def missing_requirements(self) -> list[str]:
        return []

    async def run(self, params: dict[str, Any], context: ExecContext) -> Any:
        query = str(params.get("query", "")).strip()
        if not query:
            raise PluginError("No search query given.")
        limit = max(1, min(int(params.get("num_results", 6)), 25))
        deep = str(params.get("depth", "quick")).lower() == "deep"

        attempts: list[tuple[str, Any]] = []
        if self.key("EXA_API_KEY"):
            attempts.append(("exa", self._exa))
        if self.key("TAVILY_API_KEY"):
            attempts.append(("tavily", self._tavily))
        attempts.append(("duckduckgo", self._duckduckgo))

        errors = []
        for name, backend in attempts:
            try:
                results = await asyncio.to_thread(backend, query, limit, deep)
                if results:
                    return {"query": query, "engine": name, "results": results}
                errors.append(f"{name}: no results")
            except Exception as exc:
                log.info("search backend %s failed: %s", name, exc)
                errors.append(f"{name}: {exc}")

        raise PluginError("Every search backend failed - " + "; ".join(errors))

    # ------------------------------------------------------------------ #
    # Backends
    # ------------------------------------------------------------------ #

    def _exa(self, query: str, limit: int, deep: bool) -> list[dict[str, Any]]:
        response = requests.post(
            "https://api.exa.ai/search",
            headers={"x-api-key": self.key("EXA_API_KEY") or "",
                     "Content-Type": "application/json"},
            json={
                "query": query,
                "numResults": limit,
                "type": "auto",
                "contents": {"text": {"maxCharacters": 3000 if deep else 800}},
            },
            timeout=45,
        )
        if response.status_code >= 400:
            raise PluginError(f"Exa returned {response.status_code}: {response.text[:200]}")
        return [
            {
                "title": item.get("title") or "",
                "url": item.get("url") or "",
                "snippet": (item.get("text") or "")[:3000],
                "published": item.get("publishedDate") or "",
            }
            for item in response.json().get("results", [])
        ]

    def _tavily(self, query: str, limit: int, deep: bool) -> list[dict[str, Any]]:
        response = requests.post(
            "https://api.tavily.com/search",
            json={
                "api_key": self.key("TAVILY_API_KEY"),
                "query": query,
                "max_results": limit,
                "search_depth": "advanced" if deep else "basic",
                "include_answer": True,
            },
            timeout=45,
        )
        if response.status_code >= 400:
            raise PluginError(f"Tavily returned {response.status_code}: {response.text[:200]}")
        data = response.json()
        results = [
            {
                "title": item.get("title") or "",
                "url": item.get("url") or "",
                "snippet": (item.get("content") or "")[:3000],
                "score": item.get("score"),
            }
            for item in data.get("results", [])
        ]
        if data.get("answer"):
            results.insert(0, {"title": "Tavily summary", "url": "",
                               "snippet": data["answer"]})
        return results

    def _duckduckgo(self, query: str, limit: int, deep: bool) -> list[dict[str, Any]]:
        """Keyless fallback. Scrapes the no-JS endpoint - fine for light use."""
        response = requests.get(
            f"https://html.duckduckgo.com/html/?q={quote_plus(query)}",
            headers={"User-Agent": "Mozilla/5.0 (compatible; JARVIS/1.0)"},
            timeout=30,
        )
        response.raise_for_status()
        body = response.text

        results: list[dict[str, Any]] = []
        pattern = re.compile(
            r'<a rel="nofollow" class="result__a" href="(?P<url>[^"]+)".*?>(?P<title>.*?)</a>'
            r'.*?class="result__snippet"[^>]*>(?P<snippet>.*?)</a>',
            re.DOTALL,
        )
        for match in pattern.finditer(body):
            results.append({
                "title": _strip_html(match.group("title")),
                "url": _unwrap_ddg(match.group("url")),
                "snippet": _strip_html(match.group("snippet"))[:1200],
            })
            if len(results) >= limit:
                break
        return results


def _strip_html(raw: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", raw)).strip()


def _unwrap_ddg(href: str) -> str:
    """DuckDuckGo wraps outbound links in a redirect; unwrap to the real URL."""
    from urllib.parse import parse_qs, unquote, urlparse

    if "duckduckgo.com/l/" in href or href.startswith("//duckduckgo.com/l/"):
        query = parse_qs(urlparse("https:" + href if href.startswith("//") else href).query)
        target = query.get("uddg", [""])[0]
        if target:
            return unquote(target)
    return href
