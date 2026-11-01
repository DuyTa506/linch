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
    """Create another holder sharing storage but with independent lease state."""
    peer = copy.copy(store)
    # ``copy.copy`` shares the durable dictionaries, including ``_lease_state``.
    # Lease ownership is process-local, so each peer gets independent copies of
    # the holder's maps only.
    for name in ("_held_leases", "_lease_ttls", "_lease_owners"):
        setattr(peer, name, dict(getattr(store, name)))
    return peer


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


async def test_run_store_round_trip_status_and_reconnect(store_case: _StoreCase) -> None:
    run_id = f"conformance-{uuid4()}"
    store = store_case.store
    await store.create_run("session-1", id=run_id, meta={"source": "test"})

    saved = await store.save_checkpoint(run_id, _checkpoint())
    assert saved.status == "running"
    assert saved.checkpoint is not None
    assert saved.checkpoint.prompt == "hello"

    # A returned checkpoint is an isolated snapshot, so caller mutation cannot
    # alter the durable record.
    saved.checkpoint.extension_state["nested"]["labels"].append("caller")
    loaded = await store.load_run(run_id)
    assert loaded is not None
    assert loaded.checkpoint is not None
    assert loaded.checkpoint.extension_state["nested"]["labels"] == ["a", "b"]

    seqs = await store.append_events(run_id, [_event("one"), _event("two")])
    assert seqs == [1, 2]
    assert await store.append_event(run_id, _event("three")) == 3
    rows = await store.load_events(run_id)
    assert [row.seq for row in rows] == [1, 2, 3]
    assert [row.event.message.content[0].text for row in rows] == ["one", "two", "three"]
    resumed = await store.load_events(run_id, after_seq=1)
    assert [row.seq for row in resumed] == [2, 3]

    done = await store.mark_completed(run_id, _checkpoint("done"))
    assert done.status == "completed"
    assert done.checkpoint is not None
    assert done.checkpoint.phase == "completed"

    failed_id = f"conformance-failed-{uuid4()}"
    await store.create_run("session-1", id=failed_id)
    failed, seqs = await store.mark_failed(
        failed_id, _checkpoint("failed"), {"message": "boom"}, events=[_event("terminal")]
    )
    assert seqs == [1]
    assert [row.event for row in await store.load_events(failed_id)] == [_event("terminal")]
    assert failed.status == "failed"
    failed_loaded = await store.load_run(failed_id)
    assert failed_loaded is not None
    assert failed_loaded.status == "failed"
    assert failed_loaded.meta["errors"] == [{"message": "boom"}]


async def test_run_store_preserves_cyclic_tool_metadata_as_markers(store_case: _StoreCase) -> None:
    from linch.events import ToolCallEndEvent
    from linch.tools.base import ToolResult

    metadata = {"labels": ["stable"]}
    metadata["cycle"] = metadata
    event = ToolCallEndEvent(
        tool_use_id="tool-1",
        tool_name="Lookup",
        tool_result=ToolResult(content="done", metadata=metadata),
    )
    store = store_case.store
    run_id = f"conformance-{uuid4()}"
    await store.create_run("session-1", id=run_id)

    await store.append_event(run_id, event)
    rows = await store.load_events(run_id)

    assert rows[0].event.tool_result.metadata == {"labels": ["stable"], "cycle": "<recursion>"}


async def test_run_store_bounds_deep_acyclic_tool_metadata(store_case: _StoreCase) -> None:
    from typing import Any

    from linch.events import IGNORABLE_MAX_DEPTH, ToolCallEndEvent
    from linch.tools.base import ToolResult

    payload: Any = "leaf"
    for _ in range(IGNORABLE_MAX_DEPTH + 1000):
        payload = [payload]
    event = ToolCallEndEvent(
        tool_use_id="tool-1",
        tool_name="Lookup",
        tool_result=ToolResult(content="done", metadata={"payload": payload}),
    )
    store = store_case.store
    run_id = f"conformance-{uuid4()}"
    await store.create_run("session-1", id=run_id)

    await store.append_event(run_id, event)
    rows = await store.load_events(run_id)

    node = rows[0].event.tool_result.metadata["payload"]
    while isinstance(node, list):
        node = node[0]
    assert node == "<max-depth>"


@pytest.mark.parametrize("kind", ["dict", "list", "mixed"])
@pytest.mark.parametrize("wrappers", [0, 1, 2])
async def test_run_store_preserves_opaque_event_at_exact_depth_boundary(
    store_case: _StoreCase, kind: str, wrappers: int
) -> None:
    from typing import Any

    from linch.events import IGNORABLE_MAX_DEPTH, IgnorableEvent, SubagentEvent, event_to_dict

    payload: Any = "leaf"
    for index in range(IGNORABLE_MAX_DEPTH - 1 - wrappers):
        payload = (
            {"next": payload} if kind == "dict" or (kind == "mixed" and index % 2) else [payload]
        )
    event = IgnorableEvent(
        original_type="future_event",
        raw={"type": "future_event", "ignorable": True, "payload": payload},
    )
    for _ in range(wrappers):
        event = SubagentEvent("parent", "child", "worker", "Worker", event)
    expected = event_to_dict(event)
    store = store_case.store
    run_id = f"conformance-{uuid4()}"
    await store.create_run("session-1", id=run_id)

    await store.append_event(run_id, event)
    rows = await store.load_events(run_id)

    assert event_to_dict(rows[0].event) == expected


async def test_run_store_event_sequence_is_monotonic_under_concurrent_appends(
    store_case: _StoreCase,
) -> None:
    import asyncio

    run_id = f"conformance-concurrent-{uuid4()}"
    store = store_case.store
    await store.create_run("session-1", id=run_id)
    returned = await asyncio.gather(
        *(
            (store if index % 2 == 0 else store_case.peer).append_event(run_id, _event(str(index)))
            for index in range(32)
        )
    )
    assert sorted(returned) == list(range(1, 33))
    rows = await store.load_events(run_id)
    assert [row.seq for row in rows] == list(range(1, 33))

    other_run_id = f"conformance-concurrent-other-{uuid4()}"
    await store.create_run("session-1", id=other_run_id)
    assert await store.append_event(other_run_id, _event("independent")) == 1


async def test_event_batch_encoding_failure_is_atomic(store_case) -> None:
    store = store_case.store
    run_id = f"batch-encoding-{uuid4()}"
    await store.create_run("session", id=run_id)
    await store.append_event(run_id, _event("before"))
    before = await store.load_events(run_id)
    with pytest.raises(ValueError):
        await store.append_events(run_id, [_event("valid prefix"), object()])
    assert await store.load_events(run_id) == before
    assert await store.append_event(run_id, _event("after")) == 2


@pytest.mark.parametrize("staleness", ["epoch", "owner"])
async def test_stale_release_preserves_current_local_lease(store_case, staleness) -> None:
    from dataclasses import replace

    from linch.run_store import LeaseLostError

    store = store_case.store
    run_id = f"stale-release-{uuid4()}"
    await store.create_run("session", id=run_id)
    old = await store.claim(run_id, "owner", 30.0)
    if staleness == "epoch":
        await store.release(old)
        current = await store.claim(run_id, "owner", 30.0)
    else:
        current = old
        old = replace(old, owner="other-owner")
    with pytest.raises(LeaseLostError):
        await store.release(old)
    assert await store.renew(current) is not None
    assert await store.append_event(run_id, _event("current owner")) == 1
    await store.release(current)


async def test_run_store_concurrent_claim_has_one_winner(store_case: _StoreCase) -> None:
    import asyncio

    from linch.run_store import RunLeaseConflictError

    run_id = f"conformance-claim-{uuid4()}"
    store = store_case.store
    peer = store_case.peer
    await store.create_run("session-1", id=run_id)
    results = await asyncio.gather(
        store.claim(run_id, "owner-a", 30.0),  # type: ignore[attr-defined]
        peer.claim(run_id, "owner-b", 30.0),  # type: ignore[attr-defined]
        return_exceptions=True,
    )
    winners = [result for result in results if not isinstance(result, Exception)]
    conflicts = [result for result in results if isinstance(result, RunLeaseConflictError)]
    assert len(winners) == 1
    assert len(conflicts) == 1


async def test_run_store_concurrent_create_same_id_rejects_existing_id(
    store_case: _StoreCase,
) -> None:
    import asyncio

    run_id = f"conformance-create-{uuid4()}"
    results = await asyncio.gather(
        store_case.store.create_run("session-1", id=run_id, meta={"owner": "first"}),
        store_case.peer.create_run("session-1", id=run_id, meta={"owner": "second"}),
        return_exceptions=True,
    )
    errors = [result for result in results if isinstance(result, ValueError)]
    successes = [result for result in results if not isinstance(result, Exception)]
    assert len(errors) == 1
    assert len(successes) == 1
    assert successes[0].id == run_id


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


async def test_pending_adoption_rejects_different_session(store_case: _StoreCase) -> None:
    run_id = f"conformance-pending-owner-{uuid4()}"
    await _seed_pending(store_case, run_id, "session-1", {})
    with pytest.raises(ValueError, match="belongs to session session-1"):
        await store_case.store.create_run("session-2", id=run_id, initial_checkpoint=_checkpoint())


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
    await store.claim(run_id, "owner-a", 30.0)  # type: ignore[attr-defined]
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
            "session-1", id=run_id, meta={"worker": "stale"}, initial_checkpoint=_checkpoint()
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


@pytest.mark.parametrize(
    ("meta", "error", "history"),
    [
        ({"prompt": "before\0after"}, {"message": "failed"}, []),
        ({"prompt\0key": "value"}, {"message": "failed"}, []),
        (
            {"errors": [{"message": "old\0failure"}]},
            {"message": "failed"},
            [{"message": "old\0failure"}],
        ),
        ({"errors": [{"old\0key": "failure"}]}, {"message": "failed"}, [{"old\0key": "failure"}]),
        ({}, {"message": "failed\0again", "key\0": "value"}, []),
        ({"errors": ["malformed\0history"]}, {"message": "failed"}, []),
        ({"errors": {"malformed\0key": "value"}}, {"message": "failed"}, []),
    ],
)
async def test_mark_failed_preserves_nul_metadata_and_error_history(
    store_case, meta, error, history
):
    store = store_case.store
    run_id = f"failed-nul-{uuid4()}"
    await store.create_run("session", id=run_id, meta=meta)
    assert await store.append_event(run_id, _event("before")) == 1
    failed, seqs = await store.mark_failed(
        run_id, _checkpoint("failed\0prompt"), error, events=[_event("terminal\0event")]
    )
    assert failed.status == "failed"
    assert failed.checkpoint.phase == "failed"
    assert failed.checkpoint.prompt == "failed\0prompt"
    assert failed.meta == {**meta, "errors": [*history, error]}
    assert await store.load_run(run_id) == failed
    assert seqs == [2]
    assert [row.event for row in await store.load_events(run_id)] == [
        _event("before"),
        _event("terminal\0event"),
    ]


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


async def test_invalid_initial_checkpoint_cannot_partially_adopt_pending(store_case):
    rid = f"invalid-adoption-{uuid4()}"
    store = store_case.store
    await _seed_pending(store_case, rid, "s", {"queue": "original"})
    before = await store.load_run(rid)
    invalid = _checkpoint()
    invalid.pending_tool_blocks = [object()]
    with pytest.raises(TypeError):
        await store.create_run("s", id=rid, meta={"new": "metadata"}, initial_checkpoint=invalid)
    assert await store.load_run(rid) == before
    assert await store.load_events(rid) == []
