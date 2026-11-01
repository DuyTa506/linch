"""Workflow engine — drives a deterministic workflow function to completion."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any, cast
from uuid import uuid4

from ..._wire import WIRE_VERSION, require_wire_version
from ...errors import ConfigError, WorkflowSuspended, WorkflowTimeoutError
from ...events import ErrorEvent, Event
from ...run_store import LeaseLostError
from .context import SNAPSHOT_NAMESPACE, WorkflowContext, workflow_checkpoint
from .journal import WorkflowJournal

_WORKFLOW_LEASE_TTL_S = 300.0


def _workflow_error_payload(exc: BaseException) -> dict[str, object]:
    """Build the durable error envelope for a failed workflow."""
    return {
        "name": type(exc).__name__,
        "message": str(exc),
        "retryable": getattr(exc, "retryable", False),
    }


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

    Args:
        agent: Agent that owns the workflow host session and optional run store.
        fn: Async workflow function receiving its :class:`WorkflowContext`.
        budget: Shared budget for child calls; defaults to the agent budget.
        run_id: Durable run identifier enabling journaling and resume.
        max_concurrency: Maximum number of workflow branches in one fan-out.
        max_agent_concurrency: Maximum number of live child provider calls.
        on_event: Callback for workflow and child events.
        step_timeout_ms: Default timeout for journaled agent and step calls.
        deadline_ms: Overall workflow deadline in milliseconds.
        journal_snapshot_every: Checkpoint interval for journal snapshots.
        signal: Abort signal propagated to child calls.
        resume: Answers for pending interrupt keys.

    Returns:
        The value returned by ``fn``.

    Raises:
        ConfigError: If durable journaling is requested without a run store.
        WorkflowSuspended: If the workflow reaches an unanswered interrupt.
        WorkflowTimeoutError: If the overall deadline expires.
        LeaseLostError: If a claimed workflow loses its run lease.
    """
    raw_store = getattr(agent, "run_store", None)
    if run_id is not None and raw_store is None:
        raise ConfigError("run_workflow(run_id=...) requires Agent(run_store=...)")
    # Durable journaling only when both a store and a run_id are present.
    store = raw_store if (raw_store is not None and run_id is not None) else None
    workflow_name = getattr(fn, "__name__", "workflow")
    host = None
    lease_ref: list[Any] = [None]
    lease_lost_ref: list[LeaseLostError | None] = [None]
    try:
        existing = (
            await store.load_run(run_id) if store is not None and run_id is not None else None
        )
        if existing is not None:
            require_wire_version(existing.meta.get("schema_version"))

        host = await agent.session(meta={"workflow": workflow_name})
        journal = WorkflowJournal()
        if store is not None and run_id is not None:
            if existing is not None:
                await _claim_workflow_run(store, run_id, lease_ref)
                snapshot, after_seq = _stored_snapshot(existing)
                journal = WorkflowJournal.from_stored_events(
                    await store.load_events(run_id, after_seq=after_seq),
                    snapshot=snapshot,
                )
            else:
                await store.create_run(host.id, id=run_id, meta={"schema_version": WIRE_VERSION})
                await _claim_workflow_run(store, run_id, lease_ref)

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

        async def drive() -> Any:
            try:
                result = await _run_to_deadline(fn, wf, deadline_ms)
            except asyncio.CancelledError:
                if lease_lost_ref[0] is not None:
                    raise lease_lost_ref[0] from None
                raise
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
            except LeaseLostError:
                raise
            except BaseException:
                if store is not None and run_id is not None:
                    await store.mark_failed(run_id)
                raise
            else:
                if store is not None and run_id is not None:
                    try:
                        await store.mark_completed(
                            run_id, workflow_checkpoint(workflow_name, "completed")
                        )
                    except (asyncio.CancelledError, LeaseLostError):
                        raise
                    except BaseException as exc:
                        error = _workflow_error_payload(exc)
                        await store.mark_failed(
                            run_id,
                            error=error,
                            events=[ErrorEvent(error=error)],
                        )
                        raise
                return result

        return await _run_claimed_workflow(drive, store, lease_ref, lease_lost_ref)
    finally:
        try:
            if host is not None:
                await _drain_awaitable(agent.release_session(host.id, force=True))
        finally:
            if lease_ref[0] is not None and store is not None:
                with suppress(LeaseLostError):
                    await _drain_awaitable(store.release(lease_ref[0]))


async def _claim_workflow_run(store: Any, run_id: str, lease_ref: list[Any]) -> Any:
    """Claim a durable workflow run when the store supports optional leases."""
    claim = getattr(store, "claim", None)
    renew = getattr(store, "renew", None)
    release = getattr(store, "release", None)
    if not any(callable(method) for method in (claim, renew, release)):
        return None
    if not all(callable(method) for method in (claim, renew, release)):
        raise ConfigError("run store lease capability requires claim, renew, release")
    claim_fn = cast(Callable[..., Awaitable[Any]], claim)

    async def execute_claim() -> Any:
        return await claim_fn(run_id, owner=f"workflow:{uuid4().hex}", ttl_s=_WORKFLOW_LEASE_TTL_S)

    claim_task = asyncio.create_task(execute_claim())
    try:
        lease = await asyncio.shield(claim_task)
    except asyncio.CancelledError:
        lease = await _drain_task_result(claim_task)
        lease_ref[0] = lease
        raise
    lease_ref[0] = lease
    return lease


async def _run_claimed_workflow(
    run: Callable[[], Awaitable[Any]],
    store: Any,
    lease_ref: list[Any],
    lease_lost_ref: list[LeaseLostError | None],
) -> Any:
    """Run a workflow and renew an optional lease while it is active."""
    if lease_ref[0] is None or store is None or not callable(getattr(store, "renew", None)):
        return await run()

    async def execute() -> Any:
        return await run()

    workflow_task = asyncio.create_task(execute())
    heartbeat_task = asyncio.create_task(_renew_workflow_lease(store, lease_ref))
    try:
        done, _ = await asyncio.wait(
            (workflow_task, heartbeat_task), return_when=asyncio.FIRST_COMPLETED
        )
        if workflow_task in done:
            return workflow_task.result()
        heartbeat_error = heartbeat_task.exception()
        if isinstance(heartbeat_error, LeaseLostError):
            lease_lost_ref[0] = heartbeat_error
        workflow_task.cancel()
        await _drain_task(workflow_task)
        if heartbeat_error is not None:
            raise heartbeat_error
        return workflow_task.result()
    finally:
        cleanup_cancelled = False
        if not workflow_task.done():
            workflow_task.cancel()
        try:
            await _drain_task(workflow_task)
        except asyncio.CancelledError:
            cleanup_cancelled = True
        if not heartbeat_task.done():
            heartbeat_task.cancel()
        try:
            await _drain_task(heartbeat_task)
        except asyncio.CancelledError:
            cleanup_cancelled = True
        if cleanup_cancelled:
            raise asyncio.CancelledError


async def _renew_workflow_lease(store: Any, lease_ref: list[Any]) -> None:
    """Renew a workflow lease periodically until the workflow finishes."""
    while True:
        await asyncio.sleep(_WORKFLOW_LEASE_TTL_S / 3)
        lease_ref[0] = await store.renew(lease_ref[0])


async def _drain_task(task: asyncio.Task[Any], *, suppress_errors: bool = True) -> None:
    """Wait for a cleanup task despite repeated caller cancellation."""
    caller_cancelled = False
    task_error: BaseException | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                try:
                    task.result()
                except BaseException as exc:
                    task_error = exc
                else:
                    caller_cancelled = True
                break
            caller_cancelled = True
        except BaseException as exc:
            task_error = exc
            break
    if task_error is None:
        try:
            task.result()
        except BaseException as exc:
            task_error = exc
    if task_error is not None and not suppress_errors:
        raise task_error
    if caller_cancelled:
        raise asyncio.CancelledError


async def _drain_awaitable(awaitable: Awaitable[Any], *, suppress_errors: bool = False) -> None:
    """Run and drain one cleanup awaitable without losing its failure."""

    async def execute() -> Any:
        return await awaitable

    task = asyncio.create_task(execute())
    await _drain_task(task, suppress_errors=suppress_errors)


async def _drain_task_result(task: asyncio.Task[Any]) -> Any:
    """Drain a task through caller cancellation and return its result."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


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
    except TimeoutError as exc:
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
