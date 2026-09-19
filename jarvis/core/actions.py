"""Core tools - the things JARVIS can do without any plugin installed.

Every entry declares the capability it exercises and which argument names the
resource. The registry uses that to get a permission ruling before the handler
runs, so a new tool cannot accidentally arrive ungated: forgetting `capability`
is a visible omission in this file, not a silent hole somewhere else.
"""

from __future__ import annotations

from typing import Any

from .computer import Computer
from .grants import (
    CAP_APP_LAUNCH, CAP_FS_DELETE, CAP_FS_READ, CAP_FS_WRITE, CAP_INPUT_CONTROL,
    CAP_SCREEN_CAPTURE, CAP_SHELL_EXEC, CAP_SYSTEM_CONFIG, CAP_WEB_READ,
)
from .tools import ExecContext, ToolRegistry

STR = {"type": "string"}


def register_core_tools(registry: ToolRegistry, computer: Computer, memory,
                        broker) -> None:
    """Wire the built-in tools into the registry."""

    # ------------------------------------------------------------------ #
    # Applications and the browser
    # ------------------------------------------------------------------ #

    async def open_app(params: dict[str, Any]) -> str:
        return await computer.open_app(params["name"])

    registry.add(
        "open_app",
        "Open an application or website by name - 'chrome', 'excel', 'photoshop', "
        "'gmail', 'tiktok'. Use this whenever the user asks you to open, launch or "
        "start something.",
        {"type": "object",
         "properties": {"name": {**STR, "description": "App or site name"}},
         "required": ["name"]},
        open_app, capability=CAP_APP_LAUNCH, resource_key="name",
    )

    async def open_url(params: dict[str, Any]) -> str:
        return await computer.open_url(params["url"])

    registry.add(
        "open_url",
        "Open a specific URL in the default browser.",
        {"type": "object",
         "properties": {"url": {**STR, "description": "Full URL"}},
         "required": ["url"]},
        open_url, capability=CAP_WEB_READ, resource_key="url",
    )

    # ------------------------------------------------------------------ #
    # Files
    # ------------------------------------------------------------------ #

    def list_files(params: dict[str, Any]) -> list[dict[str, Any]]:
        return computer.list_dir(params.get("path", ""), params.get("pattern", "*"))

    registry.add(
        "list_files",
        "List files and folders. A bare name is interpreted relative to the "
        "workspace folder.",
        {"type": "object",
         "properties": {"path": {**STR, "description": "Folder path (default: workspace)"},
                        "pattern": {**STR, "description": "Glob such as *.mp4"}}},
        list_files, capability=CAP_FS_READ, resource_key="path", path_keys=("path",),
    )

    def read_file(params: dict[str, Any]) -> str:
        return computer.read_file(params["path"], int(params.get("max_chars", 200_000)))

    registry.add(
        "read_file",
        "Read a text file and return its contents.",
        {"type": "object",
         "properties": {"path": STR, "max_chars": {"type": "integer"}},
         "required": ["path"]},
        read_file, capability=CAP_FS_READ, resource_key="path", path_keys=("path",),
    )

    def write_file(params: dict[str, Any]) -> str:
        return computer.write_file(params["path"], params.get("content", ""),
                                   append=bool(params.get("append")))

    registry.add(
        "write_file",
        "Create or overwrite a text file. Set append=true to add to the end instead. "
        "Writing inside the workspace folder needs no approval.",
        {"type": "object",
         "properties": {"path": STR, "content": STR, "append": {"type": "boolean"}},
         "required": ["path", "content"]},
        write_file, capability=CAP_FS_WRITE, resource_key="path", path_keys=("path",),
    )

    def delete_file(params: dict[str, Any]) -> str:
        return computer.delete_path(params["path"])

    registry.add(
        "delete_file",
        "Delete a file or folder. Irreversible - say what you're deleting first.",
        {"type": "object", "properties": {"path": STR}, "required": ["path"]},
        delete_file, capability=CAP_FS_DELETE, resource_key="path", path_keys=("path",),
    )

    def move_file(params: dict[str, Any]) -> str:
        return computer.move_path(params["source"], params["destination"])

    registry.add(
        "move_file",
        "Move or rename a file or folder.",
        {"type": "object", "properties": {"source": STR, "destination": STR},
         "required": ["source", "destination"]},
        # Judged on the source: moving a file removes it from where it was.
        move_file, capability=CAP_FS_DELETE, resource_key="source",
        also_requires=((CAP_FS_WRITE, "destination"),),
        path_keys=("source", "destination"),
    )

    def copy_file(params: dict[str, Any]) -> str:
        return computer.copy_path(params["source"], params["destination"])

    registry.add(
        "copy_file",
        "Copy a file or folder to another location.",
        {"type": "object", "properties": {"source": STR, "destination": STR},
         "required": ["source", "destination"]},
        copy_file, capability=CAP_FS_WRITE, resource_key="destination",
        path_keys=("source", "destination"),
    )

    def find_files(params: dict[str, Any]) -> list[str]:
        return computer.find_files(params.get("pattern", "*"), params.get("root", ""),
                                   int(params.get("limit", 200)))

    registry.add(
        "find_files",
        "Find files matching a glob pattern, searching subfolders.",
        {"type": "object",
         "properties": {"pattern": STR, "root": STR, "limit": {"type": "integer"}},
         "required": ["pattern"]},
        find_files, capability=CAP_FS_READ, resource_key="root", path_keys=("root",),
    )

    def search_in_files(params: dict[str, Any]) -> list[dict[str, Any]]:
        return computer.search_in_files(params["text"], params.get("root", ""),
                                        params.get("pattern", "*.*"),
                                        int(params.get("limit", 50)))

    registry.add(
        "search_in_files",
        "Search inside files for a piece of text and return the matching lines.",
        {"type": "object",
         "properties": {"text": STR, "root": STR, "pattern": STR,
                        "limit": {"type": "integer"}},
         "required": ["text"]},
        search_in_files, capability=CAP_FS_READ, resource_key="root", path_keys=("root",),
    )

    # ------------------------------------------------------------------ #
    # Shell
    # ------------------------------------------------------------------ #

    async def run_command(params: dict[str, Any]) -> str:
        result = await computer.run_shell(params["command"],
                                          timeout=int(params.get("timeout", 120)),
                                          cwd=params.get("cwd"))
        return result.summary()

    registry.add(
        "run_command",
        "Run a shell command and return its output. Needs approval unless the "
        "command is on the pre-approved list. Prefer a dedicated tool when one exists.",
        {"type": "object",
         "properties": {"command": STR, "cwd": STR, "timeout": {"type": "integer"}},
         "required": ["command"]},
        run_command, capability=CAP_SHELL_EXEC, resource_key="command",
    )

    # ------------------------------------------------------------------ #
    # Screen and input
    # ------------------------------------------------------------------ #

    async def take_screenshot(params: dict[str, Any]) -> str:
        path = await computer.screenshot(params.get("path"))
        memory.log_artifact(str(path), "screenshot")
        return f"Screenshot saved to {path}"

    registry.add(
        "take_screenshot",
        "Capture the screen to a PNG file.",
        {"type": "object", "properties": {"path": STR}},
        take_screenshot, capability=CAP_SCREEN_CAPTURE, resource_key="path",
        path_keys=("path",),
    )

    async def type_text(params: dict[str, Any]) -> str:
        return await computer.type_text(params["text"])

    registry.add(
        "type_text",
        "Type text on the keyboard into whatever window is focused. Only works "
        "with someone at the machine.",
        {"type": "object",
         "properties": {"text": STR, "app": {**STR, "description": "App being driven"}},
         "required": ["text"]},
        type_text, capability=CAP_INPUT_CONTROL, resource_key="app", attended_only=True,
    )

    async def press_keys(params: dict[str, Any]) -> str:
        keys = params.get("keys")
        sequence = keys if isinstance(keys, list) else str(keys).split("+")
        return await computer.press_keys(sequence)

    registry.add(
        "press_keys",
        "Press a keyboard shortcut, e.g. ['ctrl','s'].",
        {"type": "object",
         "properties": {"keys": {"type": "array", "items": STR}, "app": STR},
         "required": ["keys"]},
        press_keys, capability=CAP_INPUT_CONTROL, resource_key="app", attended_only=True,
    )

    async def click(params: dict[str, Any]) -> str:
        return await computer.click(params.get("x"), params.get("y"),
                                    params.get("button", "left"),
                                    int(params.get("clicks", 1)))

    registry.add(
        "click",
        "Click the mouse, optionally at specific screen coordinates.",
        {"type": "object",
         "properties": {"x": {"type": "integer"}, "y": {"type": "integer"},
                        "button": STR, "clicks": {"type": "integer"}, "app": STR}},
        click, capability=CAP_INPUT_CONTROL, resource_key="app", attended_only=True,
    )

    # ------------------------------------------------------------------ #
    # Memory - no permission needed, it's JARVIS's own notebook
    # ------------------------------------------------------------------ #

    def remember(params: dict[str, Any]) -> str:
        memory.remember(params["key"], params["value"],
                        params.get("category", "general"), source="jarvis")
        return f"Noted: {params['key']} = {params['value']}"

    registry.add(
        "remember",
        "Store a durable fact about the user or their business so you still know "
        "it in a week. Use short stable keys like 'business' or 'posting_schedule'.",
        {"type": "object",
         "properties": {"key": STR, "value": STR, "category": STR},
         "required": ["key", "value"]},
        remember,
    )

    def recall(params: dict[str, Any]) -> str:
        key = params.get("key")
        if key:
            return memory.recall(key) or f"I don't have anything stored for {key!r}."
        return memory.facts_block() or "I haven't stored any facts yet."

    registry.add(
        "recall",
        "Look up what you've stored. Omit the key to list everything you know.",
        {"type": "object", "properties": {"key": STR}},
        recall,
    )

    def forget(params: dict[str, Any]) -> str:
        return ("Forgotten." if memory.forget(params["key"])
                else f"I had nothing stored for {params['key']!r}.")

    registry.add(
        "forget",
        "Delete a stored fact.",
        {"type": "object", "properties": {"key": STR}, "required": ["key"]},
        forget,
    )

    def search_history(params: dict[str, Any]) -> list[dict[str, Any]]:
        return memory.search_messages(params["query"], int(params.get("limit", 20)))

    registry.add(
        "search_history",
        "Search past conversations for something that was said before.",
        {"type": "object", "properties": {"query": STR, "limit": {"type": "integer"}},
         "required": ["query"]},
        search_history,
    )

    # ------------------------------------------------------------------ #
    # Status and notifications
    # ------------------------------------------------------------------ #

    async def notify_user(params: dict[str, Any]) -> str:
        return await computer.notify(params.get("title", "JARVIS"), params["message"])

    registry.add(
        "notify_user",
        "Show a desktop notification. Good for telling the user an overnight "
        "mission finished.",
        {"type": "object", "properties": {"title": STR, "message": STR},
         "required": ["message"]},
        notify_user,
    )

    def system_status(params: dict[str, Any]) -> dict[str, Any]:
        info = computer.system_info()
        info["stats"] = memory.stats()
        info["permissions"] = {
            "workspace": str(broker.settings.workspace),
            "allowed_apps": broker.settings.get("autonomy.allowed_apps", []),
            "active_grants": [g.describe() for g in broker.active_grants()],
            "pending_approvals": len(broker.pending()),
        }
        return info

    registry.add(
        "system_status",
        "Report machine state, what you've done recently, spend, and your current "
        "permissions. Use this when asked 'what have you been doing' or 'what can you do'.",
        {"type": "object", "properties": {}},
        system_status,
    )

    # ------------------------------------------------------------------ #
    # Permissions - asking, never self-granting
    # ------------------------------------------------------------------ #

    def request_permission(params: dict[str, Any], context: ExecContext) -> str:
        approval_id = memory.queue_approval(
            capability=params.get("capability", "unspecified"),
            resource=params.get("resource"),
            risk="high",
            summary=params["what"],
            requested_by=context.actor,
            payload={"why": params.get("why", ""), "run_id": context.run_id},
        )
        return (f"Added to the approval queue as #{approval_id}. Tell the user what "
                f"you need and why - they clear it with `jarvis approve {approval_id}` "
                f"or from the dashboard.")

    registry.add(
        "request_permission",
        "Ask the user for permission you don't have. Use this instead of retrying a "
        "blocked action. It queues the request; it does NOT grant anything.",
        {"type": "object",
         "properties": {"what": {**STR, "description": "What you want to do, plainly"},
                        "why": STR, "capability": STR, "resource": STR},
         "required": ["what"]},
        request_permission,
    )

    def allow_app(params: dict[str, Any]) -> str:
        broker.allow_app(params["name"])
        return f"{params['name']} added to the approved app list."

    registry.add(
        "allow_app",
        "Add an app to the standing allowlist. Only call this when the user has "
        "just told you they want you to use that app.",
        {"type": "object", "properties": {"name": STR}, "required": ["name"]},
        # Changing standing policy is high-risk: needs the user's explicit say-so.
        allow_app, capability=CAP_SYSTEM_CONFIG, resource_key="name",
    )
