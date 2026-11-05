"""PostgreSQL-backed durable run store.

The backend is deliberately kept in a separate package so importing Linch does
not import ``asyncpg``.  Install it with ``pip install 'linch[postgres]'``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from contextlib import nullcontext
from typing import Any, NoReturn
from uuid import uuid4

from .._json import dumps
from ..errors import ConfigError
from ..events import Event, _stored_event_from_dict
from ..run_store import (
    RUN_CONTRACT_META_KEY,
    LeaseLostError,
    ModelInputSnapshot,
    RunCheckpoint,
    RunLease,
    RunLeaseConflictError,
    RunRecord,
    SessionBusyError,
    StoredRunEvent,
    _append_run_error,
    _json_safe,
    _json_safe_stored_event,
    _lease_kwargs,
    _safe_meta,
    _validate_lease_run,
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

CREATE UNIQUE INDEX IF NOT EXISTS runs_one_active_per_session_idx ON runs (session_id)
    WHERE status IN ('pending', 'running', 'waiting_permission', 'suspended');

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


def _lease_from_row(row: Any, run_id: str, owner: str, ttl_s: float) -> RunLease:
    return RunLease(
        run_id=run_id,
        owner=owner,
        epoch=int(row["lease_epoch"] or 0),
        expires_at=float(row["lease_expires_at"]),
        ttl_s=ttl_s,
    )


class PostgresRunStore:
    """Persist runs, checkpoints, events, leases, and snapshots in PostgreSQL.

    The optional ``asyncpg`` dependency is loaded when the store is
    constructed, while the connection pool and version-3 schema are initialized
    lazily on first use. Writes that run under a claimed lease are fenced by
    the stored owner and epoch.

    Args:
        dsn: PostgreSQL connection string used when a pool is created.
        pool: Optional host-owned asyncpg pool; the store never closes it.
        min_size: Minimum size for a store-created pool.
        max_size: Maximum size for a store-created pool.
    """

    def __init__(
        self,
        dsn: str,
        *,
        pool: Any = None,
        min_size: int = 1,
        max_size: int = 10,
    ) -> None:
        """Configure a PostgreSQL-backed run store.

        Args:
            dsn: PostgreSQL connection string used when a pool is created.
            pool: Optional host-owned asyncpg pool; the store never closes it.
            min_size: Minimum size for a store-created pool.
            max_size: Maximum size for a store-created pool.

        Raises:
            ModuleNotFoundError: If the optional ``asyncpg`` dependency is not installed.
        """

        _import_asyncpg()
        self._dsn = dsn
        self._pool: Any = pool
        self._owns_pool = pool is None
        self._min_size = min_size
        self._max_size = max_size
        self._initialized = False
        self._init_lock = asyncio.Lock()

    async def _ensure(self) -> Any:
        try:
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
        except _import_asyncpg().UniqueViolationError as exc:
            if exc.constraint_name != "runs_one_active_per_session_idx":
                raise
            raise ConfigError(
                "Cannot install run admission index: duplicate nonterminal runs exist for a "
                "session; resume or mark_failed those runs before restarting."
            ) from exc

    async def _admit(
        self,
        conn: Any,
        sql: str,
        *args: object,
        session_id: str | None = None,
        run_id: str | None = None,
    ) -> Any:
        transactional = conn.is_in_transaction()
        for attempt in range(3):
            try:
                async with conn.transaction() if transactional else nullcontext():
                    return await conn.fetchrow(sql, *args)
            except _import_asyncpg().UniqueViolationError as exc:
                if exc.constraint_name != "runs_one_active_per_session_idx":
                    raise
                owner = await conn.fetchrow(
                    "SELECT id, session_id FROM runs WHERE session_id = COALESCE($1::text, "
                    "(SELECT session_id FROM runs WHERE id = $2)) AND "
                    "status IN ('pending', 'running', 'waiting_permission', 'suspended')",
                    session_id,
                    run_id,
                )
                if owner is not None:
                    if session_id is not None and str(owner["id"]) == run_id:
                        # A same-id race, not a busy session: the caller's locked
                        # path adopts a pending row or rejects the duplicate id.
                        return None
                    raise SessionBusyError(str(owner["session_id"]), str(owner["id"])) from exc
                if transactional or attempt == 2:
                    raise RuntimeError(
                        "Run admission conflict: the conflicting nonterminal run is not visible"
                    ) from exc
                # The competing run finished after rejection; admission can retry.

    async def create_run(
        self,
        session_id: str,
        *,
        id: str | None = None,
        meta: dict[str, object] | None = None,
        initial_checkpoint: RunCheckpoint | None = None,
        lease: RunLease | None = None,
    ) -> RunRecord:
        """Create a running row or atomically adopt a matching pending row.

        Args:
            session_id: Session that owns the run.
            id: Optional stable run identifier; a UUID is generated when omitted.
            meta: JSON-safe metadata to persist with the run.
            initial_checkpoint: Checkpoint to commit with the running row.
            lease: Explicit owner/epoch token for pending adoption; omit for an unleased creation.

        Returns:
            The detached running record.

        Raises:
            ValueError: If an existing row is not pending, belongs to another
                session, or has an incompatible run contract.
            RunContractMismatchError: If pending-run contract inputs differ.
            KeyError: If a conflicting row disappears before adoption completes.
            LeaseLostError: If a held lease was replaced before adoption completes.
        """

        pool = await self._ensure()
        rid = id or str(uuid4())
        safe_meta = _safe_meta(meta)
        payload = (
            dumps(checkpoint_to_dict(initial_checkpoint))
            if initial_checkpoint is not None
            else None
        )
        async with pool.acquire() as conn:
            if lease is None:
                ts = now_iso()
                row = await self._admit(
                    conn,
                    """
                    INSERT INTO runs
                        (id, session_id, status, created_at, updated_at, checkpoint, meta)
                    VALUES ($1, $2, 'running', $3, $3, $5, $4)
                    ON CONFLICT (id) DO NOTHING
                    RETURNING id, session_id, status, created_at, updated_at, checkpoint, meta
                    """,
                    rid,
                    session_id,
                    ts,
                    dumps(safe_meta),
                    payload,
                    session_id=session_id,
                    run_id=rid,
                )
                if row is not None:
                    return _row_record(row)
            else:
                _validate_lease_run(rid, lease)

            # A conflicting caller-created row is adopted under a lock.  An
            # explicit lease skips the INSERT fast path and must find its row.
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT id, session_id, status, created_at, updated_at, checkpoint, meta, "
                    "lease_epoch, lease_owner, lease_expires_at "
                    "FROM runs WHERE id = $1 FOR UPDATE",
                    rid,
                )
                if row is None:
                    if lease is not None:
                        raise LeaseLostError(f"lease lost for run {rid}")
                    raise KeyError(f"run not found: {rid}")
                self._assert_fence(rid, row, lease)
                if row["status"] != "pending":
                    raise ValueError(f"run {rid} already exists")
                if str(row["session_id"]) != session_id:
                    raise ValueError(f"run {rid} belongs to session {row['session_id']}")
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
                    SET status = 'running', updated_at = $1, meta = $2, checkpoint = $4
                    WHERE id = $3
                    RETURNING id, session_id, status, created_at, updated_at, checkpoint, meta
                    """,
                    now_iso(),
                    dumps(merged_meta),
                    rid,
                    payload,
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

        Args:
            conn: Asyncpg connection inside the caller's transaction.
            session_id: Session that owns the pending run.
            id: Stable run identifier used for queue deduplication.
            meta: JSON-safe metadata to persist with the pending row.

        Returns:
            The newly inserted pending record, or ``None`` when the same-session
            identifier already exists.

        Raises:
            ValueError: If the identifier belongs to another session.
            KeyError: If a conflicting row disappears before it can be inspected.
        """

        safe_meta = _safe_meta(meta)
        ts = now_iso()
        row = await self._admit(
            conn,
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
            dumps(safe_meta),
            session_id=session_id,
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
        """Load a detached run record from PostgreSQL.

        Args:
            run_id: Run identifier to look up.

        Returns:
            The detached record, or ``None`` when no such row exists.
        """

        pool = await self._ensure()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, session_id, status, created_at, updated_at, checkpoint, meta "
                "FROM runs WHERE id = $1",
                run_id,
            )
        return _row_record(row) if row is not None else None

    def _assert_fence(self, run_id: str, row: Any, lease: RunLease | None = None) -> None:
        _validate_lease_run(run_id, lease)
        if lease is not None and (
            lease.run_id != run_id
            or int(row["lease_epoch"] or 0) != lease.epoch
            or row["lease_owner"] != lease.owner
        ):
            raise LeaseLostError(
                f"lease lost for run {run_id}: expected epoch {lease.epoch}, "
                f"stored epoch {int(row['lease_epoch'] or 0)}"
            )

    def _fence_values(self, run_id: str, lease: RunLease | None = None) -> tuple[str | None, int]:
        _validate_lease_run(run_id, lease)
        if lease is None:
            return None, 0
        if lease.run_id != run_id:
            raise LeaseLostError(f"lease lost for run {run_id}")
        return lease.owner, lease.epoch

    async def _classify_write_failure(
        self, conn: Any, run_id: str, lease: RunLease | None = None
    ) -> NoReturn:
        row = await conn.fetchrow(
            "SELECT id, lease_owner, lease_epoch FROM runs WHERE id = $1",
            run_id,
        )
        if row is None:
            if lease is not None:
                raise LeaseLostError(f"lease lost for run {run_id}")
            raise KeyError(f"run not found: {run_id}")
        if lease is not None:
            raise LeaseLostError(
                f"lease lost for run {run_id}: expected epoch {lease.epoch}, "
                f"stored epoch {int(row['lease_epoch'] or 0)}"
            )
        raise KeyError(f"run write was not applied: {run_id}")

    async def save_checkpoint(
        self,
        run_id: str,
        checkpoint: RunCheckpoint,
        *,
        status: str = "running",
        lease: RunLease | None = None,
    ) -> RunRecord:
        """Persist a checkpoint and update the run status atomically.

        Args:
            run_id: Run whose checkpoint should be replaced.
            checkpoint: Resumable state to persist.
            status: Status to store alongside the checkpoint.
            lease: Explicit owner/epoch token; omit for an unleased mutation.

        Returns:
            The detached updated run record.

        Raises:
            KeyError: If the run does not exist.
            LeaseLostError: If the supplied lease token is stale or mismatched.
        """

        record, _ = await self.save_checkpoint_with_events(
            run_id,
            checkpoint,
            (),
            status=status,
            **_lease_kwargs(self.save_checkpoint_with_events, lease),
        )
        return record

    async def append_event(
        self, run_id: str, event: Event, *, lease: RunLease | None = None
    ) -> int:
        """Append one event in a fenced database statement.

        Args:
            run_id: Run whose event log should receive the event.
            event: Typed event to persist.
            lease: Explicit owner/epoch token; omit for an unleased mutation.

        Returns:
            The 1-based sequence number assigned to ``event``.

        Raises:
            KeyError: If the run does not exist.
            LeaseLostError: If the supplied lease token is stale or mismatched.
        """

        return (
            await self.append_events(
                run_id,
                [event],
                **_lease_kwargs(self.append_events, lease),
            )
        )[0]

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
        connection: Any = None,
        lease: RunLease | None = None,
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
        manager = (
            (await self._ensure()).acquire() if connection is None else nullcontext(connection)
        )
        async with manager as conn:
            row = await self._admit(conn, sql, *args, run_id=run_id)
            if row is None:
                await self._classify_write_failure(conn, run_id, lease)
        return row, [int(seq) for seq in row["allocated_seqs"]]

    async def save_checkpoint_with_events(
        self,
        run_id: str,
        checkpoint: RunCheckpoint,
        events: Sequence[Event],
        *,
        status: str = "running",
        lease: RunLease | None = None,
    ) -> tuple[RunRecord, list[int]]:
        """Commit a phase's events and checkpoint in one fenced statement.

        Args:
            run_id: Run whose state should be updated.
            checkpoint: Resumable state to persist.
            events: Events to append in the supplied order.
            status: Status to store alongside the checkpoint.
            lease: Explicit owner/epoch token; omit for an unleased mutation.

        Returns:
            The detached updated record and 1-based sequence numbers parallel to
            ``events``.

        Raises:
            KeyError: If the run does not exist.
            LeaseLostError: If the supplied lease token is stale or mismatched.
            TypeError: If checkpoint or event data cannot be encoded.
        """

        encoded = [dumps(_json_safe_stored_event(event)) for event in events]
        payload = dumps(checkpoint_to_dict(checkpoint))
        owner, epoch = self._fence_values(run_id, lease)
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
            lease=lease,
        )
        return _row_record(row), seqs

    async def append_events(
        self, run_id: str, events: list[Event], *, lease: RunLease | None = None
    ) -> list[int]:
        """Append an event batch atomically and return its assigned sequences.

        Args:
            run_id: Run whose event log should receive the batch.
            events: Typed events to persist in input order.
            lease: Explicit owner/epoch token; omit for an unleased mutation.

        Returns:
            Contiguous 1-based sequence numbers parallel to ``events``; an empty
            input returns an empty list; a supplied lease is still validated.

        Raises:
            KeyError: If a non-empty batch targets a missing run.
            LeaseLostError: If a non-empty batch uses a stale fencing lease.
            TypeError: If an event in a non-empty batch cannot be encoded.
        """

        if not events:
            if lease is not None:
                await self._assert_current_lease(run_id, lease)
            return []
        encoded = [dumps(_json_safe_stored_event(event)) for event in events]
        owner, epoch = self._fence_values(run_id, lease)
        _, seqs = await self._write_event_rows(
            run_id,
            (run_id, now_iso(), len(encoded), encoded, owner, epoch),
            update="updated_at = $2, last_event_seq = last_event_seq + $3",
            event_arg="$4",
            fence="$5::text IS NULL OR (lease_owner = $5 AND COALESCE(lease_epoch, 0) = $6)",
            returning="last_event_seq - $3 AS first_seq",
            result="",
            lease=lease,
        )
        return seqs

    async def load_events(self, run_id: str, *, after_seq: int = 0) -> list[StoredRunEvent]:
        """Load persisted events after an exclusive sequence watermark.

        Args:
            run_id: Run whose event log should be read.
            after_seq: Exclusive sequence watermark; zero reads from the beginning.

        Returns:
            Detached events in ascending sequence order.
        """

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

    async def _assert_current_lease(self, run_id: str, lease: RunLease) -> None:
        """Check an explicit token for no-op mutations before returning."""
        _validate_lease_run(run_id, lease)
        pool = await self._ensure()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT lease_owner, lease_epoch FROM runs WHERE id = $1", run_id
            )
        if row is None:
            raise LeaseLostError(f"lease lost for run {run_id}")
        self._assert_fence(
            run_id,
            {
                "lease_owner": row["lease_owner"],
                "lease_epoch": row["lease_epoch"],
            },
            lease,
        )

    async def mark_completed(
        self, run_id: str, checkpoint: RunCheckpoint, *, lease: RunLease | None = None
    ) -> RunRecord:
        """Persist a terminal completed status and checkpoint.

        Args:
            run_id: Run to complete.
            checkpoint: Final checkpoint whose phase is set to ``completed``.
            lease: Explicit owner/epoch token; omit for an unleased mutation.

        Returns:
            The detached completed record.

        Raises:
            KeyError: If the run does not exist.
            LeaseLostError: If the supplied lease token is stale or mismatched.
        """

        checkpoint.phase = "completed"
        return await self.save_checkpoint(
            run_id,
            checkpoint,
            status="completed",
            **_lease_kwargs(self.save_checkpoint, lease),
        )

    async def mark_failed(
        self,
        run_id: str,
        checkpoint: RunCheckpoint | None = None,
        error: dict[str, object] | None = None,
        *,
        events: Sequence[Event] = (),
        lease: RunLease | None = None,
    ) -> tuple[RunRecord, list[int]]:
        """Commit failed status, checkpoint, error metadata, and events together.

        Args:
            run_id: Run to mark failed.
            checkpoint: Optional final checkpoint whose phase becomes ``failed``.
            error: Optional JSON-safe error metadata appended to the run history.
            events: Terminal events to append with the failed state.
            lease: Explicit owner/epoch token; omit for an unleased mutation.

        Returns:
            The detached failed record and 1-based sequence numbers parallel to
            ``events``.

        Raises:
            KeyError: If the run does not exist.
            LeaseLostError: If the supplied lease token is stale or mismatched.
            TypeError: If checkpoint, error, or event data cannot be encoded.
        """

        checkpoint_json = None
        if checkpoint is not None:
            checkpoint.phase = "failed"
            checkpoint_json = dumps(checkpoint_to_dict(checkpoint))
        error_json = dumps(_json_safe(error, strict=False)) if error is not None else None
        encoded = [dumps(_json_safe_stored_event(event)) for event in events]
        owner, epoch = self._fence_values(run_id, lease)
        pool = await self._ensure()
        async with pool.acquire() as conn, conn.transaction():
            meta_json = None
            if error_json is not None:
                # PostgreSQL JSON field access decodes escapes and rejects NUL,
                # even in unrelated keys. Merge the locked TEXT value in Python.
                stored_meta = await conn.fetchval(
                    "SELECT meta FROM runs WHERE id = $1 FOR UPDATE", run_id
                )
                if stored_meta is None:
                    await self._classify_write_failure(conn, run_id, lease)
                meta = _safe_meta(json.loads(stored_meta))
                _append_run_error(meta, json.loads(error_json))
                meta_json = dumps(meta)
            row, seqs = await self._write_event_rows(
                run_id,
                (
                    run_id,
                    now_iso(),
                    checkpoint_json,
                    meta_json,
                    len(encoded),
                    encoded,
                    owner,
                    epoch,
                ),
                update="""
                    updated_at = $2, status = 'failed', checkpoint = COALESCE($3, checkpoint),
                    last_event_seq = last_event_seq + $5, meta = COALESCE($4, meta)
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
                connection=conn,
                lease=lease,
            )
        return _row_record(row), seqs

    async def claim(self, run_id: str, owner: str, ttl_s: float) -> RunLease:
        """Claim a run using one conditional PostgreSQL update.

        Args:
            run_id: Existing run to claim.
            owner: Unique worker or process identifier.
            ttl_s: Positive finite lease duration in seconds.

        Returns:
            An immutable lease containing the current fencing epoch and expiry.

        Raises:
            KeyError: If ``run_id`` does not identify a run.
            RunLeaseConflictError: If another owner holds a live lease.
            ValueError: If ``ttl_s`` is not positive and finite.
        """

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
            lease = _lease_from_row(row, run_id, owner, ttl_s)
        return lease

    async def renew(self, lease: RunLease) -> RunLease:
        """Renew a matching live lease without changing its epoch.

        Args:
            lease: Immutable owner, epoch, and renewal-duration token.

        Returns:
            The same lease identity with a refreshed expiry timestamp.

        Raises:
            LeaseLostError: If the supplied lease token is stale, expired, or no
                longer matches the stored owner and epoch.
        """

        _validate_lease_run(lease.run_id, lease)
        ttl_s = _validate_lease_ttl(lease.ttl_s)
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
        return _lease_from_row(row, lease.run_id, lease.owner, ttl_s)

    async def release(self, lease: RunLease) -> None:
        """Release a matching lease and advance the stored fencing epoch.

        Args:
            lease: Lease whose ownership should be relinquished.

        Raises:
            LeaseLostError: If another holder has already changed the owner or epoch.
        """

        _validate_lease_run(lease.run_id, lease)
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

    async def save(
        self,
        run_id: str,
        provider_attempt: int,
        request: ProviderRequest,
        *,
        lease: RunLease | None = None,
    ) -> ModelInputSnapshot:
        """Persist an exact provider-request snapshot under the run fence.

        Args:
            run_id: Run that owns the pending provider request.
            provider_attempt: Positive, 1-based provider dispatch attempt.
            request: Provider request to encode without its live abort signal.
            lease: Explicit owner/epoch token; omit for an unleased mutation.

        Returns:
            The immutable encoded snapshot.

        Raises:
            KeyError: If the run does not exist.
            LeaseLostError: If the supplied lease token is stale or mismatched.
            ValueError: If ``provider_attempt`` is invalid.
            TypeError: If the request contains unsupported data.
            ModelInputSnapshotError: If snapshot validation fails.
        """

        snapshot = create_model_input_snapshot(run_id, provider_attempt, request)
        pool = await self._ensure()
        owner, epoch = self._fence_values(run_id, lease)
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
                await self._classify_write_failure(conn, run_id, lease)
        return snapshot

    async def load(self, snapshot_id: str) -> ModelInputSnapshot | None:
        """Load and integrity-check a stored provider-request snapshot.

        Args:
            snapshot_id: Snapshot identifier to look up.

        Returns:
            The validated snapshot, or ``None`` when it does not exist.

        Raises:
            ModelInputSnapshotError: If the stored snapshot is corrupt or unsupported.
        """

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
        lease: RunLease | None = None,
    ) -> int:
        pool = await self._ensure()
        owner, epoch = self._fence_values(run_id, lease)
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
                await self._classify_write_failure(conn, run_id, lease)
        return int(row["deleted_count"] or 0)

    async def delete(self, snapshot_id: str, *, run_id: str, lease: RunLease | None = None) -> None:
        """Delete one snapshot only when it belongs to the specified run.

        Args:
            snapshot_id: Snapshot identifier to delete.
            run_id: Run that must own the snapshot.
            lease: Explicit owner/epoch token; omit for an unleased mutation.

        Raises:
            KeyError: If the run does not exist.
            LeaseLostError: If the supplied lease token is stale or mismatched.
        """

        await self._delete_snapshots(run_id, (snapshot_id,), "snapshots.id = $2", lease)

    async def prune(
        self, run_id: str, keep_ids: Sequence[str] = (), *, lease: RunLease | None = None
    ) -> int:
        """Delete all run snapshots except explicitly retained identifiers.

        Args:
            run_id: Run whose snapshots should be pruned.
            keep_ids: Snapshot identifiers to retain.
            lease: Explicit owner/epoch token; omit for an unleased mutation.

        Returns:
            Number of snapshots deleted.

        Raises:
            KeyError: If the run does not exist.
            LeaseLostError: If the supplied lease token is stale or mismatched.
        """

        return await self._delete_snapshots(
            run_id,
            (list(str(item) for item in keep_ids),),
            "snapshots.id <> ALL($2::text[])",
            lease,
        )

    async def close(self) -> None:
        """Close this store's owned pool and reset lazy initialization.

        Caller-supplied pools are retained; their host owns their lifecycle.
        """

        await close_pool(self, reset_initialized=True)

    async def __aenter__(self) -> PostgresRunStore:
        await self._ensure()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()
