from __future__ import annotations

from uuid import uuid4

from .base import ToolContext, ToolResult

SUBAGENT_TOOL_NAME = "Subagent"

SUBAGENT_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "description": {
            "type": "string",
            "description": "Short 3-5 word label for the spawned subagent (shown in UI).",
        },
        "prompt": {
            "type": "string",
            "description": (
                "The task for the subagent. Provide complete context"
                " — the subagent has no parent history."
            ),
        },
        "subagent_type": {
            "type": "string",
            "description": (
                "Optional: pick from the listed agent types. Omit to spawn"
                " a general-purpose subagent with full tool access."
            ),
        },
    },
    "required": ["description", "prompt"],
}


class SubagentTool:
    name = SUBAGENT_TOOL_NAME
    input_schema = SUBAGENT_TOOL_SCHEMA
    scope = "exec"
    parallel_safe = True

    def __init__(
        self,
        registry: object,
        get_session: object,
        next_default_display_name: object,
    ) -> None:
        self._registry = registry
        self._get_session = get_session
        self._next_default_display_name = next_default_display_name

    @property
    def description(self) -> str:

        header = "\n".join(
            [
                "Launch a subagent to handle a focused task.",
                "",
                "Delegation rules:",
                "- Fresh subagents start with no parent history; include complete context,",
                "  relevant files, constraints, prior findings, and the exact expected output.",
                "- Use subagents for meaningful research, implementation, or verification work.",
                "- Do not delegate synthesis with vague prompts like \"based on your findings\";",
                "  synthesize yourself, then assign specific next actions.",
                "- Launch independent research tasks in parallel when they do not depend",
                "  on each other.",
            ]
        )
        trailer = "\n".join(
            [
                "Omit `subagent_type` to spawn a general-purpose subagent with full tool access.",
                "The subagent returns a single text response; its tool calls and partial output",
                "stream into your event log while it runs.",
            ]
        )
        listed = self._registry.list()
        if not listed:
            return "\n".join([header, "", trailer])
        catalog = "\n".join(f"- {a.name}: {a.frontmatter.description}" for a in listed)
        return "\n".join([header, "", "Available subagent types:", catalog, "", trailer])

    def validate(self, raw: dict[str, object]) -> dict[str, object]:
        desc = raw.get("description")
        if not isinstance(desc, str) or desc.strip() == "":
            raise ValueError("description must be a non-empty string")
        prompt = raw.get("prompt")
        if not isinstance(prompt, str) or prompt == "":
            raise ValueError("prompt must be a non-empty string")
        st = raw.get("subagent_type")
        if st is not None and not isinstance(st, str):
            raise ValueError("subagent_type must be a string when provided")
        out: dict[str, object] = {"description": desc, "prompt": prompt}
        if isinstance(st, str):
            out["subagent_type"] = st
        return out

    def summarize(self, input: dict[str, object]) -> str:
        return f"Spawn subagent: {input['description']}"

    async def execute(self, input: dict[str, object], ctx: ToolContext) -> ToolResult:
        from ..subagents.default_agent import DEFAULT_AGENT_TYPE
        from ..subagents.runner import RunSubagentArgs, run_subagent

        requested = str(input.get("subagent_type", "")).strip()
        if requested == "" or requested == "general-purpose":
            subagent_type = DEFAULT_AGENT_TYPE
        else:
            subagent_type = requested

        definition = self._registry.get(subagent_type)
        if definition is None:
            available = [a.name for a in self._registry.list()]
            avail_text = (
                f"Available: {', '.join(available)}. "
                "Or omit subagent_type to use the built-in default."
                if available
                else (
                    "No named subagents are loaded; omit subagent_type to use the built-in default."
                )
            )
            return ToolResult(
                content=f"Unknown subagent_type '{subagent_type}'. {avail_text}",
                summary=self.summarize(input),
                is_error=True,
            )

        session = self._get_session(ctx.session_id)
        if session is None:
            return ToolResult(
                content=f"Internal error: parent session '{ctx.session_id}' not registered.",
                summary=self.summarize(input),
                is_error=True,
            )

        display_name: str
        if subagent_type == DEFAULT_AGENT_TYPE:
            display_name = self._next_default_display_name(session.id)
        else:
            display_name = definition.frontmatter.name

        subagent_run_id = f"sa_{uuid4().hex[:8]}"

        result = await run_subagent(
            RunSubagentArgs(
                parent_session=session,
                parent_agent=session.agent,
                definition=definition,
                prompt=str(input["prompt"]),
                display_name=display_name,
                subagent_run_id=subagent_run_id,
                signal=ctx.signal,
                emit=getattr(ctx, "emit", None),
            )
        )

        if result.aborted:
            return ToolResult(
                content=(
                    f"Subagent aborted. Partial output: {result.final_text}"
                    if result.final_text
                    else "Subagent aborted before producing output."
                ),
                summary=self.summarize(input),
                is_error=True,
            )
        if result.errored:
            error_text = (
                f"{result.error['name']}: {result.error['message']}" if result.error else None
            )
            return ToolResult(
                content=" ".join(
                    [
                        f"Subagent encountered an error{f': {error_text}' if error_text else ''}.",
                        *([f"Partial output: {result.final_text}"] if result.final_text else []),
                    ]
                ),
                summary=self.summarize(input),
                is_error=True,
            )
        if result.final_text == "":
            return ToolResult(
                content="Subagent produced no text output.",
                summary=self.summarize(input),
                is_error=True,
            )
        return ToolResult(
            content=result.final_text,
            summary=self.summarize(input),
            is_error=False,
        )
