"""Shared run-store conformance checks for the local and Postgres backends.

The Postgres parameter is intentionally part of this suite even though the
repository's normal test environment does not install ``asyncpg``.  Set
``AGENT_KIT_TEST_PG_DSN`` and install the ``postgres`` extra to exercise it.
"""

from __future__ import annotations

import copy
import time
from dataclasses import dataclass
from uuid import uuid4

import pytest

from linch.events import UserEvent
from linch.run_store import InMemoryRunStore, RunCheckpoint
from linch.run_stores.postgres import PostgresRunStore
from linch.types import Message, TextBlock, Usage


@dataclass
class _StoreCase:
    name: str
    store: object
    peer: object


def _memory_peer(store: InMemoryRunStore) -> InMemoryRunStore:
    """Create another holder sharing the durable in-memory dictionaries."""
    return copy.copy(store)


@pytest.fixture(params=["memory", "postgres"])
async def store_case(request: pytest.FixtureRequest, pg_dsn):
    kind = request.param
    if kind == "memory":
        store: object = InMemoryRunStore()
        peer: object = _memory_peer(store)
    else:
        store = PostgresRunStore(pg_dsn)
        peer = PostgresRunStore(pg_dsn)

    if kind == "postgres":
        # Warm each backend separately so the conformance checks exercise
        # concurrent store operations rather than connection/pool startup.
        await store.load_run("__conformance_fixture_probe__")
        await peer.load_run("__conformance_fixture_probe__")

    case = _StoreCase(kind, store, peer)
    try:
        yield case
    finally:
        await store.close()  # type: ignore[attr-defined]
        if peer is not store:
            await peer.close()  # type: ignore[attr-defined]


def _checkpoint(prompt: str = "hello") -> RunCheckpoint:
    return RunCheckpoint(
        phase="started",
        prompt=prompt,
        turn_index=1,
        total_usage=Usage(input_tokens=2, output_tokens=3),
        extension_state={"nested": {"labels": ["a", "b"]}},
    )


def _event(label: str) -> UserEvent:
    return UserEvent(message=Message(role="user", content=[TextBlock(text=label)]))


async def test_leased_event_encoding_failure_is_atomic(store_case) -> None:
    """A leased invalid batch cannot partially update the run or event log."""
    store = store_case.store
    run_id = f"leased-batch-encoding-{uuid4()}"
    await store.create_run("session", id=run_id)
    lease = await store.claim(run_id, "owner", 30.0)
    await store.save_checkpoint(run_id, _checkpoint("before"), lease=lease)
    await store.append_event(run_id, _event("before"), lease=lease)
    before = await store.load_run(run_id)
    before_events = await store.load_events(run_id)
    with pytest.raises(ValueError):
        await store.append_events(run_id, [_event("valid prefix"), object()], lease=lease)
    with pytest.raises(ValueError):
        await store.save_checkpoint_with_events(
            run_id, _checkpoint("invalid phase"), [object()], lease=lease
        )
    assert await store.load_run(run_id) == before
    assert await store.load_events(run_id) == before_events
    await store.save_checkpoint(run_id, _checkpoint("after"), lease=lease)
    await store.release(lease)


async def test_unleased_public_delegates_support_legacy_override_signatures(
    store_case: _StoreCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Omitted leases stay compatible while supplied leases fail closed."""
    store = store_case.store
    run_id = f"legacy-delegates-{uuid4()}"
    await store.create_run("session", id=run_id)
    lease = await store.claim(run_id, "owner", 30.0)
    if isinstance(store, PostgresRunStore):
        original_phase = store.save_checkpoint_with_events

        async def legacy_phase(run_id, checkpoint, events, *, status="running"):
            return await original_phase(run_id, checkpoint, events, status=status)

        monkeypatch.setattr(store, "save_checkpoint_with_events", legacy_phase)
        await store.save_checkpoint(run_id, _checkpoint("unleased phase"))
        with pytest.raises(TypeError, match="does not support lease"):
            await store.save_checkpoint(run_id, _checkpoint("leased phase"), lease=lease)

        original_batch = store.append_events

        async def legacy_batch(run_id, events):
            return await original_batch(run_id, events)

        monkeypatch.setattr(store, "append_events", legacy_batch)
        await store.append_event(run_id, _event("unleased event"))
        with pytest.raises(TypeError, match="does not support lease"):
            await store.append_event(run_id, _event("leased event"), lease=lease)

        original_checkpoint = store.save_checkpoint

        async def legacy_checkpoint(run_id, checkpoint, *, status="running"):
            return await original_checkpoint(run_id, checkpoint, status=status)

        monkeypatch.setattr(store, "save_checkpoint", legacy_checkpoint)
        await store.mark_completed(run_id, _checkpoint("unleased complete"))
        with pytest.raises(TypeError, match="does not support lease"):
            await store.mark_completed(run_id, _checkpoint("leased complete"), lease=lease)
    else:
        original_checkpoint = store.save_checkpoint

        async def legacy_checkpoint(run_id, checkpoint, *, status="running"):
            return await original_checkpoint(run_id, checkpoint, status=status)

        monkeypatch.setattr(store, "save_checkpoint", legacy_checkpoint)
        await store.mark_completed(run_id, _checkpoint("unleased complete"))
        with pytest.raises(TypeError, match="does not support lease"):
            await store.mark_completed(run_id, _checkpoint("leased complete"), lease=lease)


async def _seed_pending(
    store_case: _StoreCase,
    run_id: str,
    session_id: str,
    meta: dict[str, object],
) -> None:
    store = store_case.store
    if isinstance(store, InMemoryRunStore):
        await store.create_run(session_id, id=run_id, meta=meta)
        store._runs[run_id].status = "pending"  # type: ignore[assignment]
        return
    assert isinstance(store, PostgresRunStore)
    pool = await store._ensure()
    async with pool.acquire() as conn:
        async with conn.transaction():
            created = await store.create_pending_run(conn, session_id, id=run_id, meta=meta)
            assert created is not None


async def test_pending_adoption_merges_metadata_and_retains_contract(
    store_case: _StoreCase,
) -> None:
    from linch.run_store import build_run_contract, run_meta_with_contract

    run_id = f"conformance-pending-contract-{uuid4()}"
    contract = build_run_contract(primary_model="provider/model")
    await _seed_pending(
        store_case,
        run_id,
        "session-1",
        run_meta_with_contract(contract, {"created": "before"}),
    )
    adopted = await store_case.store.create_run(
        "session-1",
        id=run_id,
        meta=run_meta_with_contract(contract, {"created": "after", "worker": "one"}),
        initial_checkpoint=_checkpoint(),
    )
    assert adopted.status == "running"
    assert adopted.checkpoint == _checkpoint()
    assert adopted.meta["created"] == "after"
    assert adopted.meta["worker"] == "one"
    assert adopted.meta["run_contract"] == run_meta_with_contract(contract)["run_contract"]


async def test_pending_adoption_rejects_incompatible_contract_without_mutation(
    store_case: _StoreCase,
) -> None:
    from linch.run_store import (
        RunContractMismatchError,
        build_run_contract,
        run_meta_with_contract,
    )

    run_id = f"conformance-pending-mismatch-{uuid4()}"
    stored_contract = build_run_contract(primary_model="provider/stored")
    requested_contract = build_run_contract(primary_model="provider/requested")
    stored_meta = run_meta_with_contract(stored_contract, {"source": "queue"})
    await _seed_pending(store_case, run_id, "session-1", stored_meta)
    with pytest.raises(RunContractMismatchError):
        await store_case.store.create_run(
            "session-1",
            id=run_id,
            meta=run_meta_with_contract(requested_contract, {"worker": "one"}),
            initial_checkpoint=_checkpoint(),
        )
    loaded = await store_case.store.load_run(run_id)
    assert loaded is not None
    assert loaded.status == "pending"
    assert loaded.meta == stored_meta


async def test_pending_adoption_is_fenced_after_peer_claims_newer_epoch(
    store_case: _StoreCase,
) -> None:
    from linch.run_store import LeaseLostError

    run_id = f"conformance-pending-fence-{uuid4()}"
    await _seed_pending(store_case, run_id, "session-1", {})
    store = store_case.store
    peer = store_case.peer
    old_lease = await store.claim(run_id, "owner-a", 30.0)  # type: ignore[attr-defined]
    if isinstance(store, InMemoryRunStore):
        owner, _, epoch = store._lease_state[run_id]
        assert owner == "owner-a"
        store._lease_state[run_id] = (owner, time.time() - 1, epoch)
    else:
        assert isinstance(store, PostgresRunStore)
        pool = await store._ensure()
        await pool.execute("UPDATE runs SET lease_expires_at = 0 WHERE id = $1", run_id)
    await peer.claim(run_id, "owner-b", 30.0)  # type: ignore[attr-defined]
    with pytest.raises(LeaseLostError):
        await store.create_run(
            "session-1",
            id=run_id,
            meta={"worker": "stale"},
            initial_checkpoint=_checkpoint(),
            lease=old_lease,
        )
    loaded = await store.load_run(run_id)
    assert loaded is not None
    assert loaded.status == "pending"


async def test_failed_terminal_encoding_failure_changes_neither_status_nor_log(store_case):
    store = store_case.store
    run_id = f"failed-encoding-{uuid4()}"
    await store.create_run("session", id=run_id, meta={"errors": [{"message": "before"}]})
    await store.save_checkpoint(run_id, _checkpoint("before"))
    await store.append_event(run_id, _event("before"))
    before = await store.load_run(run_id)
    before_events = await store.load_events(run_id)
    with pytest.raises(ValueError):
        await store.mark_failed(
            run_id,
            _checkpoint("after"),
            {"message": "after"},
            events=[_event("valid prefix"), object()],
        )
    assert await store.load_run(run_id) == before
    assert await store.load_events(run_id) == before_events


async def test_create_run_atomically_detaches_initial_checkpoint(store_case):
    store = store_case.store
    rid = f"initial-{uuid4()}"
    checkpoint = _checkpoint()
    record = await store.create_run("s", id=rid, initial_checkpoint=checkpoint)
    assert record.checkpoint == checkpoint
    with pytest.raises(ValueError, match="already exists"):
        await store.create_run("s", id=rid, initial_checkpoint=_checkpoint("collision"))
    assert (await store.load_run(rid)).checkpoint == checkpoint
    checkpoint.extension_state["nested"]["labels"].append("changed")
    assert (await store.load_run(rid)).checkpoint.extension_state["nested"]["labels"] == ["a", "b"]
    invalid = _checkpoint()
    invalid.pending_tool_blocks = [object()]
    with pytest.raises((TypeError, ValueError)):
        await store.create_run("s", id=rid + "-invalid", initial_checkpoint=invalid)
    assert await store.load_run(rid + "-invalid") is None


@pytest.mark.parametrize("terminal", ["aborted"])
async def test_terminal_run_frees_session_and_reactivation_is_atomic(store_case, terminal):
    from linch import SessionBusyError

    session_id, old_id, new_id = str(uuid4()), str(uuid4()), str(uuid4())
    store = store_case.store
    await store.create_run(session_id, id=old_id)
    await store.save_checkpoint(old_id, _checkpoint("terminal"), status=terminal)
    await store_case.peer.create_run(session_id, id=new_id)
    before = await store.load_run(old_id)
    for with_events in (False, True):
        with pytest.raises(SessionBusyError) as busy:
            if with_events:
                await store.save_checkpoint_with_events(old_id, _checkpoint(), [_event("no")])
            else:
                await store.save_checkpoint(old_id, _checkpoint())
        assert busy.value.run_id == new_id
        assert await store.load_run(old_id) == before
        assert await store.load_events(old_id) == []
