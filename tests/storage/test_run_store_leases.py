"""Lease fencing, migration, and SQLite serialization tests for run stores."""

from __future__ import annotations

import asyncio
import copy
import importlib.util
import os
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from linch.run_store import InMemoryRunStore, RunCheckpoint, SqliteRunStore
from linch.types import ProviderRequest, Usage


def _checkpoint(prompt: str = "lease") -> RunCheckpoint:
    return RunCheckpoint(
        phase="started",
        prompt=prompt,
        turn_index=1,
        total_usage=Usage(),
    )


def _memory_peer(store: InMemoryRunStore) -> InMemoryRunStore:
    peer = copy.copy(store)
    # ``copy.copy`` shares durable state (including ``_lease_state``), while
    # holder-local lease maps must be independent for fencing tests.
    for name in ("_held_leases", "_lease_ttls", "_lease_owners"):
        setattr(peer, name, dict(getattr(store, name)))
    return peer


def _local_lease_maps(store: object) -> list[dict[str, object]]:
    """Return holder-local maps, allowing Postgres to omit ``_lease_owners``."""
    maps: list[dict[str, object]] = []
    for name in ("_held_leases", "_lease_ttls", "_lease_owners"):
        value = getattr(store, name, None)
        if isinstance(value, dict):
            maps.append(value)
    return maps


def _assert_no_local_leases(store: object) -> None:
    for mapping in _local_lease_maps(store):
        assert mapping == {}


def _postgres_store_type():
    from linch.run_stores.postgres import PostgresRunStore

    return PostgresRunStore


@pytest.fixture(params=["memory", "sqlite", "postgres"])
async def lease_stores(request: pytest.FixtureRequest, tmp_path: Path):
    kind = request.param
    if kind == "memory":
        first = InMemoryRunStore()
        second = _memory_peer(first)
    elif kind == "sqlite":
        first = SqliteRunStore(tmp_path / "leases.db")
        second = SqliteRunStore(tmp_path / "leases.db")
    else:
        dsn = os.environ.get("AGENT_KIT_TEST_PG_DSN", "")
        if not dsn:
            pytest.skip("AGENT_KIT_TEST_PG_DSN not set")
        if importlib.util.find_spec("asyncpg") is None:
            pytest.skip("asyncpg not installed")
        store_type = _postgres_store_type()
        first = store_type(dsn)
        second = store_type(dsn)
    if kind in {"sqlite", "postgres"}:
        # Warm each backend separately so lease timing starts after connection
        # and pool setup, rather than during the claim/expiry window.
        await first.load_run("__lease_fixture_probe__")
        await second.load_run("__lease_fixture_probe__")
    try:
        yield first, second
    finally:
        await first.close()
        if second is not first:
            await second.close()


async def test_lease_claim_conflict_renew_expiry_takeover_and_fencing(lease_stores) -> None:
    from linch.run_store import LeaseLostError, RunLeaseConflictError

    first, second = lease_stores
    run_id = f"lease-{id(first)}-{time.monotonic_ns()}"
    await first.create_run("session-1", id=run_id)
    lease_a = await first.claim(run_id, "owner-a", 0.2)
    assert lease_a.run_id == run_id
    assert lease_a.owner == "owner-a"
    assert lease_a.epoch >= 1
    assert lease_a.expires_at > time.time()

    with pytest.raises(RunLeaseConflictError):
        await second.claim(run_id, "owner-b", 1.0)

    renewed = await first.renew(lease_a)
    assert renewed.epoch == lease_a.epoch
    assert renewed.expires_at > lease_a.expires_at

    # Let the renewed lease expire, then let a second holder take over.  Every
    # write by the old holder is fenced by the newer epoch.
    await asyncio.sleep(max(0.0, renewed.expires_at - time.time()) + 0.03)
    lease_b = await second.claim(run_id, "owner-b", 1.0)
    assert lease_b.epoch > lease_a.epoch

    with pytest.raises(LeaseLostError):
        await first.renew(lease_a)
    with pytest.raises(LeaseLostError):
        await first.save_checkpoint(run_id, _checkpoint())
    with pytest.raises(LeaseLostError):
        await first.append_event(run_id, _event())
    with pytest.raises(LeaseLostError):
        await first.mark_completed(run_id, _checkpoint("done"))

    with pytest.raises(LeaseLostError):
        await first.release(lease_a)
    _assert_no_local_leases(first)

    await second.release(lease_b)


async def test_live_same_owner_claim_keeps_epoch_and_expiry_only_blocks_renewal(
    lease_stores,
) -> None:
    """A heartbeat-style re-claim keeps the epoch; writes fence by epoch only."""
    from linch.run_store import LeaseLostError

    first, second = lease_stores
    run_id = f"lease-expiry-{id(first)}-{time.monotonic_ns()}"
    await first.create_run("session-1", id=run_id)
    lease_a = await first.claim(run_id, "owner-a", 0.2)
    same_owner = await second.claim(run_id, "owner-a", 0.2)
    assert same_owner.epoch == lease_a.epoch
    await asyncio.sleep(max(0.0, same_owner.expires_at - time.time()) + 0.03)
    saved = await first.save_checkpoint(run_id, _checkpoint("expired"))
    assert saved.checkpoint is not None
    assert saved.checkpoint.prompt == "expired"
    with pytest.raises(LeaseLostError):
        await first.renew(lease_a)


async def test_release_bumps_epoch_and_fences_other_copy_of_old_lease(lease_stores) -> None:
    """Releasing invalidates every holder copy of the released epoch."""
    from linch.run_store import LeaseLostError

    first, second = lease_stores
    run_id = f"lease-release-{id(first)}-{time.monotonic_ns()}"
    await first.create_run("session-1", id=run_id)
    lease_a = await first.claim(run_id, "owner-a", 1.0)
    stale_copy = await second.claim(run_id, "owner-a", 1.0)
    assert stale_copy.epoch == lease_a.epoch
    await first.release(lease_a)
    with pytest.raises(LeaseLostError):
        await second.save_checkpoint(run_id, _checkpoint("released"))
    takeover = await second.claim(run_id, "owner-b", 1.0)
    assert takeover.epoch > lease_a.epoch


async def test_expired_release_matching_epoch_succeeds(lease_stores) -> None:
    first, second = lease_stores
    run_id = f"lease-expired-release-{id(first)}-{time.monotonic_ns()}"
    await first.create_run("session-1", id=run_id)
    lease = await first.claim(run_id, "owner-a", 0.2)
    await asyncio.sleep(max(0.0, lease.expires_at - time.time()) + 0.03)
    await first.release(lease)
    takeover = await second.claim(run_id, "owner-b", 1.0)
    assert takeover.epoch > lease.epoch


def _event():
    from linch.events import UserEvent
    from linch.types import Message, TextBlock

    return UserEvent(message=Message(role="user", content=[TextBlock(text="event")]))


async def test_unclaimed_instance_keeps_legacy_write_behavior(lease_stores) -> None:
    first, second = lease_stores
    run_id = f"unclaimed-{id(first)}-{time.monotonic_ns()}"
    await first.create_run("session-1", id=run_id)
    # Only the first instance claims the run.  The second has no local lease,
    # so its legacy writes remain available to hosts that do not opt in.
    await first.claim(run_id, "owner-a", 1.0)
    saved = await second.save_checkpoint(run_id, _checkpoint("legacy"))
    assert saved.checkpoint is not None
    assert saved.checkpoint.prompt == "legacy"


async def test_lease_validates_ttl_and_run_id(lease_stores) -> None:
    first, _second = lease_stores
    with pytest.raises(KeyError, match="run not found"):
        await first.claim("missing-run", "owner", 1.0)
    with pytest.raises(ValueError, match="finite positive"):
        await first.claim("missing-run", "owner", 0)
    await first.create_run("session-1", id="ttl-run")
    for ttl in (-1, float("nan"), float("inf"), "1"):
        with pytest.raises(ValueError, match="finite positive"):
            await first.claim("ttl-run", "owner", ttl)


async def test_stale_writes_are_fenced_without_mutating_persisted_state(lease_stores) -> None:
    from linch.run_store import LeaseLostError

    first, second = lease_stores
    run_id = f"lease-fence-all-{id(first)}-{time.monotonic_ns()}"
    await first.create_run("session-1", id=run_id)
    old_lease = await first.claim(run_id, "owner-a", 0.2)
    await asyncio.sleep(max(0.0, old_lease.expires_at - time.time()) + 0.03)
    new_lease = await second.claim(run_id, "owner-b", 1.0)
    assert new_lease.epoch > old_lease.epoch

    checkpoint = _checkpoint("durable")
    await second.save_checkpoint(run_id, checkpoint)
    await second.append_event(run_id, _event())
    request = ProviderRequest(model="test", system=[], tools=[], messages=[])
    snapshot = await second.save(run_id, 1, request)
    before = await second.load_run(run_id)
    before_events = await second.load_events(run_id)
    before_snapshot = await second.load(snapshot.id)
    assert before is not None
    assert before_snapshot == snapshot

    stale_ops = (
        first.append_events(run_id, [_event(), _event()]),
        first.mark_failed(run_id, _checkpoint("bad"), {"message": "stale"}),
        first.save(run_id, 2, request),
        first.delete(snapshot.id),
        first.prune(run_id),
    )
    for operation in stale_ops:
        with pytest.raises(LeaseLostError):
            await operation

    after = await second.load_run(run_id)
    after_events = await second.load_events(run_id)
    after_snapshot = await second.load(snapshot.id)
    assert after is not None
    assert after.status == before.status
    assert after.checkpoint == before.checkpoint
    assert [(row.seq, row.event.type) for row in after_events] == [
        (row.seq, row.event.type) for row in before_events
    ]
    assert after_snapshot == before_snapshot


async def test_repeated_claim_write_release_clears_holder_local_state(lease_stores) -> None:
    first, _second = lease_stores
    prefix = f"lease-cycle-{id(first)}-{time.monotonic_ns()}"
    for index in range(1000):
        run_id = f"{prefix}-{index}"
        await first.create_run("session-1", id=run_id)
        lease = await first.claim(run_id, "cycle-owner", 30.0)
        await first.save_checkpoint(run_id, _checkpoint(str(index)))
        await first.release(lease)
    _assert_no_local_leases(first)


async def test_expired_takeover_fence_then_stale_release_clears_only_old_holder(
    lease_stores,
) -> None:
    from linch.run_store import LeaseLostError

    old_holder, live_holder = lease_stores
    run_id = f"lease-stale-release-{id(old_holder)}-{time.monotonic_ns()}"
    await old_holder.create_run("session-1", id=run_id)
    old_lease = await old_holder.claim(run_id, "old-owner", 0.2)
    await asyncio.sleep(max(0.0, old_lease.expires_at - time.time()) + 0.03)
    live_lease = await live_holder.claim(run_id, "live-owner", 30.0)

    with pytest.raises(LeaseLostError):
        await old_holder.save_checkpoint(run_id, _checkpoint("fenced"))
    held = old_holder._held_leases
    assert held.get(run_id) == old_lease.epoch

    with pytest.raises(LeaseLostError):
        await old_holder.release(old_lease)
    _assert_no_local_leases(old_holder)

    renewed = await live_holder.renew(live_lease)
    await live_holder.save_checkpoint(run_id, _checkpoint("live"))
    await live_holder.release(renewed)
    _assert_no_local_leases(live_holder)


async def test_sqlite_claim_cancellation_keeps_committed_lease_fencing(
    tmp_path: Path, monkeypatch
) -> None:
    """Cancellation after COMMIT cannot lose the holder's local epoch."""
    import linch.run_store as run_store_module
    from linch.run_store import LeaseLostError

    first = SqliteRunStore(tmp_path / "cancel-claim.db")
    second = SqliteRunStore(tmp_path / "cancel-claim.db")
    entered = threading.Event()
    release_worker = threading.Event()
    finished = threading.Event()
    original = run_store_module._claim_sqlite
    captured: list[object] = []

    def delayed_callback(conn, run_id, owner, ttl_s, on_claim=None):
        def delayed(lease):
            captured.append(lease)
            entered.set()
            assert release_worker.wait(5.0)
            if on_claim is not None:
                on_claim(lease)
            finished.set()

        return original(conn, run_id, owner, ttl_s, on_claim=delayed)

    monkeypatch.setattr(run_store_module, "_claim_sqlite", delayed_callback)
    run_id = "cancelled-claim"
    try:
        await first.create_run("session-1", id=run_id)
        claim_task = asyncio.create_task(first.claim(run_id, "owner-a", 0.2))
        assert await asyncio.to_thread(entered.wait, 5.0)
        claim_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await claim_task
        release_worker.set()
        assert await asyncio.to_thread(finished.wait, 5.0)
        assert captured
        await asyncio.sleep(max(0.0, captured[0].expires_at - time.time()) + 0.03)

        takeover = await second.claim(run_id, "owner-b", 1.0)
        assert takeover.epoch >= 2
        with pytest.raises(LeaseLostError):
            await first.save_checkpoint(run_id, _checkpoint("stale"))
    finally:
        release_worker.set()
        await first.close()
        await second.close()


async def test_sqlite_migrates_legacy_schema_without_lease_columns(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE runs (
              id TEXT PRIMARY KEY,
              session_id TEXT NOT NULL,
              status TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              checkpoint TEXT,
              meta TEXT NOT NULL
            );
            CREATE TABLE run_events (
              run_id TEXT NOT NULL,
              seq INTEGER NOT NULL,
              appended_at TEXT NOT NULL,
              event TEXT NOT NULL,
              PRIMARY KEY (run_id, seq)
            );
            INSERT INTO runs VALUES
              ('legacy-run', 'session-1', 'running', '2026-01-01', '2026-01-01', NULL, '{}');
            """
        )
    store = SqliteRunStore(path)
    try:
        loaded = await store.load_run("legacy-run")
        assert loaded is not None
        lease = await store.claim("legacy-run", "owner", 1.0)
        assert lease.epoch >= 1
    finally:
        await store.close()


async def test_sqlite_concurrent_old_schema_migration_is_forward_tolerant(tmp_path: Path) -> None:
    path = tmp_path / "legacy-concurrent.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE runs (
              id TEXT PRIMARY KEY,
              session_id TEXT NOT NULL,
              status TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              checkpoint TEXT,
              meta TEXT NOT NULL
            );
            CREATE TABLE run_events (
              run_id TEXT NOT NULL,
              seq INTEGER NOT NULL,
              appended_at TEXT NOT NULL,
              event TEXT NOT NULL,
              PRIMARY KEY (run_id, seq)
            );
            INSERT INTO runs VALUES
              ('legacy-concurrent', 'session-1', 'running', '2026-01-01',
               '2026-01-01', NULL, '{}');
            """
        )
        # SqliteExecutor switches file-backed stores to WAL on first connect.
        # Preconfigure the legacy file so this test isolates concurrent lease
        # migration from that shared executor's cold journal-mode switch.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.commit()
    first = SqliteRunStore(path)
    second = SqliteRunStore(path)
    try:
        records = await asyncio.gather(
            first.load_run("legacy-concurrent"), second.load_run("legacy-concurrent")
        )
        assert all(record is not None for record in records)
    finally:
        await first.close()
        await second.close()


async def test_sqlite_snapshot_serialization_runs_off_event_loop(
    tmp_path: Path, monkeypatch
) -> None:
    """The expensive model-input encoding runs in SqliteExecutor's worker."""
    import linch.run_store as run_store_module

    store = SqliteRunStore(tmp_path / "snapshots.db")
    try:
        run = await store.create_run("session-1")
        loop_thread = threading.get_ident()
        seen: list[int] = []
        original = run_store_module.create_model_input_snapshot

        def recording_snapshot(*args, **kwargs):
            seen.append(threading.get_ident())
            return original(*args, **kwargs)

        monkeypatch.setattr(run_store_module, "create_model_input_snapshot", recording_snapshot)
        request = ProviderRequest(model="test", system=[], tools=[], messages=[])
        await store.save(run.id, 1, request)
        assert seen
        assert seen[0] != loop_thread
    finally:
        await store.close()
