"""Tell the user something - desktop toast or email.

Email counts as sending a message as the user, so it needs explicit
authorisation. A desktop notification on their own machine does not.
"""

from __future__ import annotations

import asyncio
import smtplib
from email.message import EmailMessage
from typing import Any

from ..core.grants import CAP_COMMS_SEND
from ..core.tools import ExecContext
from .base import Plugin, PluginError


class NotifyPlugin(Plugin):
    NAME = "notify"
    DESCRIPTION = ("Send the user a desktop notification, or an email when method="
                   "'email'. Use it to report what an overnight mission produced.")
    CAPABILITY = None          # decided per call in run(); see _needs_permission
    RESOURCE_KEY = "to"

    SCHEMA = {
        "type": "object",
        "properties": {
            "message": {"type": "string"},
            "title": {"type": "string"},
            "method": {"type": "string", "enum": ["desktop", "email", "auto"]},
            "to": {"type": "string", "description": "Email address (email only)"},
        },
        "required": ["message"],
    }

    def available(self) -> bool:
        return True

    async def run(self, params: dict[str, Any], context: ExecContext) -> Any:
        message = str(params.get("message", "")).strip()
        if not message:
            raise PluginError("Nothing to say.")
        title = params.get("title") or "JARVIS"
        method = str(params.get("method", "auto")).lower()

        if method == "auto":
            method = "email" if self._email_configured() else "desktop"

        if method == "desktop":
            computer = getattr(self.app, "computer", None)
            if computer is None:
                raise PluginError("No desktop session available.")
            return await computer.notify(title, message)

        return await self._send_email(params, title, message, context)

    # ------------------------------------------------------------------ #

    def _email_configured(self) -> bool:
        return bool(self.settings.get("notifications.smtp_user")
                    and self.config.key("SMTP_PASSWORD")
                    and self.settings.get("notifications.email_to"))

    async def _send_email(self, params: dict[str, Any], title: str, message: str,
                          context: ExecContext) -> str:
        recipient = params.get("to") or self.settings.get("notifications.email_to")
        if not recipient:
            raise PluginError("No recipient address configured for email.")

        # Emailing is sending a message as the user - always gated.
        broker = getattr(self.app, "broker", None)
        if broker is not None:
            from ..core.permissions import Request
            await broker.require(Request(
                capability=CAP_COMMS_SEND,
                resource=str(recipient),
                summary=f"email '{title}' to {recipient}",
                actor=context.actor,
                run_id=context.run_id,
            ))

        host = self.settings.get("notifications.smtp_host", "smtp.gmail.com")
        port = int(self.settings.get("notifications.smtp_port", 587))
        user = self.settings.get("notifications.smtp_user")
        password = self.config.key("SMTP_PASSWORD")
        if not (user and password):
            raise PluginError(
                "Email isn't set up. Add your address and an app password in "
                "Settings - Gmail needs an App Password, not your normal login.")

        def _send() -> str:
            email = EmailMessage()
            email["Subject"] = title
            email["From"] = user
            email["To"] = recipient
            email.set_content(message)
            try:
                with smtplib.SMTP(host, port, timeout=45) as server:
                    server.starttls()
                    server.login(user, password)
                    server.send_message(email)
            except smtplib.SMTPAuthenticationError as exc:
                raise PluginError(
                    "The mail server rejected those credentials. For Gmail you need "
                    "an App Password with 2FA enabled.") from exc
            except OSError as exc:
                raise PluginError(f"Couldn't reach the mail server: {exc}") from exc
            return f"Emailed {recipient}: {title}"

        return await asyncio.to_thread(_send)
