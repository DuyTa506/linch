from agent_kit.sessions import InMemorySessionStore, SqliteSessionStore
from agent_kit.types import Message, TextBlock


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
