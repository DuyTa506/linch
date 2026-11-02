"""Lease fencing tests shared by in-memory and Postgres run stores."""

from __future__ import annotations

import asyncio
import copy
import time

import pytest

from linch.run_store import InMemoryRunStore, RunCheckpoint
from linch.run_stores.postgres import PostgresRunStore
from linch.types import ProviderRequest, Usage


def _checkpoint(prompt: str = "lease") -> RunCheckpoint:
    return RunCheckpoint(
        phase="started",
        prompt=prompt,
        turn_index=1,
        total_usage=Usage(),
    )


def _memory_peer(store: InMemoryRunStore) -> InMemoryRunStore:
    # ``copy.copy`` shares the durable dictionaries, including lease state.
    return copy.copy(store)


def _assert_no_local_leases(store: object) -> None:
    assert not hasattr(store, "_held_leases")
    assert not hasattr(store, "_lease_owners")
    assert not hasattr(store, "_lease_ttls")


@pytest.fixture(params=["memory", "postgres"])
async def lease_stores(request: pytest.FixtureRequest, pg_dsn):
    kind = request.param
    if kind == "memory":
        first = InMemoryRunStore()
        second = _memory_peer(first)
    else:
        first = PostgresRunStore(pg_dsn)
        second = PostgresRunStore(pg_dsn)
    if kind == "postgres":
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
        await first.save_checkpoint(run_id, _checkpoint(), lease=lease_a)
    with pytest.raises(LeaseLostError):
        await first.append_event(run_id, _event(), lease=lease_a)
    with pytest.raises(LeaseLostError):
        await first.mark_completed(run_id, _checkpoint("done"), lease=lease_a)

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
    saved = await first.save_checkpoint(run_id, _checkpoint("expired"), lease=lease_a)
    assert saved.checkpoint is not None
    assert saved.checkpoint.prompt == "expired"
    with pytest.raises(LeaseLostError):
        await first.renew(lease_a)


async def test_renew_uses_the_token_ttl_after_same_owner_refresh(lease_stores) -> None:
    """Renewal carries the caller token's TTL, not a newer claim's duration."""
    first, second = lease_stores
    run_id = f"lease-token-ttl-{id(first)}-{time.monotonic_ns()}"
    await first.create_run("session-1", id=run_id)
    original = await first.claim(run_id, "owner-a", 0.05)
    refreshed = await second.claim(run_id, "owner-a", 30.0)
    assert refreshed.epoch == original.epoch
    renewed = await first.renew(original)
    assert renewed.ttl_s == original.ttl_s == 0.05
    assert renewed.expires_at < refreshed.expires_at
    await first.release(renewed)


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
        await second.save_checkpoint(run_id, _checkpoint("released"), lease=stale_copy)
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


async def test_token_remains_authoritative_after_store_reconnect(lease_stores) -> None:
    """A token remains usable when its backend handle is reopened."""
    first, _second = lease_stores
    run_id = f"lease-reconnect-{id(first)}-{time.monotonic_ns()}"
    await first.create_run("session-1", id=run_id)
    lease = await first.claim(run_id, "owner-a", 30.0)
    if isinstance(first, PostgresRunStore):
        reopened = PostgresRunStore(first._dsn)
        await first.close()
    else:
        reopened = copy.copy(first)
    try:
        saved = await reopened.save_checkpoint(run_id, _checkpoint("reconnected"), lease=lease)
        assert saved.checkpoint is not None
        assert saved.checkpoint.prompt == "reconnected"
        await reopened.release(lease)
    finally:
        await reopened.close()


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
        first.save_checkpoint(run_id, _checkpoint("stale checkpoint"), lease=old_lease),
        first.save_checkpoint_with_events(
            run_id, _checkpoint("stale phase"), [_event()], lease=old_lease
        ),
        first.append_event(run_id, _event(), lease=old_lease),
        first.append_events(run_id, [_event(), _event()], lease=old_lease),
        first.mark_completed(run_id, _checkpoint("stale completed"), lease=old_lease),
        first.mark_failed(
            run_id,
            _checkpoint("bad"),
            {"message": "stale"},
            events=[_event()],
            lease=old_lease,
        ),
        first.save(run_id, 2, request, lease=old_lease),
        first.delete(snapshot.id, run_id=run_id, lease=old_lease),
        first.prune(run_id, lease=old_lease),
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
        await first.create_run(f"session-{index}", id=run_id)
        lease = await first.claim(run_id, "cycle-owner", 30.0)
        await first.save_checkpoint(run_id, _checkpoint(str(index)), lease=lease)
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

    for prompt in ("fenced", "fenced again"):
        with pytest.raises(LeaseLostError):
            await old_holder.save_checkpoint(run_id, _checkpoint(prompt), lease=old_lease)

    with pytest.raises(LeaseLostError):
        await old_holder.release(old_lease)
    _assert_no_local_leases(old_holder)

    renewed = await live_holder.renew(live_lease)
    await live_holder.save_checkpoint(run_id, _checkpoint("live"), lease=renewed)
    await live_holder.release(renewed)
    _assert_no_local_leases(live_holder)


async def test_explicit_lease_is_required_and_remains_fenced_after_release(lease_stores) -> None:
    """A stale immutable token cannot become an unleased write after release."""
    from dataclasses import replace

    from linch.run_store import LeaseLostError

    first, second = lease_stores
    run_id = f"explicit-token-{id(first)}-{time.monotonic_ns()}"
    await first.create_run("session-1", id=run_id)
    lease = await first.claim(run_id, "owner-a", 30.0)
    assert lease.ttl_s == 30.0

    await first.release(lease)
    with pytest.raises(LeaseLostError):
        await first.save_checkpoint(run_id, _checkpoint("stale"), lease=lease)
    with pytest.raises(LeaseLostError):
        await first.append_events(run_id, [], lease=lease)
    with pytest.raises(LeaseLostError):
        await first.prune(run_id, lease=lease)

    current = await second.claim(run_id, "owner-b", 30.0)
    with pytest.raises(LeaseLostError):
        await first.append_event(run_id, _event(), lease=lease)
    before = await second.load_run(run_id)
    assert before is not None
    invalid_tokens = (
        replace(current, owner="other-owner"),
        replace(current, epoch=current.epoch + 1),
        replace(current, run_id="other"),
    )
    for invalid in invalid_tokens:
        with pytest.raises(LeaseLostError):
            await first.save_checkpoint(run_id, _checkpoint("invalid token"), lease=invalid)
    assert await second.load_run(run_id) == before
    await second.save_checkpoint(run_id, _checkpoint("current"), lease=current)
    await second.release(current)


async def test_malformed_explicit_owner_cannot_bypass_fencing(lease_stores) -> None:
    """An owner-less token is never treated as an unleased mutation."""
    from linch.run_store import LeaseLostError, RunLease

    first, _second = lease_stores
    run_id = f"malformed-token-{id(first)}-{time.monotonic_ns()}"
    await first.create_run("session-1", id=run_id)
    token = RunLease(
        run_id=run_id,
        owner=None,  # type: ignore[arg-type]
        epoch=0,
        expires_at=time.time() + 30,
        ttl_s=30.0,
    )
    with pytest.raises(LeaseLostError):
        await first.save_checkpoint(run_id, _checkpoint("must reject"), lease=token)
    with pytest.raises(LeaseLostError):
        await first.renew(token)
    with pytest.raises(LeaseLostError):
        await first.release(token)


@pytest.mark.parametrize(
    ("mutation", "variant"),
    [
        pytest.param("create_run", "pending", id="create_run"),
        pytest.param("save_checkpoint", "normal", id="save_checkpoint"),
        pytest.param("save_checkpoint_with_events", "events", id="save_checkpoint_with_events"),
        pytest.param("append_event", "normal", id="append_event"),
        pytest.param("mark_completed", "normal", id="mark_completed"),
        pytest.param("mark_failed", "events", id="mark_failed"),
        pytest.param("append_events", "events", id="append_events"),
        pytest.param("append_events", "empty", id="append_events_empty"),
        pytest.param("save", "normal", id="save_snapshot"),
        pytest.param("delete", "missing", id="delete_snapshot"),
        pytest.param("prune", "empty", id="prune_snapshots"),
    ],
)
async def test_explicit_token_missing_run_is_lease_lost_for_every_mutation(
    lease_stores, mutation: str, variant: str
) -> None:
    """Every leased mutation classifies an absent durable run as lease loss."""
    from linch.run_store import LeaseLostError, RunLease

    first, _second = lease_stores
    run_id = f"missing-token-{id(first)}-{time.monotonic_ns()}"
    token = RunLease(run_id, "owner", 1, time.time() + 30, 30.0)
    request = ProviderRequest(model="test", system=[], tools=[], messages=[])
    if mutation == "create_run":
        operation = first.create_run("session-1", id=run_id, lease=token)
    elif mutation == "save_checkpoint":
        operation = first.save_checkpoint(run_id, _checkpoint("missing"), lease=token)
    elif mutation == "save_checkpoint_with_events":
        operation = first.save_checkpoint_with_events(
            run_id, _checkpoint("missing"), [_event()], lease=token
        )
    elif mutation == "append_event":
        operation = first.append_event(run_id, _event(), lease=token)
    elif mutation == "mark_completed":
        operation = first.mark_completed(run_id, _checkpoint("missing"), lease=token)
    elif mutation == "mark_failed":
        operation = first.mark_failed(
            run_id, _checkpoint("missing"), {"message": "missing"}, events=[_event()], lease=token
        )
    elif mutation == "append_events":
        operation = first.append_events(
            run_id, [] if variant == "empty" else [_event()], lease=token
        )
    elif mutation == "save":
        operation = first.save(run_id, 1, request, lease=token)
    elif mutation == "delete":
        operation = first.delete("missing-snapshot", run_id=run_id, lease=token)
    elif mutation == "prune":
        operation = first.prune(run_id, lease=token)
    else:
        raise AssertionError(f"unhandled mutation {mutation!r}")
    with pytest.raises(LeaseLostError):
        await operation


@pytest.mark.parametrize(
    ("mutation", "variant"),
    [
        pytest.param("create_run", "pending", id="create_run_pending_adoption"),
        pytest.param("save_checkpoint", "normal", id="save_checkpoint"),
        pytest.param("save_checkpoint_with_events", "events", id="save_checkpoint_with_events"),
        pytest.param("append_event", "normal", id="append_event"),
        pytest.param("mark_completed", "normal", id="mark_completed"),
        pytest.param("mark_failed", "events", id="mark_failed"),
        pytest.param("append_events", "events", id="append_events"),
        pytest.param("append_events", "empty", id="append_events_empty"),
        pytest.param("save", "normal", id="save_snapshot"),
        pytest.param("delete", "existing", id="delete_snapshot"),
        pytest.param("delete", "missing", id="delete_missing_snapshot"),
        pytest.param("prune", "delete", id="prune_snapshots"),
        pytest.param("prune", "keep_all", id="prune_snapshots_noop"),
    ],
)
async def test_expired_token_is_fenced_for_each_mutation(
    lease_stores, mutation: str, variant: str
) -> None:
    """Fence each mutation, including explicit no-op forms, on one store handle."""
    from linch.run_store import LeaseLostError

    store, _peer = lease_stores
    if mutation == "create_run":
        pending_id = f"single-store-pending-{id(store)}-{time.monotonic_ns()}"
        if isinstance(store, InMemoryRunStore):
            await store.create_run("session-1", id=pending_id)
            store._runs[pending_id].status = "pending"
        else:
            pool = await store._ensure()
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await store.create_pending_run(conn, "session-1", id=pending_id)
        old = await store.claim(pending_id, "pending-a", 0.05)
        await asyncio.sleep(max(0.0, old.expires_at - time.time()) + 0.02)
        current = await store.claim(pending_id, "pending-b", 30.0)
        before = await store.load_run(pending_id)
        assert before is not None
        with pytest.raises(LeaseLostError):
            await store.create_run(
                "session-1",
                id=pending_id,
                initial_checkpoint=_checkpoint("stale adoption"),
                lease=old,
            )
        assert await store.load_run(pending_id) == before
        adopted = await store.create_run(
            "session-1",
            id=pending_id,
            initial_checkpoint=_checkpoint("current adoption"),
            lease=current,
        )
        assert adopted.status == "running"
        with pytest.raises(LeaseLostError):
            await store.release(old)
        await store.release(current)
        return

    run_id = f"single-store-matrix-{id(store)}-{time.monotonic_ns()}"
    await store.create_run("session-1", id=run_id)
    old = await store.claim(run_id, "owner-a", 0.05)
    await asyncio.sleep(max(0.0, old.expires_at - time.time()) + 0.02)
    current = await store.claim(run_id, "owner-b", 30.0)
    await store.save_checkpoint(run_id, _checkpoint("current"), lease=current)
    await store.append_event(run_id, _event(), lease=current)
    request = ProviderRequest(model="test", system=[], tools=[], messages=[])
    snapshots = [await store.save(run_id, index, request, lease=current) for index in (1, 2, 3)]
    before = await store.load_run(run_id)
    before_events = await store.load_events(run_id)
    assert before is not None
    before_snapshots = [await store.load(snapshot.id) for snapshot in snapshots]

    if mutation == "save_checkpoint":
        operation = store.save_checkpoint(run_id, _checkpoint("stale"), lease=old)
    elif mutation == "save_checkpoint_with_events":
        operation = store.save_checkpoint_with_events(
            run_id, _checkpoint("stale phase"), [_event()], lease=old
        )
    elif mutation == "append_event":
        operation = store.append_event(run_id, _event(), lease=old)
    elif mutation == "mark_completed":
        operation = store.mark_completed(run_id, _checkpoint("stale completed"), lease=old)
    elif mutation == "mark_failed":
        operation = store.mark_failed(
            run_id,
            _checkpoint("stale failed"),
            {"message": "stale"},
            events=[_event()],
            lease=old,
        )
    elif mutation == "append_events":
        operation = store.append_events(run_id, [] if variant == "empty" else [_event()], lease=old)
    elif mutation == "save":
        operation = store.save(run_id, 4, request, lease=old)
    elif mutation == "delete":
        snapshot_id = snapshots[0].id if variant == "existing" else "missing-snapshot"
        operation = store.delete(snapshot_id, run_id=run_id, lease=old)
    elif mutation == "prune":
        keep_ids = tuple(snapshot.id for snapshot in snapshots) if variant == "keep_all" else ()
        operation = store.prune(run_id, keep_ids=keep_ids, lease=old)
    else:
        raise AssertionError(f"unhandled mutation {mutation!r}")

    with pytest.raises(LeaseLostError):
        await operation

    after = await store.load_run(run_id)
    assert after == before
    assert await store.load_events(run_id) == before_events
    assert [await store.load(snapshot.id) for snapshot in snapshots] == before_snapshots
    with pytest.raises(LeaseLostError):
        await store.release(old)
    await store.save_checkpoint(run_id, _checkpoint("still current"), lease=current)
    await store.release(current)
