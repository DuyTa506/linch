from __future__ import annotations

from typing import Any
from uuid import uuid4

from .base import ToolContext, ToolResult, ToolScope

SUBAGENT_CONTINUE_TOOL_NAME = "SubagentContinue"

SUBAGENT_CONTINUE_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "to": {
            "type": "string",
            "description": (
                "Worker id (e.g. 'agent-a1b2') or display name of the retained worker"
                " to continue. Use the id returned in the original Subagent result."
            ),
        },
        "message": {
            "type": "string",
            "description": "The follow-up message or task to send to the worker.",
        },
    },
    "required": ["to", "message"],
}


class SubagentContinueTool:
    name = SUBAGENT_CONTINUE_TOOL_NAME
    input_schema = SUBAGENT_CONTINUE_TOOL_SCHEMA
    scope: ToolScope = "exec"
    parallel_safe = True

    def __init__(self, get_session: Any) -> None:
        self._get_session = get_session

    @property
    def description(self) -> str:
        return "\n".join(
            [
                "Continue an existing worker subagent by id, reusing its full context.",
                "",
                "Use SubagentContinue instead of spawning a new Subagent when:",
                "- The worker has already gathered useful context you want to build on.",
                "- You want the worker to refine or extend its prior output.",
                "- You need a follow-up action in the same focused scope.",
                "",
                "Provide the worker_id from the original Subagent result, or the display name.",
                "The worker's full conversation history is preserved across continuations.",
            ]
        )

    def validate(self, raw: dict[str, object]) -> dict[str, object]:
        to = raw.get("to")
        if not isinstance(to, str) or to.strip() == "":
            raise ValueError("'to' must be a non-empty string worker id or display name")
        message = raw.get("message")
        if not isinstance(message, str) or message == "":
            raise ValueError("'message' must be a non-empty string")
        return {"to": str(to).strip(), "message": str(message)}

    def summarize(self, input: dict[str, object]) -> str:
        return f"Continue worker: {input.get('to', '?')}"

    async def execute(self, input: dict[str, object], ctx: ToolContext) -> ToolResult:
        from ..subagents.runner import ContinueSubagentArgs, continue_subagent

        target = str(input["to"]).strip()
        message = str(input["message"])

        session = self._get_session(ctx.session_id)
        if session is None:
            return ToolResult(
                content=f"Internal error: parent session '{ctx.session_id}' not registered.",
                summary=self.summarize(input),
                is_error=True,
            )

        from ._worker_utils import resolve_worker

        handle = resolve_worker(session, target)
        if handle is None:
            known = list(session.workers.keys())
            if known:
                known_str = ", ".join(known)
                msg = (
                    f"No live worker '{target}'. Known worker ids: {known_str}."
                    " Spawn a fresh Subagent if this worker is no longer needed."
                )
            else:
                msg = (
                    f"No live worker '{target}'. No workers have been spawned yet."
                    " Use the Subagent tool to create one."
                )
            return ToolResult(
                content=msg,
                summary=self.summarize(input),
                is_error=True,
            )

        subagent_run_id = f"sa_cont_{uuid4().hex[:8]}"
        emit = getattr(session, "pending_child_events", None)
        emit_fn = emit.append if emit is not None else getattr(ctx, "emit", None)

        result = await continue_subagent(
            ContinueSubagentArgs(
                parent_session=session,
                parent_agent=session.agent,
                handle=handle,
                message=message,
                subagent_run_id=subagent_run_id,
                signal=ctx.signal,
                emit=emit_fn,
            )
        )

        if result.aborted:
            return ToolResult(
                content=(
                    f"Worker aborted. Partial output: {result.final_text}"
                    if result.final_text
                    else "Worker aborted before producing output."
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
                        f"Worker encountered an error{f': {error_text}' if error_text else ''}.",
                        *([f"Partial output: {result.final_text}"] if result.final_text else []),
                    ]
                ),
                summary=self.summarize(input),
                is_error=True,
            )
        if result.final_text == "":
            return ToolResult(
                content="Worker produced no text output.",
                summary=self.summarize(input),
                is_error=True,
            )
        return ToolResult(
            content=result.final_text,
            summary=self.summarize(input),
            is_error=False,
        )
