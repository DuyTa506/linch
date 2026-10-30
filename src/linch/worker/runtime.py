"""PgQueuer dispatch with a separate Linch lease and fencing epoch per job."""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Awaitable, Callable, Mapping
from contextlib import aclosing, suppress
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, cast
from uuid import uuid4

import asyncpg
from opentelemetry import context as otel_context
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from pgqueuer import AsyncpgPoolDriver, DatabaseRetryEntrypointExecutor, Job, Queries, QueueManager

from ..agent import Agent
from ..run_store import (
    RUN_CONTRACT_META_KEY,
    LeaseLostError,
    RunLease,
    RunLeaseConflictError,
    RunRecord,
    _safe_meta,
    ensure_run_contract_compatible,
    run_contract_from_meta,
)
from ..run_stores.postgres import PostgresRunStore, _row_record
from ..session import RunOptions, Session
from ..sessions.memory import now_iso
from .dispatch import _BorrowedPool, _entrypoint, install

_TERMINAL = frozenset({"completed", "failed", "aborted"})
_INFRASTRUCTURE = (
    asyncpg.PostgresError,
    asyncpg.InterfaceError,
    OSError,
    TimeoutError,
    RunLeaseConflictError,
)


class _JobRunStore(PostgresRunStore):
    def __init__(self, pool: Any, run_id: str, session_id: str) -> None:
        super().__init__("", pool=_BorrowedPool(pool))
        self._initialized = True  # Worker.run initialized this pool's schema.
        self._run_id = run_id
        self._session_id = session_id

    async def close(self) -> None:
        # Worker owns this per-job handle and releases only after agent teardown.
        # The host owns the underlying pool.
        pass

    def _assert_fence(self, run_id: str, row: Any) -> None:
        if run_id == self._run_id and run_id not in self._held_leases:
            raise LeaseLostError(f"job no longer owns run {run_id}")
        super()._assert_fence(run_id, row)

    async def create_run(
        self, session_id: str, *, id: str | None = None, meta: dict[str, object] | None = None
    ) -> RunRecord:
        if session_id != self._session_id:
            return await super().create_run(session_id, id=id, meta=meta)
        if id is not None and id != self._run_id:
            raise ValueError("queued run ID cannot change")
        # The ordinary Session.run path builds the execution contract. Bind it
        # to the dispatch receipt rather than silently creating a second run.
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                row = await self._locked_run(conn, self._run_id)
                self._assert_fence(self._run_id, row)
                existing = _row_record(row)
                if existing.session_id != session_id or existing.status in _TERMINAL:
                    raise ValueError("queued run is no longer startable")
                if existing.checkpoint is not None:
                    raise ValueError("checkpointed runs must be resumed")
                incoming = _safe_meta(meta)
                if RUN_CONTRACT_META_KEY in existing.meta:
                    ensure_run_contract_compatible(
                        run_contract_from_meta(existing.meta), run_contract_from_meta(incoming)
                    )
                    incoming[RUN_CONTRACT_META_KEY] = existing.meta[RUN_CONTRACT_META_KEY]
                merged = existing.meta | incoming
                updated = await conn.fetchrow(
                    """
                    UPDATE runs SET status='running', meta=$2, updated_at=$3
                    WHERE id=$1 RETURNING *
                """,
                    self._run_id,
                    json.dumps(merged, allow_nan=False),
                    now_iso(),
                )
                return _row_record(updated)


@dataclass(frozen=True, slots=True)
class WorkerJob:
    """Host factory input. Use these exact store and pool handles for the agent.

    A fresh agent is owned and closed by the worker. ``pool`` is a borrowed
    asyncpg pool handle: closing a store using it does not close the host pool.
    """

    run: RunRecord
    run_store: PostgresRunStore
    pool: Any
    queue: str | None = None


@dataclass(slots=True)
class _Execution:
    agent: Agent | None = None
    session: Session | None = None


AgentFactory = Callable[[WorkerJob], Awaitable[Agent]]


def _positive(value: float, name: str) -> float:
    if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be positive and finite")
    return value


class Worker:
    """A fleet member. The host owns its asyncpg pool and process lifecycle.

    Configure identical per-queue limits on every fleet member. Zero means
    unlimited. ``stop()`` stops picking and drains current jobs; cancellation
    is cooperative shutdown, while process death leaves jobs for stall recovery.
    """

    def __init__(
        self,
        pool: Any,
        agent_factory: AgentFactory,
        *,
        worker_id: str | None = None,
        queues: Mapping[str, int] | None = None,
        lease_ttl_s: float = 30,
        stall_timeout_s: float = 60,
        poll_interval_s: float = 1,
        retry_delay_s: float = 1,
    ) -> None:
        self._pool = pool
        self.agent_factory = agent_factory
        self.worker_id = worker_id or str(uuid4())
        self.lease_ttl_s = _positive(lease_ttl_s, "lease_ttl_s")
        self.stall_timeout_s = _positive(stall_timeout_s, "stall_timeout_s")
        self.poll_interval_s = _positive(poll_interval_s, "poll_interval_s")
        self.retry_delay_s = _positive(retry_delay_s, "retry_delay_s")
        self._active_runs: set[str] = set()
        self._manager = QueueManager(Queries(AsyncpgPoolDriver(pool)))
        limits = dict(queues) if queues is not None else {"default": 0}
        if not limits:
            raise ValueError("at least one queue is required")
        for queue, limit in limits.items():
            self._manager.entrypoint(
                _entrypoint(queue),
                concurrency_limit=limit,
                executor_factory=self._executor,
                on_failure="hold",
            )(self._handle)
        self._manager.entrypoint(
            "linch.schedule",
            concurrency_limit=1,
            executor_factory=self._executor,
            on_failure="hold",
        )(self._fire)

    def _executor(self, parameters: Any) -> DatabaseRetryEntrypointExecutor:
        return DatabaseRetryEntrypointExecutor(
            parameters=parameters, initial_delay=timedelta(seconds=self.retry_delay_s)
        )

    def stop(self) -> None:
        """Stop dequeueing; ``run()`` returns after active jobs finish."""
        self._manager.shutdown.set()

    async def run(self) -> None:
        """Process jobs until ``stop``; initialize the schema if necessary."""
        await install(self._pool)
        await self._manager.run(
            heartbeat_timeout=timedelta(seconds=self.stall_timeout_s),
            dequeue_timeout=timedelta(seconds=self.poll_interval_s),
            batch_size=1,
            log_aggregation_interval=timedelta(0),
        )

    async def _heartbeat(
        self,
        store: PostgresRunStore,
        lease: RunLease,
        run_task: asyncio.Task[None],
        deadline: float | None = None,
    ) -> None:
        loop = asyncio.get_running_loop()
        deadline = deadline if deadline is not None else loop.time() + self.lease_ttl_s
        while not run_task.done():
            started = loop.time()
            if started >= deadline:
                run_task.cancel()
                return
            try:
                async with asyncio.timeout(deadline - started):
                    lease = await store.renew(lease)
            except LeaseLostError:
                run_task.cancel()
                return
            except Exception:
                # A failed request is not proof of lost ownership. Retry within
                # the last confirmed TTL, including a bound on a hung renewal.
                pass
            else:
                # Starting the budget before I/O conservatively accounts for
                # round-trip latency rather than extending an unconfirmed lease.
                deadline = started + self.lease_ttl_s
            remaining = deadline - loop.time()
            if remaining <= 0:
                run_task.cancel()
                return
            await asyncio.sleep(min(self.lease_ttl_s / 3, remaining))

    async def _invoke(
        self,
        execution: _Execution,
        store: PostgresRunStore,
        record: RunRecord,
        payload: dict[str, Any],
    ) -> None:
        execution.agent = await self.agent_factory(
            WorkerJob(record, store, _BorrowedPool(self._pool), payload.get("queue"))
        )
        if execution.agent.run_store is not store:
            raise ValueError("agent factory must use WorkerJob.run_store")
        execution.session = await execution.agent.session(id=record.session_id)
        options = RunOptions()
        iterator = (
            execution.session.resume(record.id, options)
            if record.checkpoint is not None
            else execution.session.run(payload["prompt"], options)
        )
        async with aclosing(cast(Any, iterator)):
            async for _ in iterator:
                pass

    async def _handle(self, job: Job) -> None:
        payload = json.loads(job.payload or b"{}")
        rid, sid = payload["run_id"], payload["session_id"]
        store = _JobRunStore(self._pool, rid, sid)
        record = await store.load_run(rid)
        if record is None:
            raise KeyError(f"run not found: {rid}")
        if record.session_id != sid:
            raise ValueError("job session does not match run")
        if record.status in _TERMINAL:
            return
        if rid in self._active_runs:
            raise RunLeaseConflictError(f"run {rid} is active on this worker")
        self._active_runs.add(rid)
        claim_started = asyncio.get_running_loop().time()
        try:
            lease = await store.claim(rid, self.worker_id, self.lease_ttl_s)
        except BaseException:
            self._active_runs.discard(rid)
            raise
        execution = _Execution()
        run_task: asyncio.Task[None] | None = None
        heartbeat: asyncio.Task[None] | None = None
        token = otel_context.attach(
            TraceContextTextMapPropagator().extract(
                {"traceparent": payload.get("traceparent") or ""}
            )
        )
        try:
            try:
                record = await store.load_run(rid)
                if record is None:
                    raise KeyError(f"run not found: {rid}")
                if record.status not in _TERMINAL:
                    run_task = asyncio.create_task(self._invoke(execution, store, record, payload))
                    # Renew immediately so two short failed requests have a full
                    # TTL budget; subsequent attempts run on the ttl/3 cadence.
                    heartbeat = asyncio.create_task(
                        self._heartbeat(store, lease, run_task, claim_started + self.lease_ttl_s)
                    )
                    await run_task
            except (asyncio.CancelledError, LeaseLostError):
                # Cancellation/loss is an orphan, not an aborted or failed run.
                # The common terminal check below decides ACK versus retry.
                pass
            except Exception as exc:
                current = await store.load_run(rid)
                if current is None or current.status not in _TERMINAL:
                    if isinstance(exc, _INFRASTRUCTURE):
                        raise
                    with suppress(LeaseLostError):
                        await store.mark_failed(
                            rid, error={"type": type(exc).__name__, "message": str(exc)}
                        )
            finally:
                if heartbeat is not None:
                    heartbeat.cancel()
                    with suppress(asyncio.CancelledError):
                        await heartbeat
                if run_task is not None and not run_task.done():
                    run_task.cancel()
                    with suppress(asyncio.CancelledError, LeaseLostError):
                        await run_task
                try:
                    # Quiesce session-owned work before ending its run lease.
                    # The cancelled iterator has already cleared active state,
                    # so this cleanup does not abort the run's signal.
                    if execution.session is not None:
                        with suppress(LeaseLostError):
                            await execution.session.aclose(force=True)
                finally:
                    with suppress(LeaseLostError):
                        await store.release(lease)
            current = await store.load_run(rid)
            if current is None or current.status not in _TERMINAL:
                status = current.status if current is not None else "missing"
                raise RuntimeError(f"run {rid} exited non-terminal ({status}); retry dispatch")
        finally:
            try:
                if execution.agent is not None:
                    await execution.agent.close()
                else:
                    await store.close()
            finally:
                self._active_runs.discard(rid)
                otel_context.detach(token)

    async def _fire(self, job: Job) -> None:
        from .schedules import _fire

        await _fire(self._pool, json.loads(job.payload or b"{}"))
