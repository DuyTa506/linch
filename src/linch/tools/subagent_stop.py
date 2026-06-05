from __future__ import annotations

from .base import ToolContext, ToolResult

TASK_STOP_TOOL_NAME = "TaskStop"

TASK_STOP_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "task_id": {
            "type": "string",
            "description": (
                "The worker id (e.g. 'agent-a1b2') or display name to stop."
                " Stopped workers remain addressable via SubagentContinue."
            ),
        },
        "reason": {
            "type": "string",
            "description": "Optional reason for stopping the worker (logged to the handle).",
        },
    },
    "required": ["task_id"],
}


class TaskStopTool:
    name = TASK_STOP_TOOL_NAME
    input_schema = TASK_STOP_TOOL_SCHEMA
    scope = "exec"
    parallel_safe = True

    def __init__(self, get_session: object) -> None:
        self._get_session = get_session

    @property
    def description(self) -> str:
        return "\n".join(
            [
                "Stop a running worker subagent by id.",
                "",
                "The worker is cancelled immediately. Its handle remains in the worker",
                "registry so it can be continued via SubagentContinue.",
                "Use this when a worker has gone off-track or is no longer needed.",
            ]
        )

    def validate(self, raw: dict[str, object]) -> dict[str, object]:
        task_id = raw.get("task_id")
        if not isinstance(task_id, str) or task_id.strip() == "":
            raise ValueError("'task_id' must be a non-empty string")
        out: dict[str, object] = {"task_id": str(task_id).strip()}
        reason = raw.get("reason")
        if isinstance(reason, str) and reason:
            out["reason"] = reason
        return out

    def summarize(self, input: dict[str, object]) -> str:
        return f"Stop worker: {input.get('task_id', '?')}"

    async def execute(self, input: dict[str, object], ctx: ToolContext) -> ToolResult:
        import asyncio

        target = str(input["task_id"]).strip()

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
            msg = f"No worker '{target}' found." + (
                f" Known workers: {', '.join(known)}." if known else " No workers spawned yet."
            )
            return ToolResult(content=msg, summary=self.summarize(input), is_error=True)

        # Cancel the asyncio task if it's running
        task = getattr(handle, "task", None)
        if task is not None and isinstance(task, asyncio.Task) and not task.done():
            task.cancel()

        # Also abort the child session if it's still live
        from ..session import Session

        child_session = session.agent._sessions.get(handle.child_session_id)
        if child_session is not None and isinstance(child_session, Session):
            child_session.abort()

        handle.status = "killed"
        reason = str(input.get("reason", ""))
        msg = f"Worker '{handle.worker_id}' ({handle.display_name}) stopped."
        if reason:
            msg += f" Reason: {reason}"
        msg += " Use SubagentContinue to re-engage if needed."
        return ToolResult(content=msg, summary=self.summarize(input), is_error=False)
