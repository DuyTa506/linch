"""Task coordination primitives: claim / ready_tasks / release.

The DAG model (owner, blocks, blocked_by) already lives on Task; these tests
cover the store-level *verbs* that let multiple workers self-distribute work
over that graph without double-claiming.
"""

import pytest

from linch.sessions import InMemorySessionStore
from linch.sessions.tasks import CreateTaskInput


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
