"""WorkflowContext — the ``wf`` object handed to workflow functions."""

from __future__ import annotations

import asyncio
import dataclasses
import json
from collections.abc import Awaitable, Callable, Sequence
from typing import Any
from uuid import uuid4

from ..errors import ConfigError, WorkflowError
from ..events import Event, WorkflowEvent
from ..session import RunOptions
from .journal import WorkflowJournal, call_key


class WorkflowContext:
    """Deterministic orchestration primitives for a workflow function.

    The workflow function must be deterministic (no random or time-based
    branching) for resume to replay the unchanged ``wf.agent`` prefix.
    """

    def __init__(
        self,
        agent: Any,
        host_session: Any,
        *,
        journal: WorkflowJournal | None = None,
        budget: Any = None,
        max_concurrency: int = 4,
        on_event: Callable[[Event], None] | None = None,
        store: Any = None,
        run_id: str | None = None,
    ) -> None:
        self._agent = agent
        self._host_session = host_session
        self._journal = journal or WorkflowJournal()
        self.budget = budget
        self._semaphore = asyncio.Semaphore(max(1, max_concurrency))
        self._on_event = on_event
        self._store = store
        self._run_id = run_id

    # ── event plumbing ───────────────────────────────────────────────────

    def _emit_sync(self, event: Event) -> None:
        """Forward an event to the host callback (child SubagentEvents land here)."""
        if self._on_event is not None:
            self._on_event(event)

    async def _emit(self, event: Event) -> None:
        """Forward to the host callback and persist when the run is durable."""
        self._emit_sync(event)
        if self._store is not None and self._run_id is not None:
            await self._store.append_event(self._run_id, event)

    # ── primitives ───────────────────────────────────────────────────────

    async def phase(self, title: str) -> None:
        """Mark a named phase (progress grouping for observers)."""
        await self._emit(WorkflowEvent(kind="phase", title=title))

    async def agent(
        self,
        prompt: str,
        *,
        name: str | None = None,
        label: str | None = None,
        tools: list[str] | None = None,
        run_options: RunOptions | None = None,
        output_schema: Any = None,
        final_tool_name: str | None = None,
        fork: bool = False,
        isolation: Any = None,
        isolation_keep: bool = False,
    ) -> str:
        """Run a subagent and return its final text.

        ``name`` selects a subagent definition from the agent's registry
        (default: the built-in general-purpose subagent).  Results are
        journaled; on resume an unchanged call returns its cached result
        without a provider call.

        ``fork=True`` runs the subagent as a *continuation* of the workflow
        host's context (shared conversation prefix, system blocks, tools, and
        read-file tracker) so a caching provider reuses the cached prefix — a
        cost win for fans over a large shared context. Default ``False`` keeps
        each subagent isolated.

        ``isolation`` (an :class:`~linch.tools.isolation.IsolationBackend`) runs
        the subagent in its own acquired working directory, so parallel branches
        editing the same relative path don't collide; ``isolation_keep=True``
        preserves that directory after the branch finishes (e.g. to merge it).
        """
        from ..subagents.default_agent import DEFAULT_AGENT
        from ..subagents.runner import RunSubagentArgs, result_text_for_caller, run_subagent

        definition = None
        if name is None:
            definition = DEFAULT_AGENT
        else:
            registry = getattr(self._agent, "subagent_registry", None)
            if registry is not None:
                definition = registry.get(name)
            if definition is None:
                raise ConfigError(f"unknown subagent type for wf.agent(): {name!r}")

        subagent_type = definition.frontmatter.name
        effective_run_options = _merge_run_options(
            run_options,
            output_schema=output_schema,
            final_tool_name=final_tool_name,
        )
        options_fingerprint = _run_options_fingerprint(effective_run_options)
        key = call_key(subagent_type, prompt, options_fingerprint)
        occurrence = self._journal.next_occurrence(key)
        display_name = label or name or "agent"

        cached = self._journal.lookup_record(key, occurrence)
        if cached is not None:
            await self._emit(
                WorkflowEvent(
                    kind="agent_replayed",
                    title=display_name,
                    call_key=key,
                    occurrence=occurrence,
                    subagent_type=subagent_type,
                    result_text=cached.result_text,
                    structured_output=cached.structured_output,
                    structured_error=cached.structured_error,
                )
            )
            return cached.result_text

        await self._emit(
            WorkflowEvent(
                kind="agent_start",
                title=display_name,
                call_key=key,
                occurrence=occurrence,
                subagent_type=subagent_type,
            )
        )

        result = await run_subagent(
            RunSubagentArgs(
                parent_session=self._host_session,
                parent_agent=self._agent,
                definition=definition,
                prompt=prompt,
                display_name=display_name,
                subagent_run_id=f"wf_{uuid4().hex[:8]}",
                tools_filter=tools,
                run_options=effective_run_options,
                emit=self._emit_sync,
                fork=fork,
                isolation=isolation,
                isolation_keep=isolation_keep,
            )
        )
        if result.errored:
            error = result.error or {"name": "WorkflowError", "message": "subagent failed"}
            raise WorkflowError(
                f"wf.agent({display_name!r}) failed: "
                f"{error.get('name', 'Error')}: {error.get('message', '')}",
                error=error,
            )

        result_text = result_text_for_caller(result)
        self._journal.record(
            key,
            occurrence,
            result_text,
            structured_output=result.structured_output,
            structured_error=result.structured_error,
        )
        await self._emit(
            WorkflowEvent(
                kind="agent_end",
                title=display_name,
                call_key=key,
                occurrence=occurrence,
                subagent_type=subagent_type,
                result_text=result_text,
                structured_output=result.structured_output,
                structured_error=result.structured_error,
            )
        )
        return result_text

    async def parallel(self, thunks: Sequence[Callable[[], Awaitable[Any]]]) -> list[Any]:
        """Run *thunks* concurrently (capped by ``max_concurrency``).

        Results are returned in input order.  The semaphore gates the thunks
        themselves — one slot per branch.
        """

        async def gated(thunk: Callable[[], Awaitable[Any]]) -> Any:
            async with self._semaphore:
                return await thunk()

        return list(await asyncio.gather(*(gated(thunk) for thunk in thunks)))

    async def pipeline(
        self,
        items: Sequence[Any],
        *stages: Callable[[Any], Awaitable[Any]],
    ) -> list[Any]:
        """Run each item through all stages independently — no barrier between
        stages, so item B's stage 1 and item A's stage 2 can overlap."""

        def make_chain(item: Any) -> Callable[[], Awaitable[Any]]:
            async def chain() -> Any:
                value = item
                for stage in stages:
                    value = await stage(value)
                return value

            return chain

        return await self.parallel([make_chain(item) for item in items])


def _merge_run_options(
    run_options: RunOptions | None,
    *,
    output_schema: Any = None,
    final_tool_name: str | None = None,
) -> RunOptions | None:
    if output_schema is None and final_tool_name is None:
        return run_options
    opts = run_options or RunOptions()
    updates: dict[str, Any] = {}
    if output_schema is not None:
        updates["output_schema"] = output_schema
    if final_tool_name is not None:
        updates["final_tool_name"] = final_tool_name
    return dataclasses.replace(opts, **updates)


def _run_options_fingerprint(run_options: RunOptions | None) -> str:
    if run_options is None:
        return ""
    payload: dict[str, Any] = {}
    for field in dataclasses.fields(run_options):
        if field.name in {"signal", "budget"}:
            continue
        value = getattr(run_options, field.name)
        if value is not None:
            payload[field.name] = _fingerprint_value(value)
    if not payload:
        return ""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _fingerprint_value(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return {
            field.name: _fingerprint_value(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
    if isinstance(value, dict):
        return {str(k): _fingerprint_value(v) for k, v in sorted(value.items(), key=str)}
    if isinstance(value, (list, tuple)):
        return [_fingerprint_value(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return f"{value.__class__.__module__}.{value.__class__.__qualname__}:{value!r}"
