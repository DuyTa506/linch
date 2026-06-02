from __future__ import annotations

from linch import defaultTools
from linch.sessions import InMemorySessionStore, SqliteSessionStore
from linch.sessions.tasks import CreateTaskInput, TaskPatch


def test_default_tools_schema_names_match_typescript_contract() -> None:
    registry = defaultTools()
    tools = {tool.name: tool for tool in registry.list()}

    assert "file_path" in tools["Read"].input_schema["properties"]
    assert "file_path" in tools["Write"].input_schema["properties"]
    assert "file_path" in tools["Edit"].input_schema["properties"]
    assert "old_string" in tools["Edit"].input_schema["properties"]
    assert "glob_pattern" in tools["Glob"].input_schema["properties"]
    assert "target_directory" in tools["Glob"].input_schema["properties"]


async def test_memory_store_persists_tasks_and_skills() -> None:
    store = InMemorySessionStore()
    rec = await store.create(meta={"title": "x"})
    await store.set_invoked_skills(rec.id, [{"name": "s", "substituted_body": "b"}])
    loaded = await store.load(rec.id)
    assert loaded is not None
    assert loaded.invoked_skills[0]["name"] == "s"

    created = await store.create_task(
        rec.id, CreateTaskInput(subject="Ship", description="Do thing")
    )
    assert created.id == "1"
    updated = await store.update_task(
        rec.id,
        created.id,
        TaskPatch(status="in_progress", add_blocks=["2"]),
    )
    assert updated is not None
    assert updated.status == "in_progress"
    assert updated.blocks == ["2"]


async def test_sqlite_store_task_round_trip(tmp_path) -> None:
    store = SqliteSessionStore(tmp_path / "sessions.db")
    rec = await store.create(meta={"title": "x"})
    created = await store.create_task(rec.id, CreateTaskInput(subject="Task", description="Desc"))
    loaded = await store.get_task(rec.id, created.id)
    assert loaded is not None
    assert loaded.subject == "Task"
    await store.close()
