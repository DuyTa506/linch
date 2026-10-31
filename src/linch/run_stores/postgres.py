"""PostgreSQL-backed durable run store.

The backend is deliberately kept in a separate package so importing Linch does
not import ``asyncpg``.  Install it with ``pip install 'linch[postgres]'``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from typing import Any, NoReturn
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
    _json_safe,
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
    lease_expires_at DOUBLE PRECISION,
    last_event_seq BIGINT NOT NULL DEFAULT 0
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
        self._lease_owners: dict[str, str] = {}
        self._lease_ttls: dict[str, float] = {}

    async def _ensure(self) -> Any:
        return await ensure_pool(
            self,
            _SCHEMA,
            _import_asyncpg,
            schema_key="runs",
            tables=("runs", "run_events", "model_input_snapshots"),
            required_columns={"runs": ("last_event_seq",)},
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
            if row is not None:
                return _row_record(row)

            # Adoption of a caller-created pending row is the conflict path.
            # It needs a row lock while validating session/contract ownership;
            # normal run creation above remains one autocommit statement.
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT id, session_id, status, created_at, updated_at, checkpoint, meta, "
                    "lease_epoch, lease_owner, lease_expires_at "
                    "FROM runs WHERE id = $1 FOR UPDATE",
                    rid,
                )
                if row is None:
                    raise KeyError(f"run not found: {rid}")
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

    def _assert_fence(self, run_id: str, row: Any) -> None:
        held = self._held_leases.get(run_id)
        owner = self._lease_owners.get(run_id)
        if held is not None and (
            int(row["lease_epoch"] or 0) != held or row["lease_owner"] != owner
        ):
            raise LeaseLostError(
                f"lease lost for run {run_id}: held epoch {held}, "
                f"stored epoch {int(row['lease_epoch'] or 0)}"
            )

    def _fence_values(self, run_id: str) -> tuple[str | None, int]:
        epoch = self._held_leases.get(run_id)
        if epoch is None:
            return None, 0
        owner = self._lease_owners.get(run_id)
        if owner is None:
            raise LeaseLostError(f"lease lost for run {run_id}")
        return owner, epoch

    async def _classify_write_failure(self, conn: Any, run_id: str) -> NoReturn:
        row = await conn.fetchrow(
            "SELECT id, lease_owner, lease_epoch FROM runs WHERE id = $1",
            run_id,
        )
        if row is None:
            raise KeyError(f"run not found: {run_id}")
        held = self._held_leases.get(run_id)
        if held is not None:
            raise LeaseLostError(
                f"lease lost for run {run_id}: held epoch {held}, "
                f"stored epoch {int(row['lease_epoch'] or 0)}"
            )
        raise KeyError(f"run write was not applied: {run_id}")

    async def save_checkpoint(
        self,
        run_id: str,
        checkpoint: RunCheckpoint,
        *,
        status: str = "running",
    ) -> RunRecord:
        record, _ = await self.save_checkpoint_with_events(run_id, checkpoint, (), status=status)
        return record

    async def append_event(self, run_id: str, event: Event) -> int:
        return (await self.append_events(run_id, [event]))[0]

    async def _write_event_rows(
        self,
        run_id: str,
        args: tuple[object, ...],
        *,
        update: str,
        event_arg: str,
        fence: str,
        returning: str,
        result: str,
    ) -> tuple[Any, list[int]]:
        sql = f"""
            WITH updated AS (
                UPDATE runs SET {update} WHERE id = $1 AND ({fence})
                RETURNING id, {returning}
            ), inserted AS (
                INSERT INTO run_events (run_id, seq, appended_at, event)
                SELECT updated.id, updated.first_seq + ordinality, $2, event_json
                FROM updated CROSS JOIN unnest({event_arg}::text[])
                    WITH ORDINALITY AS events(event_json, ordinality)
                RETURNING seq
            )
            SELECT {result}COALESCE((SELECT array_agg(seq ORDER BY seq) FROM inserted),
                                    ARRAY[]::BIGINT[]) AS allocated_seqs
            FROM updated
        """
        pool = await self._ensure()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(sql, *args)
            if row is None:
                await self._classify_write_failure(conn, run_id)
        return row, [int(seq) for seq in row["allocated_seqs"]]

    async def save_checkpoint_with_events(
        self,
        run_id: str,
        checkpoint: RunCheckpoint,
        events: Sequence[Event],
        *,
        status: str = "running",
    ) -> tuple[RunRecord, list[int]]:
        """Commit a phase's events and checkpoint in one fenced statement."""
        encoded = [json.dumps(_json_safe_stored_event(event), allow_nan=False) for event in events]
        payload = json.dumps(checkpoint_to_dict(checkpoint), allow_nan=False)
        owner, epoch = self._fence_values(run_id)
        row, seqs = await self._write_event_rows(
            run_id,
            (run_id, now_iso(), status, payload, len(encoded), encoded, owner, epoch),
            update=(
                "updated_at = $2, status = $3, checkpoint = $4, "
                "last_event_seq = last_event_seq + $5"
            ),
            event_arg="$6",
            fence="$7::text IS NULL OR (lease_owner = $7 AND COALESCE(lease_epoch, 0) = $8)",
            returning=(
                "session_id, status, created_at, updated_at, checkpoint, meta, "
                "last_event_seq - $5 AS first_seq"
            ),
            result=(
                "updated.id, updated.session_id, updated.status, updated.created_at, "
                "updated.updated_at, updated.checkpoint, updated.meta,"
            ),
        )
        return _row_record(row), seqs

    async def append_events(self, run_id: str, events: list[Event]) -> list[int]:
        if not events:
            return []
        encoded = [json.dumps(_json_safe_stored_event(event), allow_nan=False) for event in events]
        owner, epoch = self._fence_values(run_id)
        _, seqs = await self._write_event_rows(
            run_id,
            (run_id, now_iso(), len(encoded), encoded, owner, epoch),
            update="updated_at = $2, last_event_seq = last_event_seq + $3",
            event_arg="$4",
            fence="$5::text IS NULL OR (lease_owner = $5 AND COALESCE(lease_epoch, 0) = $6)",
            returning="last_event_seq - $3 AS first_seq",
            result="",
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
        *,
        events: Sequence[Event] = (),
    ) -> tuple[RunRecord, list[int]]:
        checkpoint_json = None
        if checkpoint is not None:
            checkpoint.phase = "failed"
            checkpoint_json = json.dumps(checkpoint_to_dict(checkpoint), allow_nan=False)
        error_json = (
            json.dumps(_json_safe(error, strict=False), allow_nan=False)
            if error is not None
            else None
        )
        encoded = [json.dumps(_json_safe_stored_event(event), allow_nan=False) for event in events]
        owner, epoch = self._fence_values(run_id)
        row, seqs = await self._write_event_rows(
            run_id,
            (run_id, now_iso(), checkpoint_json, error_json, len(encoded), encoded, owner, epoch),
            update="""
                updated_at = $2, status = 'failed', checkpoint = COALESCE($3, checkpoint),
                last_event_seq = last_event_seq + $5,
                meta = CASE
                    WHEN $4::jsonb IS NULL THEN meta
                    WHEN jsonb_typeof(meta::jsonb -> 'errors') = 'array'
                     AND NOT EXISTS (
                         SELECT 1 FROM jsonb_array_elements(meta::jsonb -> 'errors') AS error_item
                         WHERE jsonb_typeof(error_item) <> 'object'
                     )
                    THEN jsonb_set(meta::jsonb, '{errors}',
                        (meta::jsonb -> 'errors') || jsonb_build_array($4::jsonb))::text
                    ELSE jsonb_set(meta::jsonb, '{errors}', jsonb_build_array($4::jsonb))::text
                END
            """,
            event_arg="$6",
            fence="$7::text IS NULL OR (lease_owner = $7 AND COALESCE(lease_epoch, 0) = $8)",
            returning=(
                "session_id, status, created_at, updated_at, checkpoint, meta, "
                "last_event_seq - $5 AS first_seq"
            ),
            result=(
                "updated.id, updated.session_id, updated.status, updated.created_at, "
                "updated.updated_at, updated.checkpoint, updated.meta,"
            ),
        )
        return _row_record(row), seqs

    async def claim(self, run_id: str, owner: str, ttl_s: float) -> RunLease:
        ttl_s = _validate_lease_ttl(ttl_s)
        pool = await self._ensure()
        async with pool.acquire() as conn:
            # The conditional UPDATE is the ownership decision.  A live lease
            # by the same owner is idempotently refreshed; any other live owner
            # leaves the row untouched and returns no row.
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
        self._lease_owners[run_id] = owner
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
            self._lease_owners.pop(lease.run_id, None)
            self._lease_ttls.pop(lease.run_id, None)

    async def save(
        self,
        run_id: str,
        provider_attempt: int,
        request: ProviderRequest,
    ) -> ModelInputSnapshot:
        snapshot = create_model_input_snapshot(run_id, provider_attempt, request)
        pool = await self._ensure()
        owner, epoch = self._fence_values(run_id)
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                WITH fenced AS (
                    UPDATE runs
                    SET updated_at = updated_at
                    WHERE id = $2
                      AND ($8::text IS NULL OR
                           (lease_owner = $8 AND COALESCE(lease_epoch, 0) = $9))
                    RETURNING id
                )
                INSERT INTO model_input_snapshots
                    (id, run_id, provider_attempt, codec_version, request_json,
                     integrity_hash, created_at)
                SELECT $1, $2, $3, $4, $5, $6, $7 FROM fenced
                RETURNING id
                """,
                snapshot.id,
                snapshot.run_id,
                snapshot.provider_attempt,
                snapshot.codec_version,
                snapshot.request_json,
                snapshot.integrity_hash,
                snapshot.created_at,
                owner,
                epoch,
            )
            if row is None:
                await self._classify_write_failure(conn, run_id)
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

    async def _delete_snapshots(
        self,
        run_id: str,
        values: tuple[object, ...],
        predicate: str,
    ) -> int:
        pool = await self._ensure()
        owner, epoch = self._fence_values(run_id)
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                WITH fenced AS (
                    UPDATE runs
                    SET updated_at = updated_at
                    WHERE id = $1
                      AND ($3::text IS NULL OR (lease_owner = $3 AND
                           COALESCE(lease_epoch, 0) = $4))
                    RETURNING id
                ), deleted AS (
                    DELETE FROM model_input_snapshots AS snapshots
                    USING fenced
                    WHERE snapshots.run_id = $1 AND {predicate}
                    RETURNING snapshots.id
                )
                SELECT (SELECT id FROM fenced) AS fenced_id,
                       (SELECT count(*) FROM deleted) AS deleted_count
                """,
                run_id,
                *values,
                owner,
                epoch,
            )
            if row is None or row["fenced_id"] is None:
                await self._classify_write_failure(conn, run_id)
        return int(row["deleted_count"] or 0)

    async def delete(self, snapshot_id: str, *, run_id: str) -> None:
        await self._delete_snapshots(run_id, (snapshot_id,), "snapshots.id = $2")

    async def prune(self, run_id: str, keep_ids: Sequence[str] = ()) -> int:
        return await self._delete_snapshots(
            run_id,
            (list(str(item) for item in keep_ids),),
            "snapshots.id <> ALL($2::text[])",
        )

    async def close(self) -> None:
        await close_pool(self, reset_initialized=True)

    async def __aenter__(self) -> PostgresRunStore:
        await self._ensure()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()
