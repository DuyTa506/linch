from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from ..abort import AbortContext, any_signal, throw_if_aborted
from ..events import (
    AssistantEvent,
    ErrorEvent,
    ResultEvent,
    SubagentEvent,
)
from ..hooks import (
    HookDispatcher,
    HookEvent,
    SubagentStartContext,
    SubagentStopContext,
)
from ..session import RunOptions, Session
from ..types import SystemBlock, TextBlock
from .types import AgentDefinition

if TYPE_CHECKING:
    from .workers import WorkerHandle

SUBAGENT_TOOL_NAME = "Subagent"


@dataclass
class RunSubagentArgs:
    parent_session: Session
    parent_agent: Any
    definition: AgentDefinition
    prompt: str
    display_name: str
    subagent_run_id: str
    tools_filter: list[str] | None = None
    signal: AbortContext | None = None
    emit: Any = None
    retain: bool = False
    """When True, the child session is kept in agent._sessions after completion
    so it can be continued later via SubagentContinue."""
    on_child_registered: Any = None
    """Optional callback ``(child_session_id: str) -> None`` invoked as soon as the
    child session is registered in ``agent._sessions``, before the run is driven.
    Lets the caller record the real child id immediately, so a worker cancelled
    mid-run (``CancelledError`` before this function returns) is still addressable."""


@dataclass
class ContinueSubagentArgs:
    parent_session: Session
    parent_agent: Any
    handle: WorkerHandle
    message: str
    subagent_run_id: str
    signal: AbortContext | None = None
    emit: Any = None


@dataclass
class RunSubagentResult:
    child_session_id: str
    final_text: str
    aborted: bool
    errored: bool
    error: dict[str, str] | None = None


def build_child_tools(parent_tools: Any, filter: list[str] | None) -> Any:
    from ..tools import ToolRegistry

    child = ToolRegistry()
    if filter is None or "*" in filter:
        wanted = None
    else:
        wanted = set(filter)
    for t in parent_tools.list():
        if t.name == SUBAGENT_TOOL_NAME:
            continue
        if wanted is None or t.name in wanted:
            child.register(t)
    return child


def _last_assistant_text(message: Any) -> str:
    parts = []
    for block in message.content:
        if isinstance(block, TextBlock) and not (
            hasattr(block, "type") and block.type == "thinking"
        ):
            parts.append(block.text)
    return "".join(parts)


async def _drive_child(
    child_session: Session,
    prompt: str,
    *,
    emit: Any = None,
    subagent_run_id: str = "",
    subagent_type: str = "",
    display_name: str = "",
    parent_session_id: str = "",
    signal: AbortContext | None = None,
) -> RunSubagentResult:
    """Drive *child_session* with *prompt* and collect the final result.

    Shared by :func:`run_subagent` and :func:`continue_subagent`.
    """
    aborted = False
    errored = False
    last_error: dict[str, str] | None = None
    last_assistant_text = ""

    try:
        child_events = child_session.run(prompt, RunOptions(signal=signal))
        async for event in child_events:
            if emit is not None and callable(emit):
                emit(
                    SubagentEvent(
                        parent_session_id=parent_session_id,
                        subagent_run_id=subagent_run_id,
                        subagent_type=subagent_type,
                        display_name=display_name,
                        event=event,
                    )
                )
            if isinstance(event, AssistantEvent):
                last_assistant_text = _last_assistant_text(event.message)
            elif isinstance(event, ResultEvent) and event.subtype == "aborted":
                aborted = True
            elif isinstance(event, ErrorEvent):
                errored = True
                last_error = {
                    "name": event.error.get("name", "Error"),
                    "message": event.error.get("message", ""),
                }
    except Exception as exc:
        errored = True
        last_error = {
            "name": exc.__class__.__name__,
            "message": str(exc),
        }

    return RunSubagentResult(
        child_session_id=child_session.id,
        final_text=last_assistant_text,
        aborted=aborted,
        errored=errored,
        error=last_error,
    )


async def run_subagent(args: RunSubagentArgs) -> RunSubagentResult:
    agent = args.parent_agent
    store = agent._get_store()

    child_record = await store.create(
        meta={
            "parentSessionId": args.parent_session.id,
            "subagentType": args.definition.frontmatter.name,
            "subagentRunId": args.subagent_run_id,
            "displayName": args.display_name,
        }
    )

    tools_filter = args.tools_filter
    if tools_filter is None:
        tools_filter = args.definition.frontmatter.tools

    effective_tools = args.parent_session.tools_override or agent.tools
    child_tools = build_child_tools(effective_tools, tools_filter)

    # Build the child system blocks from the child's filtered tool names so the
    # protocol block only describes tools the subagent actually has access to.
    child_tool_names = sorted(t.name for t in child_tools.list())
    builder = getattr(agent, "build_system_blocks_for_tool_names", None)
    if callable(builder):
        child_system = list(cast(Iterable[SystemBlock], builder(child_tool_names)))
    else:
        child_system = list(agent.system_blocks)
    child_system.append(
        SystemBlock(
            text=f"User-provided instructions:\n\n{args.definition.body}",
            cacheable=True,
        )
    )

    child_session = Session(
        id=child_record.id,
        created_at=child_record.created_at,
        meta=child_record.meta,
        agent=agent,
        store=store,
        provider_view=[],
    )
    child_session.tools_override = child_tools
    child_session.system_blocks_override = child_system
    # Child joins the parent's spending cap: same RunBudget object, so child
    # turns are visible to the parent's next pre-call budget check.
    child_session.inherited_budget = getattr(args.parent_session, "active_budget", None)

    agent._sessions[child_record.id] = child_session

    if callable(args.on_child_registered):
        args.on_child_registered(child_record.id)

    if args.signal is not None and args.signal.aborted:
        throw_if_aborted(args.signal)

    merged_signal = any_signal(child_session._abort_controller, args.signal)
    hook_dispatcher = HookDispatcher(getattr(agent, "hooks", None))
    result: RunSubagentResult | None = None
    try:
        if hook_dispatcher.active:
            await hook_dispatcher.dispatch(
                HookEvent.SUBAGENT_START,
                SubagentStartContext(
                    session=args.parent_session,
                    run_id=args.parent_session.active_run_id or "unknown",
                    turn_index=None,
                    deps=getattr(args.parent_session, "run_deps", None),
                    child_session_id=child_record.id,
                    subagent_run_id=args.subagent_run_id,
                    subagent_type=args.definition.frontmatter.name,
                    display_name=args.display_name,
                    prompt=args.prompt,
                ),
            )

        result = await _drive_child(
            child_session,
            args.prompt,
            emit=args.emit,
            subagent_run_id=args.subagent_run_id,
            subagent_type=args.definition.frontmatter.name,
            display_name=args.display_name,
            parent_session_id=args.parent_session.id,
            signal=merged_signal,
        )
    finally:
        # Always release the merged-signal watcher task and dispatch
        # SUBAGENT_STOP so cleanup runs on all failure and cancellation paths.
        merged_signal.close()
        if hook_dispatcher.active:
            await hook_dispatcher.dispatch(
                HookEvent.SUBAGENT_STOP,
                SubagentStopContext(
                    session=args.parent_session,
                    run_id=args.parent_session.active_run_id or "unknown",
                    turn_index=None,
                    deps=getattr(args.parent_session, "run_deps", None),
                    child_session_id=child_record.id,
                    subagent_run_id=args.subagent_run_id,
                    subagent_type=args.definition.frontmatter.name,
                    display_name=args.display_name,
                    result=result,
                ),
            )

    if not args.retain:
        agent._sessions.pop(child_record.id, None)

    assert result is not None  # _drive_child succeeded if we reach here
    return result


async def continue_subagent(args: ContinueSubagentArgs) -> RunSubagentResult:
    """Continue a retained worker by driving its existing child session.

    The child session already holds the full ``provider_view`` from prior runs,
    so the model receives the complete conversation history on continuation.

    If the child session is no longer live (process restart), returns an error
    result instead of raising.
    """
    agent = args.parent_agent
    handle = args.handle
    child_session = agent._sessions.get(handle.child_session_id)

    if child_session is None:
        return RunSubagentResult(
            child_session_id=handle.child_session_id,
            final_text="",
            aborted=False,
            errored=True,
            error={
                "name": "WorkerNotLive",
                "message": (
                    f"Worker '{handle.worker_id}' is no longer live after a process restart."
                    " Spawn a fresh subagent to continue this work."
                ),
            },
        )

    if args.signal is not None and args.signal.aborted:
        throw_if_aborted(args.signal)

    # Re-establish the abort link for this continuation.
    merged_signal = any_signal(child_session._abort_controller, args.signal)

    subagent_run_id = f"sa_cont_{args.handle.worker_id}"
    hook_dispatcher = HookDispatcher(getattr(agent, "hooks", None))
    result: RunSubagentResult | None = None
    try:
        if hook_dispatcher.active:
            await hook_dispatcher.dispatch(
                HookEvent.SUBAGENT_START,
                SubagentStartContext(
                    session=args.parent_session,
                    run_id=args.parent_session.active_run_id or "unknown",
                    turn_index=None,
                    deps=getattr(args.parent_session, "run_deps", None),
                    child_session_id=child_session.id,
                    subagent_run_id=subagent_run_id,
                    subagent_type=handle.definition.frontmatter.name,
                    display_name=handle.display_name,
                    prompt=args.message,
                ),
            )
        result = await _drive_child(
            child_session,
            args.message,
            emit=args.emit,
            subagent_run_id=subagent_run_id,
            subagent_type=handle.definition.frontmatter.name,
            display_name=handle.display_name,
            parent_session_id=args.parent_session.id,
            signal=merged_signal,
        )
    finally:
        # Always release the merged-signal watcher task and dispatch
        # SUBAGENT_STOP so cleanup runs on all failure and cancellation paths.
        merged_signal.close()
        if hook_dispatcher.active:
            await hook_dispatcher.dispatch(
                HookEvent.SUBAGENT_STOP,
                SubagentStopContext(
                    session=args.parent_session,
                    run_id=args.parent_session.active_run_id or "unknown",
                    turn_index=None,
                    deps=getattr(args.parent_session, "run_deps", None),
                    child_session_id=child_session.id,
                    subagent_run_id=subagent_run_id,
                    subagent_type=handle.definition.frontmatter.name,
                    display_name=handle.display_name,
                    result=result,
                ),
            )

    assert result is not None  # _drive_child succeeded if we reach here
    handle.last_result_text = result.final_text
    handle.status = "failed" if result.errored else "completed"
    return result
