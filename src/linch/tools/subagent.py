from __future__ import annotations

from html import escape
from typing import Any
from uuid import uuid4

from .base import ToolContext, ToolResult, ToolScope

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
        "run_in_background": {
            "type": "boolean",
            "description": (
                "When true, the subagent runs in the background. The tool returns"
                " immediately with an acknowledgement. A <task-notification> message"
                " is injected into your conversation when the worker finishes."
            ),
        },
    },
    "required": ["description", "prompt"],
}


class SubagentTool:
    name = SUBAGENT_TOOL_NAME
    input_schema = SUBAGENT_TOOL_SCHEMA
    scope: ToolScope = "exec"
    parallel = True

    def __init__(
        self,
        registry: Any,
        get_session: Any,
        next_default_display_name: Any,
        *,
        retain_subagents: bool = False,
        enable_background_subagents: bool = False,
    ) -> None:
        self._registry = registry
        self._get_session = get_session
        self._next_default_display_name = next_default_display_name
        self._retain_subagents = retain_subagents
        self._enable_background_subagents = enable_background_subagents

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
                '- Do not delegate synthesis with vague prompts like "based on your findings";',
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
        bg = raw.get("run_in_background")
        if bg is not None:
            out["run_in_background"] = bool(bg)
        return out

    def summarize(self, input: dict[str, object]) -> str:
        return f"Spawn subagent: {input['description']}"

    async def execute(self, input: dict[str, object], ctx: ToolContext) -> ToolResult:
        import asyncio

        from ..subagents.default_agent import DEFAULT_AGENT_TYPE
        from ..subagents.runner import RunSubagentArgs, result_text_for_caller, run_subagent
        from ..subagents.workers import WorkerHandle

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
        worker_id = f"agent-{uuid4().hex[:4]}"
        run_in_background = bool(input.get("run_in_background", False))
        if run_in_background and not self._enable_background_subagents:
            return ToolResult(
                content=(
                    "Background subagents are not enabled for this agent. "
                    "Use create_deep_agent() for in-process background workers, "
                    "or run this Subagent without run_in_background."
                ),
                summary=self.summarize(input),
                is_error=True,
            )

        # Use the session emit channel so SubagentEvents reach pending_child_events.
        emit_list = getattr(session, "pending_child_events", None)
        emit_fn = emit_list.append if emit_list is not None else getattr(ctx, "emit", None)

        runner_args = RunSubagentArgs(
            parent_session=session,
            parent_agent=session.agent,
            definition=definition,
            prompt=str(input["prompt"]),
            display_name=display_name,
            subagent_run_id=subagent_run_id,
            signal=ctx.signal,
            emit=emit_fn,
            retain=self._retain_subagents,
        )

        if run_in_background:
            runner_args.retain = True
            # Spawn detached; return immediately with an ack.
            handle = WorkerHandle(
                worker_id=worker_id,
                child_session_id="",  # populated when task completes
                display_name=display_name,
                definition=definition,
                status="running",
            )
            session.workers[worker_id] = handle

            # Record the real child session id as soon as it is registered, so a
            # worker stopped mid-run (CancelledError before run_subagent returns)
            # stays addressable by TaskStop / SubagentContinue and is not leaked.
            def _record_child_id(child_session_id: str) -> None:
                handle.child_session_id = child_session_id

            runner_args.on_child_registered = _record_child_id

            async def _bg_run() -> None:
                try:
                    result = await run_subagent(runner_args)
                except asyncio.CancelledError:
                    if handle.status != "killed":
                        handle.status = "killed"
                    raise
                result_text = result_text_for_caller(result)
                handle.child_session_id = result.child_session_id
                handle.last_result_text = result_text
                handle.status = "failed" if result.errored or result.aborted else "completed"
                # Append a <task-notification> message for the next turn to drain.
                # Use the session captured at spawn time, not a fresh _get_session lookup,
                # to avoid writing into a different session if the id was re-registered.
                if not hasattr(session, "pending_notifications"):
                    return
                status_str = (
                    "aborted" if result.aborted else ("failed" if result.errored else "completed")
                )
                error_line = ""
                if result.errored and result.error:
                    name = escape(result.error["name"])
                    msg = escape(result.error["message"])
                    error_line = f"<error>{name}: {msg}</error>"
                from ..events import BackgroundWorkerEvent
                from ..types import Message, TextBlock

                notification_text = (
                    f"<task-notification>"
                    f"<task-id>{escape(worker_id)}</task-id>"
                    f"<status>{escape(status_str)}</status>"
                    f"<summary>Worker '{escape(display_name)}' finished.</summary>"
                    f"<result>{escape(result_text)}</result>"
                    f"{error_line}"
                    f"</task-notification>"
                )
                session.pending_notifications.append(
                    Message(role="user", content=[TextBlock(text=notification_text)])
                )
                # Emit a BackgroundWorkerEvent to the session's child-event log too.
                if emit_list is not None:
                    emit_list.append(
                        BackgroundWorkerEvent(
                            worker_id=worker_id,
                            status=status_str,
                            display_name=display_name,
                        )
                    )

            handle.task = asyncio.create_task(_bg_run())
            return ToolResult(
                content=(
                    f"Worker '{worker_id}' started in background."
                    f" You will receive a <task-notification> when it finishes."
                ),
                summary=self.summarize(input),
                is_error=False,
            )

        # Foreground (blocking) execution
        result = await run_subagent(runner_args)
        result_text = result_text_for_caller(result)

        if self._retain_subagents:
            # Store worker handle so SubagentContinue can address it by id.
            handle = WorkerHandle(
                worker_id=worker_id,
                child_session_id=result.child_session_id,
                display_name=display_name,
                definition=definition,
                status="failed" if result.errored else "completed",
                last_result_text=result_text,
            )
            session.workers[worker_id] = handle

        if result.aborted:
            return ToolResult(
                content=(
                    f"Subagent aborted. Partial output: {result_text}"
                    if result_text
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
                        *([f"Partial output: {result_text}"] if result_text else []),
                    ]
                ),
                summary=self.summarize(input),
                is_error=True,
            )
        if result_text == "":
            return ToolResult(
                content="Subagent produced no text output.",
                summary=self.summarize(input),
                is_error=True,
            )
        # Include worker_id in retained results so the model can address this worker later.
        suffix = f"\n\n[Worker ID: {worker_id}]" if self._retain_subagents else ""
        return ToolResult(
            content=result_text + suffix,
            summary=self.summarize(input),
            is_error=False,
        )
