import asyncio

import pytest

from linch.sessions import InMemorySessionStore
from linch.sessions.postgres import PostgresSessionStore
from linch.sessions.tasks import CreateTaskInput, TaskPatch
from linch.types import (
    ImageBlock,
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
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
    "boundary", ["create", "create_if_absent", "create_existing", "load", "list", "update_meta"]
)
async def test_session_records_are_detached(store_case, boundary) -> None:
    from uuid import uuid4

    store = store_case
    meta = {"nested": {"labels": ["before"]}}
    if boundary == "create_if_absent":
        record = await store.create_if_absent(id=str(uuid4()), meta=meta)
    else:
        record = await store.create(meta=meta)
    session_id = record.id
    await store.set_invoked_skills(session_id, [{"name": "before"}])
    if boundary == "create_existing":
        record = await store.create(id=session_id)
    elif boundary == "load":
        record = await store.load(session_id)
    elif boundary == "list":
        record = (await store.list())[0]
    elif boundary == "update_meta":
        record = await store.update_meta(session_id, {"extra": "value"})

    record.id = "caller mutation"
    record.meta["nested"]["labels"].append("caller mutation")
    if record.invoked_skills:
        record.invoked_skills[0]["name"] = "caller mutation"
    record.invoked_skills.append({"name": "caller mutation"})
    reloaded = await store.load(session_id)
    assert reloaded.id == session_id
    assert reloaded.meta["nested"] == {"labels": ["before"]}
    assert reloaded.invoked_skills == [{"name": "before"}]


@pytest.mark.parametrize(
    "boundary",
    [
        "create_task",
        "get_task",
        "list_tasks",
        "update_task",
        "claim_task",
        "ready_tasks",
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


@pytest.mark.parametrize(
    "boundary", ["create", "update_meta", "set_invoked_skills", "create_task", "update_task"]
)
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


@pytest.mark.parametrize("kind", ["memory", "postgres"])
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
async def test_multiple_sessions_concurrent_writes(kind: str, pg_dsn: str) -> None:
    """Ten sessions sharing one store retain their own five messages."""
    store = InMemorySessionStore() if kind == "memory" else PostgresSessionStore(pg_dsn)
    try:
        records = await asyncio.gather(*(store.create() for _ in range(10)))
        assert len({record.id for record in records}) == 10

        async def write_session(session_id: str) -> None:
            message = Message(role="user", content=[TextBlock(text=session_id)])
            for _ in range(50):
                await store.append_messages(session_id, [message])

        await asyncio.gather(*(write_session(record.id) for record in records))

        for record in records:
            rows = await store.load_messages(record.id)
            assert len(rows) == 50, f"session {record.id} expected 50 messages, got {len(rows)}"
            assert [row.seq for row in rows] == list(range(1, 51))
            for row in rows:
                block = row.message.content[0]
                assert isinstance(block, TextBlock)
                assert block.text == record.id
    finally:
        await store.close()


@pytest.mark.parametrize("kind", ["memory", "postgres"])
async def test_concurrent_task_ids_are_unique(kind: str, pg_dsn: str) -> None:
    store = InMemorySessionStore() if kind == "memory" else PostgresSessionStore(pg_dsn)
    try:
        session = await store.create()
        tasks = await asyncio.gather(
            *(
                store.create_task(
                    session.id,
                    CreateTaskInput(subject=f"task-{index}", description="concurrent"),
                )
                for index in range(50)
            )
        )
        assert sorted(int(task.id) for task in tasks) == list(range(1, 51))
        assert len({task.id for task in tasks}) == 50
    finally:
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


async def test_memory_store_deeply_isolates_ordinary_message_graph_at_every_boundary() -> None:
    store = InMemorySessionStore()
    rec = await store.create()
    message = Message(
        role="assistant",
        content=[
            ToolUseBlock(
                id="call-1",
                name="ordinary_tool",
                input={"nested": {"image": {"data": "before"}}},
            ),
            ToolResultBlock(
                tool_use_id="call-1",
                content=[ImageBlock(source={"media_type": "image/png", "data": "before"})],
            ),
        ],
        provider_metadata={"nested": {"value": "before"}},
    )

    returned = await store.append_messages(rec.id, [message])

    assert message.provider_metadata is not None
    message.provider_metadata["nested"]["value"] = "caller mutation"
    caller_tool = message.content[0]
    assert isinstance(caller_tool, ToolUseBlock)
    caller_tool.input["nested"]["image"]["data"] = "caller mutation"

    returned_message = returned[0].message
    assert returned_message.provider_metadata is not None
    returned_message.provider_metadata["nested"]["value"] = "returned mutation"
    returned_tool = returned_message.content[0]
    assert isinstance(returned_tool, ToolUseBlock)
    returned_tool.input["nested"]["image"]["data"] = "returned mutation"

    loaded = (await store.load_messages(rec.id))[0].message
    assert loaded.provider_metadata == {"nested": {"value": "before"}}
    loaded_tool = loaded.content[0]
    assert isinstance(loaded_tool, ToolUseBlock)
    assert loaded_tool.input == {"nested": {"image": {"data": "before"}}}

    assert loaded.provider_metadata is not None
    loaded.provider_metadata["nested"]["value"] = "loaded mutation"
    loaded_tool.input["nested"]["image"]["data"] = "loaded mutation"

    reloaded = (await store.load_messages(rec.id))[0].message
    assert reloaded.provider_metadata == {"nested": {"value": "before"}}
    reloaded_tool = reloaded.content[0]
    assert isinstance(reloaded_tool, ToolUseBlock)
    assert reloaded_tool.input == {"nested": {"image": {"data": "before"}}}


async def test_memory_missing_task_counter_continues_after_highest_numeric_id() -> None:
    store = InMemorySessionStore()
    rec = await store.create()
    first = await store.create_task(rec.id, CreateTaskInput(subject="one", description=""))
    second = await store.create_task(rec.id, CreateTaskInput(subject="two", description=""))
    store._task_counter.pop(rec.id)

    third = await store.create_task(rec.id, CreateTaskInput(subject="three", description=""))

    assert [first.id, second.id, third.id] == ["1", "2", "3"]


async def test_postgres_missing_task_counter_continues_after_highest_numeric_id(
    pg_dsn,
) -> None:
    store = PostgresSessionStore(pg_dsn)
    try:
        rec = await store.create()
        await store.create_task(rec.id, CreateTaskInput(subject="one", description=""))
        await store.create_task(rec.id, CreateTaskInput(subject="two", description=""))
        pool = await store._ensure()
        await pool.execute("DELETE FROM task_counters WHERE session_id = $1", rec.id)

        third = await store.create_task(rec.id, CreateTaskInput(subject="three", description=""))

        assert third.id == "3"
    finally:
        await store.close()


async def test_postgres_create_if_absent_tolerates_orphan_task_counter(
    pg_dsn,
) -> None:
    store = PostgresSessionStore(pg_dsn)
    try:
        pool = await store._ensure()
        await pool.execute(
            "INSERT INTO task_counters (session_id, next_id) VALUES ($1, $2)",
            "fork-target",
            9,
        )

        created = await store.create_if_absent(id="fork-target", meta={})

        assert created is not None
    finally:
        await store.close()
