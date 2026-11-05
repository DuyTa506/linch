import asyncio

import pytest

from linch.sessions import InMemorySessionStore
from linch.sessions.postgres import PostgresSessionStore
from linch.sessions.tasks import CreateTaskInput, TaskPatch
from linch.types import (
    Message,
    TextBlock,
)


@pytest.fixture(params=["memory", "postgres"])
async def store_case(request, pg_dsn):
    store = InMemorySessionStore() if request.param == "memory" else PostgresSessionStore(pg_dsn)
    try:
        yield store
    finally:
        await store.close()


async def test_message_batch_encoding_failure_is_atomic(store_case) -> None:
    store = store_case
    session = await store.create()
    message = Message(role="user", content=[TextBlock(text="before")])
    await store.append_messages(session.id, [message])
    before = await store.load_messages(session.id)
    record = await store.load(session.id)
    with pytest.raises(TypeError):
        await store.append_messages(session.id, [message, Message(role="user", content=[object()])])
    assert await store.load_messages(session.id) == before
    assert await store.load(session.id) == record
    assert (await store.append_messages(session.id, [message]))[0].seq == 2


async def test_set_invoked_skills_missing_session_raises(store_case) -> None:
    with pytest.raises(KeyError):
        await store_case.set_invoked_skills("missing", [{"name": "skill"}])
    assert await store_case.load("missing") is None


@pytest.mark.parametrize("offset", [0, 2])
async def test_session_list_offset_without_limit(store_case, monkeypatch, offset: int) -> None:
    timestamps = iter(f"2026-01-01T00:00:0{index}+00:00" for index in range(4))
    monkeypatch.setattr(f"{type(store_case).__module__}.now_iso", lambda: next(timestamps))
    sessions = [await store_case.create() for _ in range(4)]
    expected = [session.id for session in reversed(sessions)]
    assert [session.id for session in await store_case.list(offset=offset)] == expected[offset:]


@pytest.mark.parametrize("add_from_blocker", [True, False])
async def test_task_dependencies_are_reciprocal(store_case, add_from_blocker: bool) -> None:
    store = store_case
    session = await store.create()
    blocker = await store.create_task(session.id, CreateTaskInput(subject="first", description=""))
    blocked = await store.create_task(session.id, CreateTaskInput(subject="second", description=""))
    add_id = blocker.id if add_from_blocker else blocked.id
    add_patch = (
        TaskPatch(add_blocks=[blocked.id, blocked.id])
        if add_from_blocker
        else TaskPatch(add_blocked_by=[blocker.id, blocker.id])
    )
    await store.update_task(session.id, add_id, add_patch)
    assert (await store.get_task(session.id, blocker.id)).blocks == [blocked.id]
    assert (await store.get_task(session.id, blocked.id)).blocked_by == [blocker.id]
    assert [task.id for task in await store.ready_tasks(session.id)] == [blocker.id]

    await store.update_task(session.id, blocker.id, TaskPatch(status="completed"))
    assert [task.id for task in await store.ready_tasks(session.id)] == [blocked.id]
    await store.delete_task(session.id, blocker.id)
    assert (await store.get_task(session.id, blocked.id)).blocked_by == []
    assert [task.id for task in await store.ready_tasks(session.id)] == [blocked.id]


@pytest.mark.parametrize("remove_from_blocker", [True, False])
async def test_task_dependency_removal_is_reciprocal(store_case, remove_from_blocker: bool) -> None:
    store = store_case
    session = await store.create()
    blocker = await store.create_task(session.id, CreateTaskInput(subject="first", description=""))
    blocked = await store.create_task(session.id, CreateTaskInput(subject="second", description=""))
    await store.update_task(session.id, blocker.id, TaskPatch(add_blocks=[blocked.id]))
    await store.update_task(session.id, blocked.id, TaskPatch(add_blocked_by=[blocker.id]))

    remove_id = blocker.id if remove_from_blocker else blocked.id
    remove_patch = (
        TaskPatch(remove_blocks=[blocked.id])
        if remove_from_blocker
        else TaskPatch(remove_blocked_by=[blocker.id])
    )
    await store.update_task(session.id, remove_id, remove_patch)
    await store.update_task(session.id, remove_id, remove_patch)
    assert (await store.get_task(session.id, blocker.id)).blocks == []
    assert (await store.get_task(session.id, blocked.id)).blocked_by == []
    assert [task.id for task in await store.ready_tasks(session.id)] == [blocker.id, blocked.id]


@pytest.mark.parametrize("blocks_future", [True, False])
async def test_task_dependency_to_future_task_is_reciprocal(
    store_case, blocks_future: bool
) -> None:
    store = store_case
    session = await store.create()
    first = await store.create_task(session.id, CreateTaskInput(subject="first", description=""))
    patch = TaskPatch(add_blocks=["2"]) if blocks_future else TaskPatch(add_blocked_by=["2"])
    await store.update_task(session.id, first.id, patch)
    second = await store.create_task(session.id, CreateTaskInput(subject="second", description=""))
    blocker, blocked = (first, second) if blocks_future else (second, first)
    assert (await store.get_task(session.id, blocker.id)).blocks == [blocked.id]
    assert (await store.get_task(session.id, blocked.id)).blocked_by == [blocker.id]
    assert [task.id for task in await store.ready_tasks(session.id)] == [blocker.id]


@pytest.mark.parametrize("from_blocker", [True, False])
async def test_task_dependency_add_wins_over_removal_in_same_patch(
    store_case, from_blocker: bool
) -> None:
    store = store_case
    session = await store.create()
    blocker = await store.create_task(session.id, CreateTaskInput(subject="first", description=""))
    blocked = await store.create_task(session.id, CreateTaskInput(subject="second", description=""))
    task_id = blocker.id if from_blocker else blocked.id
    patch = (
        TaskPatch(add_blocks=[blocked.id], remove_blocks=[blocked.id])
        if from_blocker
        else TaskPatch(add_blocked_by=[blocker.id], remove_blocked_by=[blocker.id])
    )
    await store.update_task(session.id, task_id, patch)
    assert (await store.get_task(session.id, blocker.id)).blocks == [blocked.id]
    assert (await store.get_task(session.id, blocked.id)).blocked_by == [blocker.id]
    assert [task.id for task in await store.ready_tasks(session.id)] == [blocker.id]


@pytest.mark.parametrize(
    "boundary",
    [
        "list_tasks",
        "update_task",
        "release_task",
        "release_completed_task",
    ],
)
async def test_task_results_are_detached(store_case, boundary) -> None:
    store = store_case
    session = await store.create()
    task = await store.create_task(
        session.id,
        CreateTaskInput(subject="before", description="details", metadata={"nested": ["before"]}),
    )
    task_id = task.id
    if boundary == "get_task":
        task = await store.get_task(session.id, task_id)
    elif boundary == "list_tasks":
        task = (await store.list_tasks(session.id))[0]
    elif boundary == "update_task":
        task = await store.update_task(session.id, task_id, TaskPatch(description="details"))
    elif boundary == "claim_task":
        task = await store.claim_task(session.id, task_id, "worker")
    elif boundary == "ready_tasks":
        task = (await store.ready_tasks(session.id))[0]
    elif boundary == "release_task":
        await store.claim_task(session.id, task_id, "worker")
        task = await store.release_task(session.id, task_id)
    elif boundary == "release_completed_task":
        await store.update_task(session.id, task_id, TaskPatch(status="completed"))
        task = await store.release_task(session.id, task_id)

    task.id = "caller mutation"
    task.subject = "caller mutation"
    task.metadata["nested"].append("caller mutation")
    task.blocks.append("caller mutation")
    task.blocked_by.append("caller mutation")
    reloaded = await store.get_task(session.id, task_id)
    assert reloaded.id == task_id
    assert reloaded.subject == "before"
    assert reloaded.metadata == {"nested": ["before"]}
    assert reloaded.blocks == reloaded.blocked_by == []


@pytest.mark.parametrize("boundary", ["update_task"])
async def test_session_store_detaches_write_inputs(store_case, boundary) -> None:
    store = store_case
    values = ["before"]
    if boundary == "create":
        session = await store.create(meta={"nested": values})
    else:
        session = await store.create()
    task_id = None
    if boundary == "update_meta":
        await store.update_meta(session.id, {"nested": values})
    elif boundary == "set_invoked_skills":
        await store.set_invoked_skills(session.id, [{"nested": values}])
    elif boundary in ("create_task", "update_task"):
        task = await store.create_task(
            session.id, CreateTaskInput(subject="task", description="", metadata={"nested": values})
        )
        task_id = task.id
        if boundary == "update_task":
            values = ["before"]
            await store.update_task(session.id, task_id, TaskPatch(metadata={"nested": values}))

    values.append("caller mutation")
    if task_id is not None:
        assert (await store.get_task(session.id, task_id)).metadata == {"nested": ["before"]}
    elif boundary == "set_invoked_skills":
        assert (await store.load(session.id)).invoked_skills == [{"nested": ["before"]}]
    else:
        assert (await store.load(session.id)).meta == {"nested": ["before"]}


@pytest.mark.parametrize("kind", ["postgres"])
async def test_store_round_trip(kind: str, pg_dsn: str) -> None:
    store = InMemorySessionStore() if kind == "memory" else PostgresSessionStore(pg_dsn)
    rec = await store.create(meta={"title": "x"})
    await store.append_messages(rec.id, [Message(role="user", content=[TextBlock(text="hi")])])

    rows = await store.load_messages(rec.id)

    assert rows[0].seq == 1
    assert isinstance(rows[0].message.content[0], TextBlock)
    assert rows[0].message.content[0].text == "hi"
    await store.delete(rec.id)
    assert await store.load(rec.id) is None
    await store.close()


@pytest.mark.parametrize("kind", ["memory", "postgres"])
async def test_create_if_absent_has_one_concurrent_owner(kind: str, pg_dsn: str) -> None:
    store = InMemorySessionStore() if kind == "memory" else PostgresSessionStore(pg_dsn)
    session_id = "create-if-absent-concurrent"
    try:
        records = await asyncio.gather(
            *(
                store.create_if_absent(id=session_id, meta={"contender": index})
                for index in range(50)
            )
        )
        winners = [record for record in records if record is not None]
        assert len(winners) == 1
        loaded = await store.load(session_id)
        assert loaded is not None and loaded.meta == winners[0].meta
    finally:
        await store.close()
