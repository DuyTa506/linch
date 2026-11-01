from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from contextlib import aclosing
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, cast

from .. import lifecycle
from ..abort import AbortContext, any_signal, throw_if_aborted
from ..errors import ConfigError
from ..events import (
    AssistantEvent,
    ErrorEvent,
    ResultEvent,
    SubagentEvent,
)
from ..hooks import (
    HookDispatcher,
    SubagentStartContext,
    SubagentStopContext,
)
from ..profiles import _base_run_config
from ..session import RunOptions, Session
from ..session_log import SessionLog
from ..types import SystemBlock, TextBlock
from .isolation import (
    _bind_isolated_tools,
    _continued_isolated_world,
    _isolated_dependencies,
    _isolated_world,
)
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
    mailbox_address: str | None = None
    """Unique peer address assigned to the child when mailbox delivery is enabled."""
    config_binding: Any = None
    """Originating configuration binding captured before scheduling child work."""
    parent_run_id: str | None = None
    """Parent run identity used by lifecycle hooks after detachment."""
    detached: bool = False
    """Whether a retained background worker remains addressable after cancellation."""
    tools_filter: list[str] | None = None
    run_options: RunOptions | None = None
    """Per-child run options. The runner always overrides ``signal`` with the
    merged parent/child abort signal before calling ``Session.run``."""
    _deps_captured: bool = False
    """Whether options capture the invoking tool's deps, including None."""
    signal: AbortContext | None = None
    emit: Any = None
    retain: bool = False
    """When True, the child session is kept in agent._sessions after completion
    so it can be continued later via SubagentContinue."""
    fork: bool = False
    """When True, the child continues from the parent's context instead of a
    fresh one: it inherits the parent's ``provider_view`` (conversation prefix),
    system blocks, tools, and ``file_read_tracker``. This makes the child's
    request prefix byte-identical to the parent's, so a caching provider reuses
    the cached prefix rather than re-paying for it. Trades isolation for cost."""
    on_child_registered: Any = None
    """Optional callback ``(child_session_id: str) -> None`` invoked as soon as the
    child session is registered in ``agent._sessions``, before the run is driven.
    Lets the caller record the real child id immediately, so a worker cancelled
    mid-run (``CancelledError`` before this function returns) is still addressable."""
    isolation: Any = None
    """Optional :class:`~linch.tools.isolation.IsolationBackend`. When set, the
    child runs in its own acquired working directory (``ToolContext.cwd``), so
    parallel branches editing the same relative path don't collide. The scratch
    dir is released when the child finishes (see ``isolation_keep``)."""
    isolation_keep: bool = False
    """When True, the acquired isolation cwd is preserved after the child finishes
    (e.g. so the embedder can merge its file artifacts); otherwise it is removed."""


@dataclass
class ContinueSubagentArgs:
    parent_session: Session
    parent_agent: Any
    handle: WorkerHandle
    message: str
    subagent_run_id: str
    parent_run_id: str | None = None
    """Parent invocation identity used by continuation lifecycle hooks."""
    run_options: RunOptions | None = None
    """Per-continuation run options. The runner always overrides ``signal`` with
    the merged parent/child abort signal before calling ``Session.run``."""
    _deps_captured: bool = False
    """Whether options capture the invoking tool's deps, including None."""
    signal: AbortContext | None = None
    emit: Any = None


@dataclass
class RunSubagentResult:
    child_session_id: str
    final_text: str
    aborted: bool
    errored: bool
    structured_output: dict[str, Any] | None = None
    structured_error: str | None = None
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


def _child_run_options(parent_session: Session, options: RunOptions | None) -> RunOptions | None:
    """Fill direct runner calls with the parent's current dependency object."""
    if options is not None and options.deps is not None:
        return options
    deps = getattr(parent_session, "run_deps", None)
    return replace(options, deps=deps) if options is not None else RunOptions(deps=deps)


def _effective_child_deps(parent_session: Session, options: RunOptions | None) -> Any:
    """Resolve the dependency object the child run will actually receive."""
    if options is not None:
        if options.deps is not None:
            return options.deps
        return getattr(getattr(parent_session, "agent", None), "deps", None)
    deps = getattr(parent_session, "run_deps", None)
    return (
        deps if deps is not None else getattr(getattr(parent_session, "agent", None), "deps", None)
    )


def _last_assistant_text(message: Any) -> str:
    parts = []
    for block in message.content:
        if isinstance(block, TextBlock) and not (
            hasattr(block, "type") and block.type == "thinking"
        ):
            parts.append(block.text)
    return "".join(parts)


def result_text_for_caller(result: RunSubagentResult) -> str:
    """Return the text contract exposed by legacy subagent callers."""
    if result.structured_output is not None:
        return json.dumps(result.structured_output, sort_keys=True, separators=(",", ":"))
    if result.final_text:
        return result.final_text
    return result.final_text


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
    run_options: RunOptions | None = None,
) -> RunSubagentResult:
    """Drive *child_session* with *prompt* and collect the final result.

    Shared by :func:`run_subagent` and :func:`continue_subagent`.
    """
    aborted = False
    errored = False
    last_error: dict[str, str] | None = None
    last_assistant_text = ""
    structured_output: dict[str, Any] | None = None
    structured_error: str | None = None

    try:
        child_opts = replace(run_options or RunOptions(), signal=signal)
        child_events = child_session.run(prompt, child_opts)
        # Session.run returns a native async generator, typed as AsyncIterator.
        async with aclosing(cast(Any, child_events)):
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
                    if event.final_text is not None:
                        last_assistant_text = event.final_text
                    structured_output = event.structured_output
                    structured_error = event.structured_error
                elif isinstance(event, ResultEvent):
                    if event.final_text is not None:
                        last_assistant_text = event.final_text
                    structured_output = event.structured_output
                    structured_error = event.structured_error
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
        structured_output=structured_output,
        structured_error=structured_error,
        error=last_error,
    )


async def run_subagent(args: RunSubagentArgs) -> RunSubagentResult:
    """Drive a child with real Agent ownership and captured parent configuration.

    Args:
        args: Child launch settings, including the originating binding and retain policy.

    Returns:
        The child's answer, completion status, and durable session identity.
    """
    if not args._deps_captured:
        args = replace(
            args,
            run_options=_child_run_options(args.parent_session, args.run_options),
            _deps_captured=True,
        )
    binding = args.config_binding
    if binding is None and args.parent_session.active_run_id is not None:
        binding = args.parent_session._run_bindings.get(args.parent_session.active_run_id)
    if binding is not None:
        binding.retain()
    child: Session | None = None

    def on_registered(session: Session) -> None:
        nonlocal child
        child = session
        session._config_seed = binding

    try:
        return await _run_subagent_owned(args, binding, on_registered)
    except BaseException:
        if child is not None:
            if not (args.detached and args.retain):
                try:
                    await args.parent_agent.release_session(child, force=True)
                except BaseException as cleanup_error:
                    # Preserve the child failure; cleanup is retried by agent
                    # teardown and must not replace the primary exception.
                    logging.getLogger(__name__).warning(
                        "subagent cleanup failed for child %s", child.id, exc_info=cleanup_error
                    )
        elif binding is not None:
            try:
                await binding.release()
            except BaseException as cleanup_error:
                # Preserve the child failure; binding ownership is best-effort
                # here and must not mask the original exception.
                logging.getLogger(__name__).warning(
                    "subagent configuration cleanup failed", exc_info=cleanup_error
                )
        raise


async def _run_subagent_owned(
    args: RunSubagentArgs, binding: Any, on_registered: Any
) -> RunSubagentResult:
    # Resolve the child's invocation options before any await. Detached work can
    # outlive the parent turn that launched it, so lifecycle hooks must retain
    # the same dependency object as the child run rather than rereading the
    # parent's mutable ``run_deps`` at stop time.
    child_run_options = args.run_options
    hook_deps = _effective_child_deps(args.parent_session, child_run_options)
    agent = args.parent_agent
    store = agent._get_store()
    config = binding.config if binding is not None else _base_run_config(args.parent_session)

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

    if config._values is not None:
        effective_tools = config.tools
    else:
        effective_tools = args.parent_session.tools_override
        if effective_tools is None:
            worker_catalog = getattr(agent, "_subagent_tool_registry", None)
            effective_tools = worker_catalog if worker_catalog is not None else config.tools

    if args.fork:
        # Fork: reuse the parent's tools and system blocks verbatim (and seed the
        # conversation prefix below) so the request prefix is byte-identical and
        # the provider's prompt cache hits. A filter still narrows tools when given.
        child_tools = (
            effective_tools
            if tools_filter is None
            else build_child_tools(effective_tools, tools_filter)
        )
        effective_system = config.system_blocks_override
        child_system = list(config.system_blocks if effective_system is None else effective_system)
        seed_view = list(args.parent_session.provider_view)
    else:
        child_tools = build_child_tools(effective_tools, tools_filter)
        # Build the child system blocks from the child's filtered tool names so the
        # protocol block only describes tools the subagent actually has access to.
        child_tool_names = sorted(t.name for t in child_tools.list())
        builder = getattr(config, "build_system_blocks_for_tool_names", None)
        if callable(builder):
            child_system = list(cast(Iterable[SystemBlock], builder(child_tool_names)))
        else:
            child_system = list(config.system_blocks)
        child_system.append(
            SystemBlock(
                text=f"User-provided instructions:\n\n{args.definition.body}",
                cacheable=True,
            )
        )
        seed_view = []

    child_session = Session(
        id=child_record.id,
        created_at=child_record.created_at,
        meta=child_record.meta,
        agent=agent,
        store=store,
        session_log=SessionLog.seed(historical=[], visible=seed_view),
    )
    # A unified execution world is agent-scoped. Without isolation child
    # sessions share the parent's filesystem; an isolated child gets a fresh
    # disk backend rooted at its acquired cwd below.
    child_session.filesystem = (
        None if args.isolation is not None else getattr(args.parent_session, "filesystem", None)
    )
    if args.fork:
        # Clone the parent's read-file tracker so the child skips re-reading files
        # the parent already loaded.
        for path in args.parent_session.file_read_tracker.files():
            child_session.file_read_tracker.add(path)
    child_session.tools_override = child_tools
    child_session.system_blocks_override = child_system
    # Give the worker a unique peer-mailbox address; no-op when the agent has
    # no mailbox configured. Direct runner callers default to the child session
    # id; tool callers can provide a stable worker id that they expose to the model.
    if config.context.get("mailbox") is not None:
        child_session.mailbox_address = args.mailbox_address or child_record.id
    # Child joins the parent's spending cap: same RunBudget object, so child
    # turns are visible to the parent's next pre-call budget check.
    child_session.inherited_budget = (
        binding.budget
        if binding is not None
        else getattr(args.parent_session, "active_budget", None)
    )

    agent._sessions[child_record.id] = child_session
    on_registered(child_session)

    if callable(args.on_child_registered):
        args.on_child_registered(child_record.id)

    if args.signal is not None and args.signal.aborted:
        throw_if_aborted(args.signal)

    merged_signal = any_signal(child_session._abort_controller, args.signal)
    hook_dispatcher = HookDispatcher(config.hooks)
    result: RunSubagentResult | None = None
    primary_error: BaseException | None = None
    cleanup_errors: list[BaseException] = []
    try:
        # Acquire an isolated working directory (if configured) inside the try so
        # the finally always releases it, even on a mid-run failure path.
        if args.isolation is not None:
            isolated_cwd = await args.isolation.acquire()
            child_session.cwd_override = isolated_cwd
            child_execution, child_filesystem = _isolated_world(
                args.parent_session,
                agent,
                isolated_cwd,
                deps=hook_deps,
            )
            child_session.filesystem = child_filesystem
            child_session.tools_override = _bind_isolated_tools(
                child_tools,
                execution=child_execution,
                filesystem=child_filesystem,
            )
            isolated_deps = _isolated_dependencies(hook_deps, child_filesystem)
            if child_run_options is None:
                if isolated_deps is not None:
                    child_run_options = RunOptions(deps=isolated_deps)
            else:
                child_run_options = replace(
                    child_run_options,
                    deps=isolated_deps,
                )
            hook_deps = isolated_deps
        if hook_dispatcher.active:
            await hook_dispatcher.dispatch(
                lifecycle.SUBAGENT_START,
                SubagentStartContext(
                    session=args.parent_session,
                    run_id=args.parent_run_id
                    or (binding.run_id if binding is not None else "unknown"),
                    turn_index=None,
                    deps=hook_deps,
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
            run_options=child_run_options,
        )
    except BaseException as exc:
        primary_error = exc
    finally:
        # Release the isolation cwd (unless kept) before other cleanup.
        if args.isolation is not None and child_session.cwd_override:
            try:
                await args.isolation.release(child_session.cwd_override, keep=args.isolation_keep)
            except BaseException as exc:
                cleanup_errors.append(exc)
        # Always release the merged-signal watcher task and dispatch
        # SUBAGENT_STOP so cleanup runs on all failure and cancellation paths.
        try:
            merged_signal.close()
        except BaseException as exc:
            cleanup_errors.append(exc)
        if hook_dispatcher.active:
            try:
                await hook_dispatcher.dispatch(
                    lifecycle.SUBAGENT_STOP,
                    SubagentStopContext(
                        session=args.parent_session,
                        run_id=args.parent_run_id
                        or (binding.run_id if binding is not None else "unknown"),
                        turn_index=None,
                        deps=hook_deps,
                        child_session_id=child_record.id,
                        subagent_run_id=args.subagent_run_id,
                        subagent_type=args.definition.frontmatter.name,
                        display_name=args.display_name,
                        result=result,
                    ),
                )
            except BaseException as exc:
                cleanup_errors.append(exc)
        if not args.retain:
            try:
                await agent.release_session(child_record.id, force=True)
            except BaseException as exc:
                cleanup_errors.append(exc)

    for cleanup_error in cleanup_errors:
        logging.getLogger(__name__).warning(
            "subagent cleanup failed for child %s", child_record.id, exc_info=cleanup_error
        )
    if primary_error is not None:
        raise primary_error

    assert result is not None  # _drive_child succeeded if we reach here
    if cleanup_errors and not result.errored and not result.aborted:
        raise cleanup_errors[0]
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

    # Capture continuation dependencies before any asynchronous hook or child
    # work can observe a later parent run's dependency object.
    child_run_options = (
        args.run_options
        if args._deps_captured
        else _child_run_options(args.parent_session, args.run_options)
    )
    hook_deps = _effective_child_deps(args.parent_session, child_run_options)

    if child_session.cwd_override is not None:
        child_execution, child_filesystem = _continued_isolated_world(child_session, agent)
        child_tools = child_session.tools_override
        if child_tools is None:
            raise ConfigError("isolated subagent continuation is missing its child tool registry")
        child_session.tools_override = _bind_isolated_tools(
            child_tools,
            execution=child_execution,
            filesystem=child_filesystem,
        )
        isolated_deps = _isolated_dependencies(hook_deps, child_filesystem)
        if child_run_options is None:
            if isolated_deps is not None:
                child_run_options = RunOptions(deps=isolated_deps)
        else:
            child_run_options = replace(child_run_options, deps=isolated_deps)
        hook_deps = isolated_deps

    # Re-establish the abort link for this continuation.
    merged_signal = any_signal(child_session._abort_controller, args.signal)

    subagent_run_id = f"sa_cont_{args.handle.worker_id}"
    config = (
        child_session._config_seed.config
        if child_session._config_seed is not None
        else agent._run_config
    )
    # A retained worker keeps its original configuration seed, but its budget
    # belongs to the parent invocation that is driving this continuation.
    child_session.inherited_budget = getattr(args.parent_session, "active_budget", None)
    hook_dispatcher = HookDispatcher(config.hooks)
    result: RunSubagentResult | None = None
    try:
        if hook_dispatcher.active:
            await hook_dispatcher.dispatch(
                lifecycle.SUBAGENT_START,
                SubagentStartContext(
                    session=args.parent_session,
                    run_id=args.parent_run_id or "unknown",
                    turn_index=None,
                    deps=hook_deps,
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
            run_options=child_run_options,
        )
    finally:
        # Always release the merged-signal watcher task and dispatch
        # SUBAGENT_STOP so cleanup runs on all failure and cancellation paths.
        merged_signal.close()
        if hook_dispatcher.active:
            await hook_dispatcher.dispatch(
                lifecycle.SUBAGENT_STOP,
                SubagentStopContext(
                    session=args.parent_session,
                    run_id=args.parent_run_id or "unknown",
                    turn_index=None,
                    deps=hook_deps,
                    child_session_id=child_session.id,
                    subagent_run_id=subagent_run_id,
                    subagent_type=handle.definition.frontmatter.name,
                    display_name=handle.display_name,
                    result=result,
                ),
            )

    assert result is not None  # _drive_child succeeded if we reach here
    handle.last_result_text = result_text_for_caller(result)
    handle.status = "failed" if result.errored else "completed"
    return result
