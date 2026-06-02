from __future__ import annotations

import time
from typing import Any

from .base import ToolContext, ToolResult

SKILL_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "skill": {"type": "string", "description": "Name of the skill to invoke."},
        "args": {
            "type": "string",
            "description": "Optional argument string forwarded to the skill body.",
        },
    },
    "required": ["skill"],
}


class SkillTool:
    name = "Skill"
    description = (
        "Invoke a skill by name. Skills are listed in the skill_listing system-reminder. "
        "The skill's body is returned to you so you can act on its instructions in the next turn."
    )
    input_schema = SKILL_TOOL_SCHEMA
    scope = "exec"
    parallel_safe = False

    def __init__(
        self,
        skills: dict[str, Any],
        session_registry: dict[str, Any] | None = None,
        get_session_model: Any = None,
    ) -> None:
        self._skills = skills
        self._session_registry = session_registry if session_registry is not None else {}
        self._get_session_model = get_session_model

    def validate(self, raw: dict[str, Any]) -> dict[str, Any]:
        skill = raw.get("skill")
        if not isinstance(skill, str) or skill.strip() == "":
            raise ValueError("skill must be a non-empty string")
        args = raw.get("args")
        if args is not None and not isinstance(args, str):
            raise ValueError("args must be a string")
        result = {"skill": skill}
        if args is not None:
            result["args"] = args
        return result

    def summarize(self, input: dict[str, Any]) -> str:
        return f"Invoke skill: {input['skill']}"

    async def execute(self, input: dict[str, Any], ctx: ToolContext) -> ToolResult:
        from linch.skills.overlay import resolve_model_override
        from linch.skills.substitute import substitute_skill_body
        from linch.types import InvokedSkillRecord, SkillOverlay

        requested = input["skill"]
        if requested.startswith("/"):
            requested = requested[1:]

        skill = self._skills.get(requested)
        if skill is None:
            return ToolResult(
                content=f"Unknown skill: {requested}",
                summary=f"Invoke skill: {requested}",
                is_error=True,
            )

        if skill.frontmatter.disable_model_invocation:
            return ToolResult(
                content=f"Skill is not invokable by the model: {skill.name}",
                summary=f"Invoke skill: {skill.name}",
                is_error=True,
            )

        args = input.get("args", "")
        substituted = substitute_skill_body(skill, args, ctx.session_id)

        session = self._session_registry.get(ctx.session_id) if self._session_registry else None
        if session is not None:
            session.invoked_skills.append(
                InvokedSkillRecord(
                    name=skill.name,
                    substituted_body=substituted,
                    invoked_at=time.time(),
                )
            )

            try:
                await ctx.session_store.set_invoked_skills(
                    ctx.session_id,
                    [
                        {
                            "name": rec.name,
                            "substituted_body": rec.substituted_body,
                            "invoked_at": rec.invoked_at,
                        }
                        for rec in session.invoked_skills
                    ],
                )
            except Exception:
                pass

            allowed_tools = skill.frontmatter.allowed_tools
            model = skill.frontmatter.model

            overlay_d: dict[str, Any] = {}
            if allowed_tools and len(allowed_tools) > 0:
                overlay_d["allowed_tools"] = list(allowed_tools)
            if model:
                session_model = (
                    self._get_session_model(ctx.session_id) if self._get_session_model else ""
                )
                overlay_d["model_override"] = resolve_model_override(model, session_model)
            if overlay_d:
                session.pending_skill_overlay = SkillOverlay(**overlay_d)

        return ToolResult(
            content=substituted,
            summary=f"Invoked skill: {skill.name}",
            is_error=False,
        )
