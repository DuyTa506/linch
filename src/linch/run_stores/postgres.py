"""PostgreSQL-backed durable run store.

The backend is deliberately kept in a separate package so importing Linch does
not import ``asyncpg``.  Install it with ``pip install 'linch[postgres]'``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from typing import Any
from uuid import uuid4

from ..events import Event, _stored_event_from_dict
from ..run_store import (
    RUN_CONTRACT_META_KEY,
    LeaseLostError,
    ModelInputSnapshot,
    RunCheckpoint,
    RunLease,
    RunLeaseConflictError,
    RunRecord,
    StoredRunEvent,
    _append_run_error,
    _json_safe_stored_event,
    _safe_meta,
    _validate_lease_ttl,
    _validate_model_input_snapshot,
    checkpoint_from_dict,
    checkpoint_to_dict,
    create_model_input_snapshot,
    ensure_run_contract_compatible,
    run_contract_from_meta,
)
from ..sessions.memory import now_iso
from ..storage._pg import _import_asyncpg, close_pool, ensure_pool
from ..types import ProviderRequest

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    checkpoint TEXT,
    meta TEXT NOT NULL DEFAULT '{}',
    lease_owner TEXT,
    lease_epoch BIGINT NOT NULL DEFAULT 0,
    lease_expires_at DOUBLE PRECISION
);

CREATE TABLE IF NOT EXISTS run_events (
    run_id TEXT NOT NULL,
    seq BIGINT NOT NULL,
    appended_at TEXT NOT NULL,
    event TEXT NOT NULL,
    PRIMARY KEY (run_id, seq)
);

CREATE TABLE IF NOT EXISTS model_input_snapshots (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    provider_attempt INTEGER NOT NULL,
    codec_version INTEGER NOT NULL,
    request_json TEXT NOT NULL,
    integrity_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS model_input_snapshots_run_id_idx
    ON model_input_snapshots (run_id);
"""
_SCHEMA_LOCK_KEY = 0x4C494E434852554E


def _row_record(row: Any) -> RunRecord:
    checkpoint_raw = row["checkpoint"]
    meta_raw = row["meta"]
    checkpoint = None
    if checkpoint_raw:
        checkpoint = checkpoint_from_dict(
            json.loads(checkpoint_raw) if isinstance(checkpoint_raw, str) else checkpoint_raw
        )
    meta_value = json.loads(meta_raw) if isinstance(meta_raw, str) else (meta_raw or {})
    return RunRecord(
        id=str(row["id"]),
        session_id=str(row["session_id"]),
        status=row["status"],
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        checkpoint=checkpoint,
        meta=_safe_meta(meta_value),
    )


def _lease_from_row(row: Any, run_id: str, owner: str) -> RunLease:
    return RunLease(
        run_id=run_id,
        owner=owner,
        epoch=int(row["lease_epoch"] or 0),
        expires_at=float(row["lease_expires_at"]),
    )


class PostgresRunStore:
    """Async PostgreSQL implementation of the durable :class:`RunStore` API."""

    def __init__(
        self,
        dsn: str,
        *,
        pool: Any = None,
        min_size: int = 1,
        max_size: int = 10,
    ) -> None:
        _import_asyncpg()
        self._dsn = dsn
        self._pool: Any = pool
        self._min_size = min_size
        self._max_size = max_size
        self._initialized = False
        self._init_lock = asyncio.Lock()
        self._held_leases: dict[str, int] = {}
        self._lease_ttls: dict[str, float] = {}

    async def _ensure(self) -> Any:
        return await ensure_pool(
            self,
            _SCHEMA,
            _import_asyncpg,
            schema_key="runs",
            tables=("runs", "run_events", "model_input_snapshots"),
            advisory_lock_key=_SCHEMA_LOCK_KEY,
            allow_missing_pool_on_error=True,
        )

    async def create_run(
        self,
        session_id: str,
        *,
        id: str | None = None,
        meta: dict[str, object] | None = None,
    ) -> RunRecord:
        pool = await self._ensure()
        rid = id or str(uuid4())
        safe_meta = _safe_meta(meta)
        async with pool.acquire() as conn:
            async with conn.transaction():
                ts = now_iso()
                row = await conn.fetchrow(
                    """
                    INSERT INTO runs
                        (id, session_id, status, created_at, updated_at, checkpoint, meta)
                    VALUES ($1, $2, 'running', $3, $3, NULL, $4)
                    ON CONFLICT (id) DO NOTHING
                    RETURNING id, session_id, status, created_at, updated_at, checkpoint, meta
                    """,
                    rid,
                    session_id,
                    ts,
                    json.dumps(safe_meta, allow_nan=False),
                )
                if row is None:
                    row = await conn.fetchrow(
                        "SELECT id, session_id, status, created_at, updated_at, checkpoint, meta, "
                        "lease_epoch, lease_owner, lease_expires_at "
                        "FROM runs WHERE id = $1 FOR UPDATE",
                        rid,
                    )
                else:
                    return _row_record(row)
                assert row is not None
                if row["status"] != "pending":
                    raise ValueError(f"run {rid} already exists")
                if str(row["session_id"]) != session_id:
                    raise ValueError(f"run {rid} belongs to session {row['session_id']}")
                self._assert_fence(rid, row)
                stored_meta = _safe_meta(
                    json.loads(row["meta"]) if isinstance(row["meta"], str) else row["meta"]
                )
                stored_contract = stored_meta.get(RUN_CONTRACT_META_KEY)
                requested_contract = safe_meta.get(RUN_CONTRACT_META_KEY)
                if stored_contract is not None and requested_contract is not None:
                    ensure_run_contract_compatible(
                        run_contract_from_meta(stored_meta),
                        run_contract_from_meta(safe_meta),
                    )
                merged_meta = dict(stored_meta)
                merged_meta.update(safe_meta)
                if stored_contract is not None:
                    merged_meta[RUN_CONTRACT_META_KEY] = stored_contract
                row = await conn.fetchrow(
                    """
                    UPDATE runs
                    SET status = 'running', updated_at = $1, meta = $2
                    WHERE id = $3
                    RETURNING id, session_id, status, created_at, updated_at, checkpoint, meta
                    """,
                    ts,
                    json.dumps(merged_meta, allow_nan=False),
                    rid,
                )
                assert row is not None
                return _row_record(row)

    async def create_pending_run(
        self,
        conn: Any,
        session_id: str,
        *,
        id: str,
        meta: dict[str, object] | None = None,
    ) -> RunRecord | None:
        """Insert a pending run using a caller-owned asyncpg transaction.

        The caller must initialize this store before opening the transaction.
        A same-session existing row is left untouched and reported as ``None``;
        an ID owned by another session is rejected.
        """

        safe_meta = _safe_meta(meta)
        ts = now_iso()
        row = await conn.fetchrow(
            """
            INSERT INTO runs
                (id, session_id, status, created_at, updated_at, checkpoint, meta)
            VALUES ($1, $2, 'pending', $3, $3, NULL, $4)
            ON CONFLICT (id) DO NOTHING
            RETURNING id, session_id, status, created_at, updated_at, checkpoint, meta
            """,
            id,
            session_id,
            ts,
            json.dumps(safe_meta, allow_nan=False),
        )
        if row is not None:
            return _row_record(row)
        existing = await conn.fetchrow(
            "SELECT id, session_id, status, created_at, updated_at, checkpoint, meta "
            "FROM runs WHERE id = $1 FOR UPDATE",
            id,
        )
        if existing is None:
            raise KeyError(f"run not found: {id}")
        if str(existing["session_id"]) != session_id:
            raise ValueError(f"run {id} belongs to session {existing['session_id']}")
        return None

    async def load_run(self, run_id: str) -> RunRecord | None:
        pool = await self._ensure()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, session_id, status, created_at, updated_at, checkpoint, meta "
                "FROM runs WHERE id = $1",
                run_id,
            )
        return _row_record(row) if row is not None else None

    async def _locked_run(self, conn: Any, run_id: str) -> Any:
        row = await conn.fetchrow(
            """
            SELECT id, session_id, status, created_at, updated_at, checkpoint, meta,
                   COALESCE(lease_epoch, 0) AS lease_epoch,
                   lease_owner, lease_expires_at
            FROM runs WHERE id = $1 FOR UPDATE
            """,
            run_id,
        )
        if row is None:
            raise KeyError(f"run not found: {run_id}")
        return row

    def _assert_fence(self, run_id: str, row: Any) -> None:
        held = self._held_leases.get(run_id)
        if held is not None and int(row["lease_epoch"] or 0) != held:
            raise LeaseLostError(
                f"lease lost for run {run_id}: held epoch {held}, "
                f"stored epoch {int(row['lease_epoch'] or 0)}"
            )

    async def save_checkpoint(
        self,
        run_id: str,
        checkpoint: RunCheckpoint,
        *,
        status: str = "running",
    ) -> RunRecord:
        pool = await self._ensure()
        payload = json.dumps(checkpoint_to_dict(checkpoint), allow_nan=False)
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await self._locked_run(conn, run_id)
                self._assert_fence(run_id, row)
                ts = now_iso()
                updated = await conn.fetchrow(
                    """
                    UPDATE runs SET updated_at = $1, status = $2, checkpoint = $3
                    WHERE id = $4
                    RETURNING id, session_id, status, created_at, updated_at, checkpoint, meta
                    """,
                    ts,
                    status,
                    payload,
                    run_id,
                )
                assert updated is not None
                return _row_record(updated)

    async def append_event(self, run_id: str, event: Event) -> int:
        return (await self.append_events(run_id, [event]))[0]

    async def append_events(self, run_id: str, events: list[Event]) -> list[int]:
        if not events:
            return []
        pool = await self._ensure()
        encoded = [json.dumps(_json_safe_stored_event(event), allow_nan=False) for event in events]
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await self._locked_run(conn, run_id)
                self._assert_fence(run_id, row)
                base = int(
                    await conn.fetchval(
                        "SELECT COALESCE(MAX(seq), 0) FROM run_events WHERE run_id = $1",
                        run_id,
                    )
                    or 0
                )
                seqs = [base + index for index in range(1, len(events) + 1)]
                ts = now_iso()
                await conn.executemany(
                    "INSERT INTO run_events (run_id, seq, appended_at, event) "
                    "VALUES ($1, $2, $3, $4)",
                    [
                        (run_id, seq, ts, event_json)
                        for seq, event_json in zip(seqs, encoded, strict=True)
                    ],
                )
                return seqs

    async def load_events(self, run_id: str, *, after_seq: int = 0) -> list[StoredRunEvent]:
        pool = await self._ensure()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT seq, appended_at, event FROM run_events
                WHERE run_id = $1 AND seq > $2 ORDER BY seq
                """,
                run_id,
                after_seq,
            )
        result: list[StoredRunEvent] = []
        for row in rows:
            raw = row["event"]
            result.append(
                StoredRunEvent(
                    seq=int(row["seq"]),
                    appended_at=str(row["appended_at"]),
                    event=_stored_event_from_dict(json.loads(raw) if isinstance(raw, str) else raw),
                )
            )
        return result

    async def mark_completed(self, run_id: str, checkpoint: RunCheckpoint) -> RunRecord:
        checkpoint.phase = "completed"
        return await self.save_checkpoint(run_id, checkpoint, status="completed")

    async def mark_failed(
        self,
        run_id: str,
        checkpoint: RunCheckpoint | None = None,
        error: dict[str, object] | None = None,
    ) -> RunRecord:
        pool = await self._ensure()
        checkpoint_json = None
        if checkpoint is not None:
            checkpoint.phase = "failed"
            checkpoint_json = json.dumps(checkpoint_to_dict(checkpoint), allow_nan=False)
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await self._locked_run(conn, run_id)
                self._assert_fence(run_id, row)
                meta_raw = row["meta"]
                meta = _safe_meta(json.loads(meta_raw) if isinstance(meta_raw, str) else meta_raw)
                if error is not None:
                    _append_run_error(meta, error)
                ts = now_iso()
                updated = await conn.fetchrow(
                    """
                    UPDATE runs
                    SET updated_at = $1, status = 'failed',
                        checkpoint = COALESCE($2, checkpoint), meta = $3
                    WHERE id = $4
                    RETURNING id, session_id, status, created_at, updated_at, checkpoint, meta
                    """,
                    ts,
                    checkpoint_json,
                    json.dumps(meta, allow_nan=False),
                    run_id,
                )
                assert updated is not None
                return _row_record(updated)

    async def claim(self, run_id: str, owner: str, ttl_s: float) -> RunLease:
        ttl_s = _validate_lease_ttl(ttl_s)
        pool = await self._ensure()
        async with pool.acquire() as conn:
            async with conn.transaction():
                # The conditional UPDATE is the ownership decision.  A live
                # lease by the same owner is idempotently refreshed; any other
                # live owner leaves the row untouched and returns no row.
                row = await conn.fetchrow(
                    """
                    UPDATE runs
                    SET lease_owner = $2,
                        lease_epoch = CASE
                            WHEN lease_owner = $2 AND lease_expires_at >
                                EXTRACT(EPOCH FROM clock_timestamp())::double precision
                                THEN COALESCE(lease_epoch, 0)
                            ELSE COALESCE(lease_epoch, 0) + 1
                        END,
                        lease_expires_at =
                            EXTRACT(EPOCH FROM clock_timestamp())::double precision
                            + $3::double precision
                    WHERE id = $1
                      AND (
                          lease_owner IS NULL
                          OR lease_expires_at IS NULL
                          OR lease_expires_at <=
                              EXTRACT(EPOCH FROM clock_timestamp())::double precision
                          OR lease_owner = $2
                      )
                    RETURNING COALESCE(lease_epoch, 0) AS lease_epoch, lease_expires_at
                    """,
                    run_id,
                    owner,
                    ttl_s,
                )
                if row is None:
                    exists = await conn.fetchval("SELECT 1 FROM runs WHERE id = $1", run_id)
                    if exists is None:
                        raise KeyError(f"run not found: {run_id}")
                    raise RunLeaseConflictError(f"run {run_id} is leased by another owner")
                lease = _lease_from_row(row, run_id, owner)
        # Publish ownership after all awaited context exits, so transaction
        # failure or cancellation cannot record an uncommitted epoch.
        self._held_leases[run_id] = lease.epoch
        self._lease_ttls[run_id] = float(ttl_s)
        return lease

    async def renew(self, lease: RunLease) -> RunLease:
        held = self._held_leases.get(lease.run_id)
        if held != lease.epoch:
            raise LeaseLostError(f"lease lost for run {lease.run_id}")
        ttl_s = self._lease_ttls.get(lease.run_id)
        if ttl_s is None:
            raise LeaseLostError(f"lease lost for run {lease.run_id}")
        pool = await self._ensure()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE runs
                SET lease_expires_at =
                    EXTRACT(EPOCH FROM clock_timestamp())::double precision
                    + $4::double precision
                WHERE id = $1 AND lease_owner = $2 AND COALESCE(lease_epoch, 0) = $3
                  AND lease_expires_at >
                      EXTRACT(EPOCH FROM clock_timestamp())::double precision
                RETURNING COALESCE(lease_epoch, 0) AS lease_epoch, lease_expires_at
                """,
                lease.run_id,
                lease.owner,
                lease.epoch,
                ttl_s,
            )
        if row is None:
            raise LeaseLostError(f"lease lost for run {lease.run_id}")
        return _lease_from_row(row, lease.run_id, lease.owner)

    async def release(self, lease: RunLease) -> None:
        try:
            held = self._held_leases.get(lease.run_id)
            if held != lease.epoch:
                raise LeaseLostError(f"lease lost for run {lease.run_id}")
            pool = await self._ensure()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    UPDATE runs
                    SET lease_owner = NULL,
                        lease_expires_at = NULL,
                        lease_epoch = COALESCE(lease_epoch, 0) + 1
                    WHERE id = $1 AND lease_owner = $2 AND COALESCE(lease_epoch, 0) = $3
                    RETURNING id
                    """,
                    lease.run_id,
                    lease.owner,
                    lease.epoch,
                )
            if row is None:
                raise LeaseLostError(f"lease lost for run {lease.run_id}")
        finally:
            self._held_leases.pop(lease.run_id, None)
            self._lease_ttls.pop(lease.run_id, None)

    async def save(
        self,
        run_id: str,
        provider_attempt: int,
        request: ProviderRequest,
    ) -> ModelInputSnapshot:
        snapshot = create_model_input_snapshot(run_id, provider_attempt, request)
        pool = await self._ensure()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await self._locked_run(conn, run_id)
                self._assert_fence(run_id, row)
                await conn.execute(
                    """
                    INSERT INTO model_input_snapshots
                        (id, run_id, provider_attempt, codec_version, request_json,
                         integrity_hash, created_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                    """,
                    snapshot.id,
                    snapshot.run_id,
                    snapshot.provider_attempt,
                    snapshot.codec_version,
                    snapshot.request_json,
                    snapshot.integrity_hash,
                    snapshot.created_at,
                )
        return snapshot

    async def load(self, snapshot_id: str) -> ModelInputSnapshot | None:
        pool = await self._ensure()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, run_id, provider_attempt, codec_version, request_json,
                       integrity_hash, created_at
                FROM model_input_snapshots WHERE id = $1
                """,
                snapshot_id,
            )
        if row is None:
            return None
        snapshot = ModelInputSnapshot(
            id=str(row["id"]),
            run_id=str(row["run_id"]),
            provider_attempt=int(row["provider_attempt"]),
            codec_version=int(row["codec_version"]),
            request_json=str(row["request_json"]),
            integrity_hash=str(row["integrity_hash"]),
            created_at=str(row["created_at"]),
        )
        return _validate_model_input_snapshot(snapshot)

    async def delete(self, snapshot_id: str) -> None:
        pool = await self._ensure()
        async with pool.acquire() as conn:
            async with conn.transaction():
                snapshot = await conn.fetchrow(
                    "SELECT run_id FROM model_input_snapshots WHERE id = $1",
                    snapshot_id,
                )
                if snapshot is None:
                    return
                run = await self._locked_run(conn, str(snapshot["run_id"]))
                self._assert_fence(str(snapshot["run_id"]), run)
                await conn.execute("DELETE FROM model_input_snapshots WHERE id = $1", snapshot_id)

    async def prune(self, run_id: str, keep_ids: Sequence[str] = ()) -> int:
        pool = await self._ensure()
        keep = tuple(str(item) for item in keep_ids)
        async with pool.acquire() as conn:
            async with conn.transaction():
                run = await self._locked_run(conn, run_id)
                self._assert_fence(run_id, run)
                if keep:
                    placeholders = ", ".join(f"${index}" for index in range(2, len(keep) + 2))
                    result = await conn.execute(
                        f"DELETE FROM model_input_snapshots WHERE run_id = $1 "
                        f"AND id NOT IN ({placeholders})",
                        run_id,
                        *keep,
                    )
                else:
                    result = await conn.execute(
                        "DELETE FROM model_input_snapshots WHERE run_id = $1", run_id
                    )
        try:
            return int(str(result).split()[-1])
        except (ValueError, IndexError):
            return 0

    async def close(self) -> None:
        await close_pool(self, reset_initialized=True)

    async def __aenter__(self) -> PostgresRunStore:
        await self._ensure()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()
