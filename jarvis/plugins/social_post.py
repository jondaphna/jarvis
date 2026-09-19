"""Publish to TikTok, Instagram, YouTube - the highest-risk plugin here.

Two modes, because the platforms' posting APIs need an approved developer app
that most people won't have on day one:

* **api**     - real upload, using a token you've stored. Needs setup.
* **prepare** - stages the file and caption in one folder and opens the upload
                page, so posting is two clicks. Works immediately, no approval.

Default is `auto`: use the API when a token exists, otherwise prepare. Either
way this is CAP_WEB_PUBLISH, so it never runs unless the user authorised it in
the request that started the work.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

from ..core.grants import CAP_WEB_PUBLISH
from ..core.tools import ExecContext
from .base import Plugin, PluginError

UPLOAD_PAGES = {
    "tiktok": "https://www.tiktok.com/upload",
    "instagram": "https://www.instagram.com",
    "youtube": "https://studio.youtube.com/channel/UC/videos/upload",
    "x": "https://x.com/compose/post",
    "linkedin": "https://www.linkedin.com/feed/",
}

TOKEN_KEYS = {
    "tiktok": "TIKTOK_ACCESS_TOKEN",
    "instagram": "INSTAGRAM_ACCESS_TOKEN",
    "youtube": "YOUTUBE_ACCESS_TOKEN",
}


class SocialPostPlugin(Plugin):
    NAME = "post_to_social"
    DESCRIPTION = (
        "Publish a video or image to TikTok, Instagram or YouTube. This posts "
        "publicly under the user's name, so it only runs when they have explicitly "
        "authorised it in their request. Without a platform token it stages the "
        "file and caption for a two-click manual upload instead.")
    CAPABILITY = CAP_WEB_PUBLISH
    RESOURCE_KEY = "platform"

    SCHEMA = {
        "type": "object",
        "properties": {
            "platform": {"type": "string",
                         "enum": ["tiktok", "instagram", "youtube", "x", "linkedin"]},
            "file": {"type": "string", "description": "Path to the video or image"},
            "caption": {"type": "string", "description": "Caption or description"},
            "title": {"type": "string", "description": "Title (YouTube)"},
            "hashtags": {"anyOf": [{"type": "string"},
                                   {"type": "array", "items": {"type": "string"}}]},
            "mode": {"type": "string", "enum": ["auto", "api", "prepare"]},
            "privacy": {"type": "string", "enum": ["public", "private", "unlisted"]},
        },
        "required": ["platform", "file"],
    }

    def available(self) -> bool:
        return True            # prepare mode always works

    async def run(self, params: dict[str, Any], context: ExecContext) -> Any:
        platform = str(params.get("platform", "")).lower().strip()
        if platform not in UPLOAD_PAGES:
            raise PluginError(f"I don't know how to post to {platform!r}.")

        source = Path(str(params.get("file", ""))).expanduser()
        if not source.exists():
            raise PluginError(f"{source} doesn't exist, so there's nothing to post.")

        caption = _caption(params)
        mode = str(params.get("mode", "auto")).lower()
        token = self.key(TOKEN_KEYS.get(platform, "")) if platform in TOKEN_KEYS else None

        if mode == "auto":
            mode = "api" if token else "prepare"

        if mode == "api":
            if not token:
                raise PluginError(
                    f"No {platform} token stored, so I can't post through the API. "
                    f"Add {TOKEN_KEYS.get(platform)} in Settings, or use mode='prepare' "
                    f"and I'll stage it for a manual upload.")
            result = await self._post_via_api(platform, token, source, caption, params)
        else:
            result = await self._prepare(platform, source, caption, params)

        if getattr(self.app, "memory", None):
            self.app.memory.log_artifact(str(source), f"post:{platform}", context.run_id,
                                         note=caption[:200])
        return result

    # ------------------------------------------------------------------ #
    # Manual staging - works with zero platform setup
    # ------------------------------------------------------------------ #

    async def _prepare(self, platform: str, source: Path, caption: str,
                       params: dict[str, Any]) -> dict[str, Any]:
        computer = getattr(self.app, "computer", None)
        base = (computer.output_dir() if computer else Path.cwd()) / "to_post" / platform
        base.mkdir(parents=True, exist_ok=True)

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        staged = base / f"{stamp}{source.suffix}"

        def _stage() -> None:
            import shutil
            shutil.copy2(source, staged)
            staged.with_suffix(".txt").write_text(caption, encoding="utf-8")
            (base / f"{stamp}.json").write_text(json.dumps({
                "platform": platform,
                "file": str(staged),
                "caption": caption,
                "title": params.get("title", ""),
                "privacy": params.get("privacy", "public"),
                "prepared_at": datetime.now().isoformat(timespec="seconds"),
            }, indent=2), encoding="utf-8")

        await asyncio.to_thread(_stage)

        return {
            "status": "prepared",
            "platform": platform,
            "file": str(staged),
            "caption": caption,
            "upload_page": UPLOAD_PAGES[platform],
            "note": (f"Ready to post. The video and its caption are in {base}. "
                     f"Open {UPLOAD_PAGES[platform]}, drop the file in, paste the "
                     f"caption from the .txt beside it."),
        }

    # ------------------------------------------------------------------ #
    # Real uploads
    # ------------------------------------------------------------------ #

    async def _post_via_api(self, platform: str, token: str, source: Path,
                            caption: str, params: dict[str, Any]) -> dict[str, Any]:
        if platform == "youtube":
            return await asyncio.to_thread(self._youtube, token, source, caption, params)
        if platform == "tiktok":
            return await asyncio.to_thread(self._tiktok, token, source, caption)
        if platform == "instagram":
            return await asyncio.to_thread(self._instagram, token, caption, params)
        raise PluginError(f"No API upload implemented for {platform} yet - "
                          f"use mode='prepare'.")

    def _youtube(self, token: str, source: Path, caption: str,
                 params: dict[str, Any]) -> dict[str, Any]:
        metadata = {
            "snippet": {
                "title": (params.get("title") or source.stem)[:100],
                "description": caption[:5000],
                "categoryId": "22",
            },
            "status": {"privacyStatus": params.get("privacy", "private"),
                       "selfDeclaredMadeForKids": False},
        }
        start = requests.post(
            "https://www.googleapis.com/upload/youtube/v3/videos"
            "?uploadType=resumable&part=snippet,status",
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json; charset=UTF-8",
                     "X-Upload-Content-Type": "video/*"},
            json=metadata, timeout=60,
        )
        if start.status_code >= 400:
            raise PluginError(f"YouTube rejected the upload ({start.status_code}): "
                              f"{start.text[:300]}")
        session_url = start.headers.get("Location")
        if not session_url:
            raise PluginError("YouTube didn't return an upload session URL.")

        with open(source, "rb") as handle:
            upload = requests.put(
                session_url,
                headers={"Authorization": f"Bearer {token}",
                         "Content-Type": "video/*"},
                data=handle, timeout=3600,
            )
        if upload.status_code >= 400:
            raise PluginError(f"YouTube upload failed ({upload.status_code}): "
                              f"{upload.text[:300]}")

        video_id = upload.json().get("id", "")
        return {"status": "posted", "platform": "youtube", "id": video_id,
                "url": f"https://youtu.be/{video_id}" if video_id else ""}

    def _tiktok(self, token: str, source: Path, caption: str) -> dict[str, Any]:
        size = source.stat().st_size
        init = requests.post(
            "https://open.tiktokapis.com/v2/post/publish/video/init/",
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"},
            json={
                "post_info": {"title": caption[:2200], "privacy_level": "SELF_ONLY",
                              "disable_comment": False},
                "source_info": {"source": "FILE_UPLOAD", "video_size": size,
                                "chunk_size": size, "total_chunk_count": 1},
            },
            timeout=60,
        )
        if init.status_code >= 400:
            raise PluginError(f"TikTok init failed ({init.status_code}): {init.text[:300]}")

        data = (init.json().get("data") or {})
        upload_url = data.get("upload_url")
        if not upload_url:
            raise PluginError(f"TikTok didn't return an upload URL: {init.text[:200]}")

        with open(source, "rb") as handle:
            upload = requests.put(
                upload_url,
                headers={"Content-Range": f"bytes 0-{size - 1}/{size}",
                         "Content-Type": "video/mp4"},
                data=handle, timeout=3600,
            )
        if upload.status_code >= 400:
            raise PluginError(f"TikTok upload failed ({upload.status_code}): "
                              f"{upload.text[:300]}")

        return {"status": "posted", "platform": "tiktok",
                "publish_id": data.get("publish_id", ""),
                "note": "Uploaded to TikTok as private - open the app to publish it."}

    def _instagram(self, token: str, caption: str,
                   params: dict[str, Any]) -> dict[str, Any]:
        account = self.settings.get("social.instagram_user_id") or \
            self.key("INSTAGRAM_USER_ID")
        if not account:
            raise PluginError("Instagram needs your business account id "
                              "(INSTAGRAM_USER_ID) as well as a token.")
        media_url = params.get("media_url")
        if not media_url:
            raise PluginError(
                "Instagram's API can only publish a publicly reachable URL, not a "
                "local file. Upload it somewhere first, or use mode='prepare'.")

        create = requests.post(
            f"https://graph.facebook.com/v21.0/{account}/media",
            data={"video_url": media_url, "caption": caption[:2200],
                  "media_type": "REELS", "access_token": token},
            timeout=120,
        )
        if create.status_code >= 400:
            raise PluginError(f"Instagram rejected the media ({create.status_code}): "
                              f"{create.text[:300]}")
        creation_id = create.json().get("id")

        publish = requests.post(
            f"https://graph.facebook.com/v21.0/{account}/media_publish",
            data={"creation_id": creation_id, "access_token": token}, timeout=120,
        )
        if publish.status_code >= 400:
            raise PluginError(f"Instagram publish failed ({publish.status_code}): "
                              f"{publish.text[:300]}")
        return {"status": "posted", "platform": "instagram",
                "id": publish.json().get("id", "")}


def _caption(params: dict[str, Any]) -> str:
    caption = str(params.get("caption", "")).strip()
    tags = params.get("hashtags")
    if isinstance(tags, str):
        tags = [tags]
    if tags:
        formatted = " ".join(
            tag if str(tag).startswith("#") else f"#{tag}" for tag in tags)
        caption = f"{caption}\n\n{formatted}".strip()
    return caption
