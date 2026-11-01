"""PgQueuer dispatch with a separate Linch lease and fencing epoch per job."""

from __future__ import annotations

import asyncio
import json
import math
import random
from collections.abc import Awaitable, Callable, Iterable, Mapping
from contextlib import aclosing, suppress
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, cast
from uuid import uuid4

import asyncpg
from opentelemetry import context as otel_context
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from pgqueuer import DatabaseRetryEntrypointExecutor, Job, Queries, QueueManager
from pgqueuer.adapters.drivers.asyncpg import AsyncpgDriver
from pgqueuer.domain.models import AnyEvent, TableChangedEvent
from pgqueuer.errors import FailingListenerError
from pgqueuer.ports.driver import NotifyCallback

from ..agent import Agent
from ..errors import ConfigError
from ..events import ErrorEvent, ModelCallEvent, ResultEvent
from ..loop.contracts import _principal_from_value
from ..model_calls import _ModelCallLedger
from ..profiles import Profile
from ..run_store import (
    LeaseLostError,
    RunLease,
    RunLeaseConflictError,
    RunRecord,
    run_contract_from_meta,
)
from ..run_stores.postgres import PostgresRunStore
from ..session import RunOptions, Session
from ..sessions.postgres import PostgresSessionStore
from .dispatch import _BorrowedPool, _entrypoint, install

_TERMINAL = frozenset({"completed", "failed", "aborted"})
_INFRASTRUCTURE = (
    asyncpg.PostgresError,
    asyncpg.InterfaceError,
    OSError,
    TimeoutError,
    RunLeaseConflictError,
)

_RECONNECT = (
    asyncpg.PostgresConnectionError,
    asyncpg.AdminShutdownError,
    asyncpg.CrashShutdownError,
    asyncpg.CannotConnectNowError,
    asyncpg.TooManyConnectionsError,
    asyncpg.InterfaceError,
    OSError,
    TimeoutError,
    FailingListenerError,
)


@dataclass(frozen=True, slots=True)
class WorkerJob:
    """Host factory input. Use these exact store and pool handles for the agent.

    A fresh agent is owned and closed by the worker. ``pool`` is a borrowed
    asyncpg pool handle: closing a store using it does not close the host pool.

    Args:
        run: Durable run record claimed for this job.
        run_store: Worker-owned run store using the host's borrowed pool.
        pool: Borrowed pool handle supplied to the agent factory.
        session_store: Worker-owned session store using the borrowed pool.
        queue: Queue from which the job was dequeued, if supplied.

    Attributes:
        run: Claimed durable run record.
        run_store: Run store that owns the lease and writes.
        pool: Borrowed host pool handle.
        session_store: Session store shared by worker jobs.
        queue: Dequeued queue name, when supplied.
    """

    run: RunRecord
    run_store: PostgresRunStore
    pool: Any
    session_store: PostgresSessionStore
    queue: str | None = None


@dataclass(slots=True)
class _Execution:
    agent: Agent | None = None
    session: Session | None = None
    run_task: asyncio.Task[None] | None = None
    cancel_requested: bool = False


AgentFactory = Callable[[WorkerJob], Awaitable[Agent]]


def _positive(value: float, name: str) -> float:
    if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be positive and finite")
    return value


@dataclass(frozen=True, slots=True)
class _WakeCallback:
    callback: NotifyCallback
    completion_wakes: bool

    def __call__(self, payload: str | bytes | bytearray) -> None:
        if not self.completion_wakes:
            try:
                event = AnyEvent.model_validate_json(payload).root
            except ValueError:
                self.callback(payload)  # Preserve the upstream parser's diagnostics.
                return
            if isinstance(event, TableChangedEvent) and event.operation in ("delete", "truncate"):
                return
        self.callback(payload)


class _WorkerDriver(AsyncpgDriver):
    def __init__(self, connection: Any, completion_wakes: bool) -> None:
        super().__init__(connection)
        self.completion_wakes = completion_wakes

    async def add_listener(self, channel: str, callback: NotifyCallback) -> None:
        await super().add_listener(channel, _WakeCallback(callback, self.completion_wakes))

    async def remove_listener(self, channel: str, callback: NotifyCallback) -> None:
        await super().remove_listener(channel, _WakeCallback(callback, self.completion_wakes))


class _WorkerQueueManager(QueueManager):
    async def _effective_dequeue_timeout(self, dequeue_timeout: timedelta) -> timedelta:
        # PgQueuer 1.6 private hook: deferred/retry/stale work uses Worker polling.
        return dequeue_timeout


class Worker:
    """A fleet member. The host owns its asyncpg pool and process lifecycle.

    Configure identical per-queue limits on every fleet member. Zero means
    unlimited. ``stop()`` stops picking and drains current jobs; cancellation
    is cooperative shutdown, while process death leaves jobs for stall recovery.

    Args:
        pool: Host-owned asyncpg pool shared by worker stores and PgQueuer.
        agent_factory: Async factory returning a fresh agent for each job.
        worker_id: Stable owner identifier used for run leases.
        queues: Queue names mapped to fleet-wide concurrency limits; zero is unlimited.
        lease_ttl_s: Duration for each claimed run lease.
        stall_timeout_s: PgQueuer heartbeat timeout for stale jobs.
        poll_interval_s: Dequeue polling interval when notifications are quiet.
        retry_delay_s: Initial database-retry delay for queue callbacks.
        profiles: Released definitions available for queued runs and takeover.

    Attributes:
        agent_factory: Async factory used to construct each job's agent.
        worker_id: Owner identifier used for run leases.
        lease_ttl_s: Configured lease duration.
        stall_timeout_s: Queue heartbeat timeout.
        poll_interval_s: Queue polling interval.
        retry_delay_s: Initial infrastructure retry delay.
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
        profiles: Iterable[Profile] = (),
    ) -> None:
        """Configure a fleet worker over a host-owned asyncpg pool.

        Args:
            pool: Host-owned asyncpg pool shared by worker stores and PgQueuer.
            agent_factory: Async factory returning a fresh agent for each job.
            worker_id: Stable owner identifier used for run leases.
            queues: Queue names mapped to fleet-wide concurrency limits; zero is unlimited.
            lease_ttl_s: Duration for each claimed run lease.
            stall_timeout_s: PgQueuer heartbeat timeout for stale jobs.
            poll_interval_s: Dequeue polling interval when notifications are quiet.
            retry_delay_s: Initial database-retry delay for queue callbacks.
            profiles: Released definitions available for queued runs and takeover.

        Raises:
            ValueError: If the pool, queue limits, or timing settings are invalid.
        """
        self._pool = pool if isinstance(pool, _BorrowedPool) else _BorrowedPool(pool)
        if self._pool.get_max_size() < 2:
            raise ValueError("worker pool must allow at least two connections")
        self._run_store = PostgresRunStore("", pool=self._pool)
        self._session_store = PostgresSessionStore("", pool=self._pool)
        self.agent_factory = agent_factory
        self.worker_id = worker_id or str(uuid4())
        self.lease_ttl_s = _positive(lease_ttl_s, "lease_ttl_s")
        self.stall_timeout_s = _positive(stall_timeout_s, "stall_timeout_s")
        self.poll_interval_s = _positive(poll_interval_s, "poll_interval_s")
        self.retry_delay_s = _positive(retry_delay_s, "retry_delay_s")
        self._active_runs: set[str] = set()
        self._stop = asyncio.Event()
        self._manager: QueueManager | None = None
        self._limits = dict(queues) if queues is not None else {"default": 0}
        self._profiles = {(profile.id, profile.version): profile for profile in profiles}
        if not self._limits:
            raise ValueError("at least one queue is required")
        for queue, limit in self._limits.items():
            _entrypoint(queue)
            if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
                raise ValueError("concurrency_limit must be a non-negative integer")

    def _resolve_profile(self, raw: Any) -> Profile | None:
        if raw is None:
            return None
        if not isinstance(raw, Mapping):
            raise ConfigError("queued run has an invalid profile reference") from None
        profile_id, version = raw.get("id"), raw.get("version")
        if not isinstance(profile_id, str) or not isinstance(version, str):
            raise ConfigError("queued run has an invalid profile reference")
        profile = self._profiles.get((profile_id, version))
        if profile is None:
            raise ConfigError(
                f"worker has no profile {profile_id!r}@{version!r}; "
                "supply the pinned Profile definition"
            )
        if profile._contract()["content_hash"] != raw.get("content_hash"):
            raise ConfigError(
                f"profile {profile_id!r}@{version!r} content hash mismatch; "
                "supply the original definition"
            )
        return profile

    def _run_options(self, record: RunRecord) -> RunOptions:
        options: Any = record.meta
        if "run_contract" in record.meta:
            options = run_contract_from_meta(record.meta).payload.get("run_options")
            if not isinstance(options, Mapping):
                raise ConfigError("stored run contract has invalid run options")
        profile = self._resolve_profile(options.get("profile"))
        raw_principal = options.get("principal")
        principal = _principal_from_value(raw_principal) if raw_principal is not None else None
        return RunOptions(run_id=record.id, profile=profile, principal=principal)

    def _make_manager(self, connection: Any) -> QueueManager:
        manager = _WorkerQueueManager(
            Queries(_WorkerDriver(connection, completion_wakes=any(self._limits.values())))
        )
        for queue, limit in self._limits.items():
            manager.entrypoint(
                _entrypoint(queue),
                concurrency_limit=limit,
                executor_factory=self._executor,
                on_failure="hold",
            )(self._handle)
        manager.entrypoint(
            "linch.schedule",
            concurrency_limit=0,
            executor_factory=self._executor,
            on_failure="hold",
        )(self._fire)
        return manager

    def _executor(self, parameters: Any) -> DatabaseRetryEntrypointExecutor:
        return DatabaseRetryEntrypointExecutor(
            parameters=parameters, initial_delay=timedelta(seconds=self.retry_delay_s)
        )

    def stop(self) -> None:
        """Stop dequeueing; ``run()`` returns after active jobs finish."""
        self._stop.set()
        if self._manager is not None:
            self._manager.shutdown.set()

    async def run(self) -> None:
        """Process jobs until stopped, reconnecting after infrastructure loss."""
        serving = asyncio.create_task(self._serve())
        try:
            await asyncio.shield(serving)
        except asyncio.CancelledError:
            self.stop()
            while not serving.done():
                with suppress(asyncio.CancelledError, Exception):
                    await asyncio.shield(serving)
            serving.exception()  # Retrieve drain failures; preserve host cancellation.
            raise

    async def _serve(self) -> None:
        ready, ceiling, rng = False, 0.1, random.Random()
        while not self._stop.is_set():
            try:
                async with asyncio.timeout(2):
                    if not ready:
                        await install(self._pool)
                        await self._run_store._ensure()
                        await self._session_store._ensure()
                        ready = True
                    if self._stop.is_set():
                        return
                async with self._pool.acquire(timeout=2) as connection:
                    try:
                        if self._stop.is_set():
                            return
                        manager = self._make_manager(connection)
                        self._manager = manager
                        await manager.run(
                            heartbeat_timeout=timedelta(seconds=self.stall_timeout_s),
                            dequeue_timeout=timedelta(seconds=self.poll_interval_s),
                            batch_size=1,
                            log_aggregation_interval=timedelta(0),
                            shutdown_on_listener_failure=True,
                        )
                    finally:
                        # Drain before closing: old failure buffers cannot revive
                        # on a fresh connection and hold a nonterminal job.
                        with suppress(asyncpg.InterfaceError):
                            await connection.close()
            except asyncpg.ObjectNotInPrerequisiteStateError as exc:
                if getattr(exc, "severity", None) != "FATAL":
                    raise
            except _RECONNECT:
                pass
            finally:
                self._manager = None
            with suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), rng.uniform(ceiling / 2, ceiling))
            ceiling = min(2, ceiling * 2)

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
                deadline = loop.time() + self.lease_ttl_s
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
        options = self._run_options(record)
        execution.agent = await self.agent_factory(
            WorkerJob(record, store, self._pool, self._session_store, payload.get("queue"))
        )
        if execution.agent.run_store is not store:
            raise ValueError("agent factory must use WorkerJob.run_store")
        if execution.agent.session_store is not self._session_store:
            raise ValueError("agent factory must use WorkerJob.session_store")
        execution.session = await execution.agent.session(id=record.session_id)
        iterator = (
            execution.session.resume(record.id, options)
            if record.checkpoint is not None
            else execution.session.run(payload["prompt"], options)
        )
        async with aclosing(cast(Any, iterator)):
            async for _ in iterator:
                pass

    async def _handle(self, job: Job) -> None:
        execution = _Execution()
        lifecycle = asyncio.create_task(self._run_job(job, execution))
        while True:
            try:
                # Queue callback cancellation must not interrupt lease release,
                # terminal verification, or agent teardown and become an ACK.
                await asyncio.shield(lifecycle)
                return
            except asyncio.CancelledError:
                execution.cancel_requested = True
                if execution.run_task is not None:
                    execution.run_task.cancel()
                if lifecycle.cancelled():
                    lifecycle = asyncio.create_task(self._check_finished_job(job))

    async def _check_finished_job(self, job: Job) -> None:
        rid = json.loads(job.payload or b"{}")["run_id"]
        await self._require_terminal(self._run_store, rid)

    async def _require_terminal(self, store: PostgresRunStore, rid: str) -> None:
        current = await store.load_run(rid)
        if current is None or current.status not in _TERMINAL:
            status = current.status if current is not None else "missing"
            raise RuntimeError(f"run {rid} exited non-terminal ({status}); retry dispatch")

    async def _run_job(self, job: Job, execution: _Execution) -> None:
        payload = json.loads(job.payload or b"{}")
        rid, sid = payload["run_id"], payload["session_id"]
        store = self._run_store
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
        try:
            lease = await store.claim(rid, self.worker_id, self.lease_ttl_s)
        except BaseException:
            self._active_runs.discard(rid)
            raise
        started = asyncio.get_running_loop().time()
        deadline = started + self.lease_ttl_s
        run_task: asyncio.Task[None] | None = None
        heartbeat: asyncio.Task[None] | None = None
        token = None
        try:
            try:
                token = otel_context.attach(
                    TraceContextTextMapPropagator().extract(
                        {"traceparent": payload.get("traceparent") or ""}
                    )
                )
                record = await store.load_run(rid)
                if record is None:
                    raise KeyError(f"run not found: {rid}")
                if record.status not in _TERMINAL and not execution.cancel_requested:
                    run_task = asyncio.create_task(self._invoke(execution, store, record, payload))
                    execution.run_task = run_task
                    # Renew immediately so two short failed requests have a full
                    # TTL budget; subsequent attempts run on the ttl/3 cadence.
                    heartbeat = asyncio.create_task(
                        self._heartbeat(store, lease, run_task, deadline)
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
                    ledger = _ModelCallLedger(rid)
                    rows = await store.load_events(rid)
                    await ledger.restore(
                        [row.event for row in rows if isinstance(row.event, ModelCallEvent)]
                    )
                    if current is not None and current.checkpoint is not None:
                        current.checkpoint.total_usage = ledger.total_usage
                    error = {"name": type(exc).__name__, "message": str(exc), "retryable": False}
                    with suppress(LeaseLostError):
                        await store.mark_failed(
                            rid,
                            current.checkpoint if current is not None else None,
                            error=error,
                            events=[
                                ErrorEvent(error=error),
                                ResultEvent(
                                    subtype="error",
                                    stop_reason="error",
                                    duration_ms=int(
                                        (asyncio.get_running_loop().time() - started) * 1000
                                    ),
                                    total_usage=ledger.total_usage,
                                    total_cost_usd=ledger.total_cost_usd,
                                    usage_complete=ledger.usage_complete,
                                ),
                            ],
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
            await self._require_terminal(store, rid)
        finally:
            try:
                if execution.agent is not None:
                    await execution.agent.close()
            finally:
                self._active_runs.discard(rid)
                if token is not None:
                    otel_context.detach(token)

    async def _fire(self, job: Job) -> None:
        from .schedules import _fire

        await _fire(self._pool, json.loads(job.payload or b"{}"))
