"""Atomic run dispatch. Import explicitly; PgQueuer never enters the core import graph."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from pgqueuer import Queries

from ..loop.contracts import _principal_contract
from ..permissions import Principal
from ..profiles import Profile
from ..run_stores.postgres import PostgresRunStore
from ..storage._pg import _BorrowedPool


@dataclass(frozen=True, slots=True)
class RunRequest:
    """Describe one queued run and its optional pinned identity.

    Args:
        profile: Released definition whose identity reference is persisted.
        principal: Tenant/user identity restored by the worker.
    """

    session_id: str
    prompt: str
    run_id: str = field(default_factory=lambda: str(uuid4()))
    queue: str | None = None
    meta: dict[str, object] = field(default_factory=dict)
    profile: Profile | None = None
    principal: Principal | None = None


def _request_refs(request: RunRequest) -> dict[str, object]:
    refs: dict[str, object] = {}
    if request.profile is not None:
        refs["profile"] = request.profile._contract()
    if request.principal is not None:
        refs["principal"] = _principal_contract(request.principal)
    return refs


def _entrypoint(queue: str | None) -> str:
    if queue is not None and (not isinstance(queue, str) or not queue.strip()):
        raise ValueError("queue must be a non-empty string")
    return f"linch.run:{queue or 'default'}"


def _traceparent() -> str | None:
    carrier: dict[str, str] = {}
    TraceContextTextMapPropagator().inject(carrier)
    return carrier.get("traceparent")


async def install(pool: Any) -> None:
    """Initialize current Linch run and worker schemas and a fresh PgQueuer installation.

    Run once at host startup. Existing PgQueuer schemas must already be current;
    migrations are host-owned. Safe for concurrent process startup.
    """
    pool = pool if isinstance(pool, _BorrowedPool) else _BorrowedPool(pool)
    async with PostgresRunStore("", pool=pool):
        pass
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1)", 0x4C494E4348574B52)
            queries = Queries.from_asyncpg_connection(conn)
            if not await queries.schema_is_installed():
                await queries.install()
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS linch_worker_schedules (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    cron TEXT,
                    interval_s DOUBLE PRECISION,
                    payload TEXT NOT NULL,
                    enabled BOOLEAN NOT NULL DEFAULT TRUE,
                    queue TEXT,
                    next_fire TIMESTAMPTZ NOT NULL,
                    traceparent TEXT,
                    CHECK ((cron IS NULL) <> (interval_s IS NULL))
                )
            """)


async def _enqueue(conn: Any, requests: Sequence[RunRequest], traceparent: str | None) -> list[str]:
    # Sorted row acquisition avoids deadlocks for overlapping batches. A run row
    # is the permanent dedupe receipt; PgQueuer dedupes only active queue rows.
    inserted: dict[str, RunRequest] = {}
    store = PostgresRunStore("")
    for request in sorted(requests, key=lambda r: r.run_id):
        _entrypoint(request.queue)
        if not request.run_id or not request.session_id:
            raise ValueError("run_id and session_id must be non-empty")
        row = await store.create_pending_run(
            conn,
            request.session_id,
            id=request.run_id,
            meta={**request.meta, **_request_refs(request)},
        )
        if row is not None:
            inserted[request.run_id] = request
    if inserted:
        values = list(inserted.values())
        await Queries.from_asyncpg_connection(conn).enqueue(
            [_entrypoint(r.queue) for r in values],
            [
                json.dumps(
                    {
                        "run_id": r.run_id,
                        "session_id": r.session_id,
                        "prompt": r.prompt,
                        "queue": r.queue,
                        "traceparent": traceparent,
                        **_request_refs(r),
                    },
                    allow_nan=False,
                ).encode()
                for r in values
            ],
            priority=[0] * len(values),
            dedupe_key=[f"run:{r.run_id}" for r in values],
            on_conflict="skip",
        )
    return [r.run_id for r in requests]


async def enqueue_runs(pool: Any, requests: Sequence[RunRequest]) -> list[str]:
    """Create pending runs and their jobs atomically, with one batched queue insert.

    Existing run IDs are permanent no-ops, even after their job has been acked.
    Call :func:`install` first. The pool remains owned by the host.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            return await _enqueue(conn, requests, _traceparent())


async def enqueue_run(
    pool: Any,
    session_id: str,
    prompt: str,
    *,
    run_id: str | None = None,
    queue: str | None = None,
    meta: dict[str, object] | None = None,
    profile: Profile | None = None,
    principal: Principal | None = None,
) -> str:
    """Create one pending run and one PgQueuer job in the same transaction.

    Args:
        pool: Host-owned pool shared by run storage and the queue.
        session_id: Conversation receiving this run.
        prompt: User input appended when the worker starts the run.
        run_id: Optional idempotency key for repeated submissions.
        queue: Named fleet queue, or the default queue when omitted.
        meta: Host routing metadata retained on the run.
        profile: Released definition whose identity reference is persisted.
        principal: Authorization identity restored by the worker.

    Returns:
        The assigned run ID, including when it was already enqueued.
    """
    request = RunRequest(
        session_id,
        prompt,
        run_id or str(uuid4()),
        queue,
        meta or {},
        profile,
        principal,
    )
    return (await enqueue_runs(pool, [request]))[0]
