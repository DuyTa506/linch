from linch.sessions import InMemorySessionStore, SqliteSessionStore
from linch.sessions.tasks import CreateTaskInput
from linch.types import Message, TextBlock


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
