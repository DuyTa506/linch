from __future__ import annotations

from dataclasses import dataclass

from ..abort import AbortContext, any_signal, throw_if_aborted
from ..events import (
    AssistantEvent,
    ErrorEvent,
    ResultEvent,
    SubagentEvent,
)
from ..session import Session
from ..types import SystemBlock, TextBlock
from .types import AgentDefinition

SUBAGENT_TOOL_NAME = "Subagent"


@dataclass
class RunSubagentArgs:
    parent_session: Session
    parent_agent: object
    definition: AgentDefinition
    prompt: str
    display_name: str
    subagent_run_id: str
    tools_filter: list[str] | None = None
    signal: AbortContext | None = None
    emit: object = None


@dataclass
class RunSubagentResult:
    child_session_id: str
    final_text: str
    aborted: bool
    errored: bool
    error: dict[str, str] | None = None


def build_child_tools(parent_tools: object, filter: list[str] | None) -> object:
    from ..tools import ToolRegistry

    child = ToolRegistry()
    wildcard = filter is None or "*" in filter
    wanted = None if wildcard else set(filter)
    for t in parent_tools.list():
        if t.name == SUBAGENT_TOOL_NAME:
            continue
        if wanted is None or t.name in wanted:
            child.register(t)
    return child


def _last_assistant_text(message: object) -> str:
    parts = []
    for block in message.content:
        if isinstance(block, TextBlock) and not (
            hasattr(block, "type") and block.type == "thinking"
        ):
            parts.append(block.text)
    return "".join(parts)


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
        child_system = list(builder(child_tool_names))
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

    agent._sessions[child_record.id] = child_session

    if args.signal is not None and args.signal.aborted:
        throw_if_aborted(args.signal)

    _ = any_signal(child_session._abort_controller, args.signal)

    aborted = False
    errored = False
    last_error: dict[str, str] | None = None
    last_assistant_text = ""

    try:
        child_events = child_session.run(args.prompt)
        async for event in child_events:
            if args.emit is not None and callable(args.emit):
                args.emit(
                    SubagentEvent(
                        parent_session_id=args.parent_session.id,
                        subagent_run_id=args.subagent_run_id,
                        subagent_type=args.definition.frontmatter.name,
                        display_name=args.display_name,
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
    finally:
        agent._sessions.pop(child_record.id, None)

    return RunSubagentResult(
        child_session_id=child_record.id,
        final_text=last_assistant_text,
        aborted=aborted,
        errored=errored,
        error=last_error,
    )
