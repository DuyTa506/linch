"""Task coordination primitives: claim / ready_tasks / release.

The DAG model (owner, blocks, blocked_by) already lives on Task; these tests
cover the store-level *verbs* that let multiple workers self-distribute work
over that graph without double-claiming.
"""

import asyncio

import pytest

from linch.sessions import InMemorySessionStore, SqliteSessionStore
from linch.sessions.tasks import CreateTaskInput, TaskPatch

STORE_KINDS = ["memory", "sqlite"]


def _make_store(kind, tmp_path):
    if kind == "memory":
        return InMemorySessionStore()
    return SqliteSessionStore(tmp_path / "sessions.db")


@pytest.mark.parametrize("kind", STORE_KINDS)
async def test_claim_task_is_exclusive(kind, tmp_path) -> None:
    store = _make_store(kind, tmp_path)
    try:
        sess = await store.create()
        t = await store.create_task(sess.id, CreateTaskInput(subject="a", description="d"))

        first = await store.claim_task(sess.id, t.id, owner="w1")
        second = await store.claim_task(sess.id, t.id, owner="w2")

        assert first is not None and first.owner == "w1"
        assert second is None  # already owned — no double claim
        reloaded = await store.get_task(sess.id, t.id)
        assert reloaded is not None and reloaded.owner == "w1"
    finally:
        await store.close()


@pytest.mark.parametrize("kind", STORE_KINDS)
async def test_claim_unknown_task_returns_none(kind, tmp_path) -> None:
    store = _make_store(kind, tmp_path)
    try:
        sess = await store.create()
        assert await store.claim_task(sess.id, "999", owner="w1") is None
    finally:
        await store.close()


@pytest.mark.parametrize("kind", STORE_KINDS)
async def test_ready_tasks_respects_dependencies(kind, tmp_path) -> None:
    store = _make_store(kind, tmp_path)
    try:
        sess = await store.create()
        a = await store.create_task(sess.id, CreateTaskInput(subject="a", description="d"))
        b = await store.create_task(sess.id, CreateTaskInput(subject="b", description="d"))
        # b depends on a
        await store.update_task(sess.id, b.id, TaskPatch(add_blocked_by=[a.id]))

        ready = await store.ready_tasks(sess.id)
        assert [t.id for t in ready] == [a.id]  # b is blocked

        # completing the blocker unblocks the dependent
        await store.update_task(sess.id, a.id, TaskPatch(status="completed"))
        ready = await store.ready_tasks(sess.id)
        assert [t.id for t in ready] == [b.id]
    finally:
        await store.close()


@pytest.mark.parametrize("kind", STORE_KINDS)
async def test_ready_tasks_excludes_owned(kind, tmp_path) -> None:
    store = _make_store(kind, tmp_path)
    try:
        sess = await store.create()
        a = await store.create_task(sess.id, CreateTaskInput(subject="a", description="d"))

        assert [t.id for t in await store.ready_tasks(sess.id)] == [a.id]
        await store.claim_task(sess.id, a.id, owner="w1")
        assert await store.ready_tasks(sess.id) == []
    finally:
        await store.close()


@pytest.mark.parametrize("kind", STORE_KINDS)
async def test_release_resets_to_pending(kind, tmp_path) -> None:
    store = _make_store(kind, tmp_path)
    try:
        sess = await store.create()
        a = await store.create_task(sess.id, CreateTaskInput(subject="a", description="d"))
        await store.claim_task(sess.id, a.id, owner="w1")
        await store.update_task(sess.id, a.id, TaskPatch(status="in_progress"))

        released = await store.release_task(sess.id, a.id)

        assert released is not None
        assert released.owner is None
        assert released.status == "pending"
        # reclaimable again
        assert [t.id for t in await store.ready_tasks(sess.id)] == [a.id]
    finally:
        await store.close()


@pytest.mark.parametrize("kind", STORE_KINDS)
async def test_release_leaves_completed_task(kind, tmp_path) -> None:
    store = _make_store(kind, tmp_path)
    try:
        sess = await store.create()
        a = await store.create_task(sess.id, CreateTaskInput(subject="a", description="d"))
        await store.update_task(sess.id, a.id, TaskPatch(status="completed"))

        released = await store.release_task(sess.id, a.id)

        assert released is not None
        assert released.status == "completed"  # not resurrected
    finally:
        await store.close()


@pytest.mark.parametrize("kind", STORE_KINDS)
async def test_concurrent_claims_single_winner(kind, tmp_path) -> None:
    store = _make_store(kind, tmp_path)
    try:
        sess = await store.create()
        t = await store.create_task(sess.id, CreateTaskInput(subject="a", description="d"))

        results = await asyncio.gather(
            *(store.claim_task(sess.id, t.id, owner=f"w{i}") for i in range(8))
        )

        winners = [r for r in results if r is not None]
        assert len(winners) == 1
    finally:
        await store.close()
