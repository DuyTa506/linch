"""Workflow engine — drives a deterministic workflow function to completion."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, cast

from ..errors import ConfigError, WorkflowSuspended, WorkflowTimeoutError
from ..events import Event
from .context import SNAPSHOT_NAMESPACE, WorkflowContext, workflow_checkpoint
from .journal import CURRENT_FINGERPRINT_VERSION, WorkflowJournal


async def run_workflow(
    agent: Any,
    fn: Callable[[WorkflowContext], Awaitable[Any]],
    *,
    budget: Any = None,
    run_id: str | None = None,
    max_concurrency: int = 4,
    max_agent_concurrency: int = 0,
    on_event: Callable[[Event], None] | None = None,
    step_timeout_ms: float | None = None,
    deadline_ms: float | None = None,
    journal_snapshot_every: int = 0,
    signal: Any = None,
    resume: dict[str, Any] | None = None,
) -> Any:
    """Run workflow function *fn* and return its value.

    A host session is created to parent all ``wf.agent`` subagent runs; the
    shared *budget* (or ``agent.budget``) caps the whole tree.  With *run_id*
    and a configured ``Agent(run_store=...)``, every ``wf.agent`` and
    ``wf.step`` result is journaled — re-invoking with the same *run_id*
    replays the unchanged call prefix from the journal instead of re-running
    subagents or re-executing code.

    *step_timeout_ms* is the default per-call wall-clock budget for ``wf.agent``
    and ``wf.step`` (each call can override it, and ``0`` opts out), while
    *deadline_ms* caps the whole workflow: when it expires the function is
    cancelled and ``WorkflowTimeoutError`` is raised, leaving the journaled
    prefix intact so a resume picks up where it stopped.  *signal* is an
    ``AbortContext`` that stops the workflow at its next journaled call and
    propagates into every subagent it spawns.

    *journal_snapshot_every* checkpoints the journal every N records so a
    resume folds only the events after it, instead of the whole log. Off by
    default: it trades a periodic write against a one-shot load, which only
    pays off for long runs.

    *resume* answers pending ``wf.interrupt`` calls, one value per interrupt
    key. Without an answer, an interrupt raises ``WorkflowSuspended`` and the
    run is marked ``"suspended"`` — parked at a decision point, not failed.
    """
    raw_store = getattr(agent, "run_store", None)
    if run_id is not None and raw_store is None:
        raise ConfigError("run_workflow(run_id=...) requires Agent(run_store=...)")
    # Durable journaling only when both a store and a run_id are present.
    store = raw_store if (raw_store is not None and run_id is not None) else None

    host = await agent.session(
        meta={"workflow": getattr(fn, "__name__", "workflow")},
    )

    workflow_name = getattr(fn, "__name__", "workflow")

    journal = WorkflowJournal()
    if store is not None and run_id is not None:
        existing = await store.load_run(run_id)
        if existing is not None:
            fingerprint_version = existing.meta.get("journal_fingerprint_version", 1)
            snapshot, after_seq = _stored_snapshot(existing)
            journal = WorkflowJournal.from_stored_events(
                await store.load_events(run_id, after_seq=after_seq),
                fingerprint_version=cast(int, fingerprint_version),
                snapshot=snapshot,
            )
        else:
            await store.create_run(
                host.id,
                id=run_id,
                meta={"journal_fingerprint_version": CURRENT_FINGERPRINT_VERSION},
            )

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
        max_agent_concurrency=max_agent_concurrency,
        on_event=on_event,
        store=store,
        run_id=run_id if store is not None else None,
        step_timeout_ms=step_timeout_ms,
        signal=signal,
        resume=resume,
        workflow_name=workflow_name,
        journal_snapshot_every=journal_snapshot_every,
    )

    try:
        result = await _run_to_deadline(fn, wf, deadline_ms)
    except WorkflowSuspended:
        # A suspend is a parked run, not a failed one — checkpoint it so the
        # store reports "suspended" and the journal prefix stays resumable.
        if store is not None and run_id is not None:
            checkpoint = workflow_checkpoint(workflow_name, "workflow_suspended")
            # Carry any journal snapshot forward; overwriting it here would
            # silently downgrade the next resume to a full event-log fold.
            checkpoint.extension_state.update(wf.extension_state())
            await store.save_checkpoint(run_id, checkpoint, status="suspended")
        raise
    except BaseException:
        if store is not None and run_id is not None:
            await store.mark_failed(run_id)
        raise
    else:
        if store is not None and run_id is not None:
            await store.mark_completed(run_id, workflow_checkpoint(workflow_name, "completed"))
        return result
    finally:
        await agent.release_session(host.id, force=True)


async def _run_to_deadline(
    fn: Callable[[WorkflowContext], Awaitable[Any]],
    wf: WorkflowContext,
    deadline_ms: float | None,
) -> Any:
    """Await the workflow function, capped by *deadline_ms* when one is set.

    An unset (or non-positive) deadline is the common case and must cost
    nothing, so it never constructs a ``wait_for`` frame.
    """
    if deadline_ms is None or deadline_ms <= 0:
        return await fn(wf)
    try:
        return await asyncio.wait_for(fn(wf), timeout=deadline_ms / 1000.0)
    except asyncio.TimeoutError as exc:
        raise WorkflowTimeoutError(f"workflow exceeded its deadline of {deadline_ms:g}ms") from exc


def _stored_snapshot(existing: Any) -> tuple[list[Any] | None, int]:
    """Read a run's journal snapshot and its event-log watermark.

    Correct by construction when there is none, or when it is malformed: the
    caller falls back to folding the run's whole event log from seq 0.

    Returns:
        The snapshot rows (or None) and the seq to load events after.
    """
    checkpoint = getattr(existing, "checkpoint", None)
    if checkpoint is None:
        return None, 0
    raw = checkpoint.extension_state.get(SNAPSHOT_NAMESPACE)
    if not isinstance(raw, dict):
        return None, 0
    records = raw.get("records")
    after_seq = raw.get("after_seq")
    if not isinstance(records, list) or not isinstance(after_seq, int) or after_seq < 0:
        return None, 0
    return records, after_seq
