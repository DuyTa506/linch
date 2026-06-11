"""Workflow engine — drives a deterministic workflow function to completion."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from ..errors import ConfigError
from ..events import Event
from .context import WorkflowContext
from .journal import WorkflowJournal


async def run_workflow(
    agent: Any,
    fn: Callable[[WorkflowContext], Awaitable[Any]],
    *,
    budget: Any = None,
    run_id: str | None = None,
    max_concurrency: int = 4,
    on_event: Callable[[Event], None] | None = None,
) -> Any:
    """Run workflow function *fn* and return its value.

    A host session is created to parent all ``wf.agent`` subagent runs; the
    shared *budget* (or ``agent.budget``) caps the whole tree.  With *run_id*
    and a configured ``Agent(run_store=...)``, every ``wf.agent`` result is
    journaled — re-invoking with the same *run_id* replays the unchanged call
    prefix from the journal instead of re-running subagents.
    """
    raw_store = getattr(agent, "run_store", None)
    if run_id is not None and raw_store is None:
        raise ConfigError("run_workflow(run_id=...) requires Agent(run_store=...)")
    # Durable journaling only when both a store and a run_id are present.
    store = raw_store if (raw_store is not None and run_id is not None) else None

    host = await agent.session(
        meta={"workflow": getattr(fn, "__name__", "workflow")},
    )

    journal = WorkflowJournal()
    if store is not None and run_id is not None:
        existing = await store.load_run(run_id)
        if existing is not None:
            journal = WorkflowJournal.from_stored_events(await store.load_events(run_id))
        else:
            await store.create_run(host.id, id=run_id)

    resolved_budget = budget if budget is not None else getattr(agent, "budget", None)
    # Subagent children inherit the budget from the host session's
    # active_budget (see subagents/runner.py).
    host.active_budget = resolved_budget

    wf = WorkflowContext(
        agent,
        host,
        journal=journal,
        budget=resolved_budget,
        max_concurrency=max_concurrency,
        on_event=on_event,
        store=store,
        run_id=run_id if store is not None else None,
    )

    try:
        result = await fn(wf)
    except BaseException:
        if store is not None and run_id is not None:
            await store.mark_failed(run_id)
        raise
    else:
        if store is not None and run_id is not None:
            from ..run_store import RunCheckpoint
            from ..types import Usage

            await store.mark_completed(
                run_id,
                RunCheckpoint(
                    phase="completed",
                    prompt=getattr(fn, "__name__", "workflow"),
                    turn_index=0,
                    total_usage=Usage(),
                ),
            )
        return result
    finally:
        agent._sessions.pop(host.id, None)
