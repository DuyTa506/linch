from linch.sessions import InMemorySessionStore, SqliteSessionStore
from linch.sessions.tasks import CreateTaskInput
from linch.types import (
    ImageBlock,
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)


def _delete_task_counter(conn, session_id: str) -> None:
    conn.execute("delete from task_counters where session_id = ?", (session_id,))
    conn.commit()


def _seed_orphan_task_counter(conn, session_id: str) -> None:
    conn.execute(
        "insert into task_counters (session_id, next_id) values (?, ?)",
        (session_id, 9),
    )
    conn.commit()


async def test_memory_store_round_trip() -> None:
    store = InMemorySessionStore()
    rec = await store.create(meta={"title": "x"})
    await store.append_messages(rec.id, [Message(role="user", content=[TextBlock(text="hi")])])

    rows = await store.load_messages(rec.id)

    assert rows[0].seq == 1
    assert isinstance(rows[0].message.content[0], TextBlock)
    assert rows[0].message.content[0].text == "hi"


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


async def test_sqlite_store_round_trip(tmp_path) -> None:
    store = SqliteSessionStore(tmp_path / "sessions.db")
    rec = await store.create(meta={"title": "x"})
    await store.append_messages(rec.id, [Message(role="user", content=[TextBlock(text="hi")])])

    rows = await store.load_messages(rec.id)

    assert rows[0].seq == 1
    assert isinstance(rows[0].message.content[0], TextBlock)
    assert rows[0].message.content[0].text == "hi"
    await store.close()


async def test_memory_missing_task_counter_continues_after_highest_numeric_id() -> None:
    store = InMemorySessionStore()
    rec = await store.create()
    first = await store.create_task(rec.id, CreateTaskInput(subject="one", description=""))
    second = await store.create_task(rec.id, CreateTaskInput(subject="two", description=""))
    store._task_counter.pop(rec.id)

    third = await store.create_task(rec.id, CreateTaskInput(subject="three", description=""))

    assert [first.id, second.id, third.id] == ["1", "2", "3"]


async def test_sqlite_missing_task_counter_continues_after_highest_numeric_id(tmp_path) -> None:
    store = SqliteSessionStore(tmp_path / "missing-counter.db")
    try:
        rec = await store.create()
        await store.create_task(rec.id, CreateTaskInput(subject="one", description=""))
        await store.create_task(rec.id, CreateTaskInput(subject="two", description=""))
        await store._exec.run(lambda conn: _delete_task_counter(conn, rec.id))

        third = await store.create_task(rec.id, CreateTaskInput(subject="three", description=""))

        assert third.id == "3"
    finally:
        await store.close()


async def test_sqlite_create_if_absent_tolerates_orphan_task_counter(tmp_path) -> None:
    store = SqliteSessionStore(tmp_path / "orphan-counter.db")
    try:
        await store._exec.run(lambda conn: _seed_orphan_task_counter(conn, "fork-target"))

        created = await store.create_if_absent(id="fork-target", meta={})

        assert created is not None
    finally:
        await store.close()
