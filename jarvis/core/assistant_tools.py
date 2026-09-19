"""Tools that let JARVIS manage its own missions and approvals by voice."""

from __future__ import annotations

from typing import Any

from .mission import Mission, MissionError
from .tools import ExecContext, ToolRegistry

STR = {"type": "string"}


def register_mission_tools(registry: ToolRegistry, app) -> None:
    """Give Claude the ability to list, inspect, run and write missions."""

    def list_missions(params: dict[str, Any]) -> list[dict[str, Any]]:
        return app.scheduler.next_runs()

    registry.add(
        "list_missions",
        "List every saved mission with its schedule, last result and next run.",
        {"type": "object", "properties": {}},
        list_missions,
    )

    def describe_mission(params: dict[str, Any]) -> dict[str, Any]:
        mission = app.scheduler.get(params["mission_id"])
        if mission is None:
            return {"error": f"No mission called {params['mission_id']!r}."}
        recent = app.memory.runs(mission.id, limit=5)
        return {"mission": mission.to_dict(), "recent_runs": recent,
                "problems": app.scheduler.check(mission)}

    registry.add(
        "describe_mission",
        "Show a mission's steps, recent runs and any problems with it.",
        {"type": "object", "properties": {"mission_id": STR},
         "required": ["mission_id"]},
        describe_mission,
    )

    async def run_mission(params: dict[str, Any], context: ExecContext) -> dict[str, Any]:
        result = await app.scheduler.run(params["mission_id"], trigger="voice",
                                         unattended=context.unattended)
        return {"status": result.status, "summary": result.summary(),
                "run_id": result.run_id, "seconds": result.seconds}

    registry.add(
        "run_mission",
        "Run a saved mission right now. Individual steps are still permission "
        "checked, so this can't be used to skip approval.",
        {"type": "object", "properties": {"mission_id": STR},
         "required": ["mission_id"]},
        run_mission,
    )

    def save_mission(params: dict[str, Any]) -> str:
        definition = params.get("mission")
        if isinstance(definition, str):
            import json
            try:
                definition = json.loads(definition)
            except json.JSONDecodeError as exc:
                return f"That mission JSON doesn't parse: {exc}"
        try:
            mission = Mission.from_dict(definition or {})
        except MissionError as exc:
            return f"I couldn't build that mission: {exc}"

        # A mission JARVIS writes must not grant itself permissions. Only a file
        # the user edits by hand (or approves in the builder) may carry those.
        if mission.authorizations:
            mission.authorizations = []

        problems = app.scheduler.check(mission)
        if problems:
            return "That mission has problems I need you to fix first:\n- " + \
                   "\n- ".join(problems)

        path = app.scheduler.save(mission)
        return (f"Saved '{mission.name}' to {path}. "
                f"{'It will run on ' + mission.schedule if mission.schedule else 'Run it manually when you want.'} "
                f"If it needs permission to spend or post, add that in the Mission "
                f"Builder - I can't grant it to myself.")

    registry.add(
        "save_mission",
        "Create or update a saved mission from a JSON definition. Use this when "
        "the user describes a repeatable job. Fields: id, name, schedule (cron), "
        "steps[{name, plugin, params, output_key}]. Reference an earlier step's "
        "output with {output_key}.",
        {"type": "object",
         "properties": {"mission": {"type": "object",
                                    "description": "The mission definition"}},
         "required": ["mission"]},
        # Deliberately ungated: saving a mission changes nothing on its own.
        # Authorisations are stripped above, and every step is permission-checked
        # when it actually runs. Gating this would only make "build me an
        # automation" - the main thing people want - needlessly painful.
        save_mission,
    )

    def mission_history(params: dict[str, Any]) -> dict[str, Any]:
        run_id = params.get("run_id")
        if run_id:
            return {"run": app.memory.runs(limit=50),
                    "steps": app.memory.run_steps(int(run_id))}
        return {"runs": app.memory.runs(params.get("mission_id"),
                                        int(params.get("limit", 10)))}

    registry.add(
        "mission_history",
        "Look at what missions have done recently - useful for 'what happened "
        "last night?'",
        {"type": "object",
         "properties": {"mission_id": STR, "run_id": {"type": "integer"},
                        "limit": {"type": "integer"}}},
        mission_history,
    )

    def list_approvals(params: dict[str, Any]) -> list[dict[str, Any]]:
        return [{"id": item["id"], "what": item["summary"], "risk": item["risk"],
                 "asked_at": item["at"], "requested_by": item["requested_by"]}
                for item in app.broker.pending()]

    registry.add(
        "list_approvals",
        "List the actions waiting for the user's approval. Read these out when "
        "asked what needs their attention.",
        {"type": "object", "properties": {}},
        list_approvals,
    )
