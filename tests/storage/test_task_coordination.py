"""Task coordination primitives: claim / ready_tasks / release.

The DAG model (owner, blocks, blocked_by) already lives on Task; these tests
cover the store-level *verbs* that let multiple workers self-distribute work
over that graph without double-claiming.
"""

import asyncio

import pytest

from linch.sessions import InMemorySessionStore
from linch.sessions.tasks import CreateTaskInput, TaskPatch


@pytest.fixture
async def store() -> InMemorySessionStore:
    """Provide the in-memory task store used by this focused coordination suite."""
    return InMemorySessionStore()


async def test_claim_task_is_exclusive(store: InMemorySessionStore) -> None:
    sess = await store.create()
    task = await store.create_task(sess.id, CreateTaskInput(subject="a", description="d"))

    first = await store.claim_task(sess.id, task.id, owner="w1")
    second = await store.claim_task(sess.id, task.id, owner="w2")

    assert first is not None and first.owner == "w1"
    assert second is None  # already owned — no double claim
    reloaded = await store.get_task(sess.id, task.id)
    assert reloaded is not None and reloaded.owner == "w1"


async def test_claim_unknown_task_returns_none(store: InMemorySessionStore) -> None:
    sess = await store.create()
    assert await store.claim_task(sess.id, "999", owner="w1") is None


async def test_ready_tasks_respects_dependencies(store: InMemorySessionStore) -> None:
    sess = await store.create()
    first = await store.create_task(sess.id, CreateTaskInput(subject="a", description="d"))
    second = await store.create_task(sess.id, CreateTaskInput(subject="b", description="d"))
    await store.update_task(sess.id, second.id, TaskPatch(add_blocked_by=[first.id]))

    assert [task.id for task in await store.ready_tasks(sess.id)] == [first.id]
    await store.update_task(sess.id, first.id, TaskPatch(status="completed"))
    assert [task.id for task in await store.ready_tasks(sess.id)] == [second.id]


async def test_ready_tasks_excludes_owned(store: InMemorySessionStore) -> None:
    sess = await store.create()
    task = await store.create_task(sess.id, CreateTaskInput(subject="a", description="d"))

    assert [item.id for item in await store.ready_tasks(sess.id)] == [task.id]
    await store.claim_task(sess.id, task.id, owner="w1")
    assert await store.ready_tasks(sess.id) == []


async def test_release_resets_to_pending(store: InMemorySessionStore) -> None:
    sess = await store.create()
    task = await store.create_task(sess.id, CreateTaskInput(subject="a", description="d"))
    await store.claim_task(sess.id, task.id, owner="w1")
    await store.update_task(sess.id, task.id, TaskPatch(status="in_progress"))

    released = await store.release_task(sess.id, task.id)

    assert released is not None
    assert released.owner is None
    assert released.status == "pending"
    assert [item.id for item in await store.ready_tasks(sess.id)] == [task.id]


async def test_release_leaves_completed_task(store: InMemorySessionStore) -> None:
    sess = await store.create()
    task = await store.create_task(sess.id, CreateTaskInput(subject="a", description="d"))
    await store.update_task(sess.id, task.id, TaskPatch(status="completed"))

    released = await store.release_task(sess.id, task.id)

    assert released is not None
    assert released.status == "completed"  # not resurrected


async def test_concurrent_claims_single_winner(store: InMemorySessionStore) -> None:
    sess = await store.create()
    task = await store.create_task(sess.id, CreateTaskInput(subject="a", description="d"))

    results = await asyncio.gather(
        *(store.claim_task(sess.id, task.id, owner=f"w{i}") for i in range(8))
    )

    assert len([result for result in results if result is not None]) == 1
