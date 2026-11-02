"""Focused regressions for the phase-2 catalog snapshot contract."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


def _tool(
    name: str,
    description: str = "tool",
    schema: dict[str, Any] | None = None,
    *,
    is_mcp_tool: bool = False,
) -> Any:
    return SimpleNamespace(
        name=name,
        description=description,
        input_schema=schema or {"type": "object"},
        is_mcp_tool=is_mcp_tool,
    )


class _Registry:
    def __init__(self, tools: list[Any]) -> None:
        self.tools = tools
        self.generation = 0

    def list(self) -> list[Any]:
        return list(self.tools)

    def schemas(self) -> list[dict[str, Any]]:
        return [
            {"name": t.name, "description": t.description, "input_schema": t.input_schema}
            for t in self.tools
        ]

    def system_prompt_sections(self, *, active_names: list[str]) -> list[Any]:
        return []

    def get(self, name: str) -> Any:
        return next((tool for tool in self.tools if tool.name == name), None)


class _Session:
    def __init__(self, registry: _Registry) -> None:
        from linch.skills.types import Skill, SkillFrontmatter

        self.meta: dict[str, Any] = {}
        self.provider_view: list[Any] = []
        self.agent = SimpleNamespace(
            skill_listing_text="- demo: Demo skill",
            skills={
                "demo": Skill(
                    name="demo",
                    dir="",
                    body="private body",
                    frontmatter=SkillFrontmatter(name="demo", description="Demo skill"),
                )
            },
            _catalog_revision=0,
        )
        self.tools_override = None
        self.system_blocks_override = None
        self.appended: list[Any] = []
        self.meta_updates: list[dict[str, Any]] = []

    async def append(self, messages: list[Any]) -> None:
        self.appended.extend(messages)
        self.provider_view.extend(messages)

    async def update_meta(self, patch: dict[str, Any]) -> None:
        self.meta_updates.append(patch)
        self.meta.update(patch)


def _config(registry: _Registry, blocks: list[Any] | None = None) -> Any:
    return SimpleNamespace(
        _values=None,
        tools=registry,
        system_blocks_override=None,
        context={},
        _build_system_blocks=lambda names, tool_sections=None: list(blocks or []),
    )


@pytest.mark.asyncio
async def test_snapshot_has_frozen_base_and_notified_catalogs_including_skills() -> None:
    from linch.catalog import sync_catalog

    registry = _Registry([_tool("first")])
    session = _Session(registry)

    await sync_catalog(session, _config(registry))

    snapshot = session.meta["linch.prefix"]
    assert set(snapshot) == {"version", "system", "tools", "base", "notified"}
    assert snapshot["base"] == snapshot["notified"]
    assert set(snapshot["base"]) == {"tools", "skills", "subagents"}
    assert set(snapshot["notified"]["skills"]) == {"demo"}


@pytest.mark.asyncio
async def test_description_notifications_include_new_text_except_mcp() -> None:
    from linch.catalog import sync_catalog
    from linch.types import CatalogChangeBlock

    pytest.importorskip("mcp")
    from mcp.types import Tool as McpTool

    from linch.mcp.tool import make_mcp_tool

    async def _call(_name: str, _input: dict[str, Any], _signal: Any) -> Any:
        raise AssertionError("notification test does not invoke MCP")

    local = _tool("local", "old")
    mcp = make_mcp_tool(
        "server",
        McpTool(name="lookup", description="old mcp", inputSchema={"type": "object"}),
        _call,
    )
    subagent = _tool("Subagent", "old agents")
    registry = _Registry([local, mcp, subagent])
    session = _Session(registry)
    config = _config(registry)
    await sync_catalog(session, config)

    local.description = "new local description"
    mcp.description = "new third-party instructions"
    subagent.description = "new agent types"
    registry.generation += 1
    await sync_catalog(session, config)

    blocks = [
        block
        for message in session.provider_view
        for block in message.content
        if isinstance(block, CatalogChangeBlock)
    ]
    assert len(blocks) == 1
    text = blocks[0].text
    assert "new local description" in text
    assert "new agent types" in text
    assert "mcp__server__lookup" in text
    assert "new third-party instructions" not in text


def test_base_system_inserts_listing_after_the_actual_runtime_block() -> None:
    from linch.catalog import _base_system
    from linch.types import SystemBlock

    registry = _Registry([])
    session = _Session(registry)
    blocks = [
        SystemBlock(text="Runtime: user instructions should stay first"),
        SystemBlock(text="Runtime:\n\n- Tools available: none"),
    ]

    built = _base_system(session, _config(registry, blocks), registry)

    assert [block.text for block in built] == [blocks[0].text, blocks[1].text, "- demo: Demo skill"]


@pytest.mark.asyncio
async def test_sync_crash_recovery_resets_pending_notifications_without_a_notice() -> None:
    from linch.catalog import sync_catalog

    registry = _Registry([_tool("first", "new")])
    session = _Session(registry)
    session.meta["linch.prefix"] = {
        "version": 1,
        "system": [],
        "tools": [{"name": "first", "description": "old", "input_schema": {"type": "object"}}],
        "base": {
            "tools": {"first": "base-digest"},
            "skills": {"demo": "base-skill"},
            "subagents": None,
        },
        "notified": {
            "tools": {"first": "already-notified"},
            "skills": {"demo": "already-notified"},
            "subagents": None,
        },
    }

    await sync_catalog(session, _config(registry))

    assert len(session.appended) == 1
    assert session.meta_updates[0]["linch.prefix"]["notified"] == {
        "tools": {"first": "base-digest"},
        "skills": {"demo": "base-skill"},
        "subagents": None,
    }


@pytest.mark.asyncio
async def test_reset_then_sync_preserves_pending_tool_rows_and_consolidates_notice() -> None:
    from linch.catalog import reset_catalog_notifications, sync_catalog
    from linch.types import CatalogChangeBlock

    first = _tool("first", schema={"type": "object"})
    registry = _Registry([first])
    session = _Session(registry)
    config = _config(registry)
    await sync_catalog(session, config)

    first.input_schema = {"type": "object", "properties": {"count": {"type": "integer"}}}
    registry.tools.append(_tool("second"))
    registry.generation += 1
    await sync_catalog(session, config)
    before = (session.meta["linch.prefix"]["system"], session.meta["linch.prefix"]["tools"])

    session.provider_view.clear()
    assert await reset_catalog_notifications(session, config)
    await sync_catalog(session, config)

    snapshot = session.meta["linch.prefix"]
    assert (snapshot["system"], snapshot["tools"]) == before
    assert (
        sum(
            isinstance(block, CatalogChangeBlock)
            for message in session.provider_view
            for block in message.content
        )
        == 1
    )


@pytest.mark.asyncio
async def test_reset_with_no_pending_notifications_does_not_write_meta() -> None:
    from linch.catalog import reset_catalog_notifications, sync_catalog

    registry = _Registry([_tool("first")])
    session = _Session(registry)
    await sync_catalog(session, _config(registry))
    session.meta_updates.clear()

    assert not await reset_catalog_notifications(session)
    assert session.meta_updates == []


@pytest.mark.asyncio
async def test_description_reversion_still_carries_the_live_description() -> None:
    from linch.catalog import sync_catalog
    from linch.types import CatalogChangeBlock

    tool = _tool("local", "old description")
    registry = _Registry([tool])
    session = _Session(registry)
    config = _config(registry)
    await sync_catalog(session, config)

    tool.description = "new description"
    registry.generation += 1
    await sync_catalog(session, config)
    tool.description = "old description"
    registry.generation += 1
    await sync_catalog(session, config)

    notices = [
        block
        for message in session.provider_view
        for block in message.content
        if isinstance(block, CatalogChangeBlock)
    ]
    notice = notices[-1]
    assert "local: old description" in notice.text


@pytest.mark.asyncio
async def test_pending_added_tool_schema_edit_updates_its_retained_row() -> None:
    from linch.catalog import reset_catalog_notifications, sync_catalog

    registry = _Registry([_tool("first")])
    session = _Session(registry)
    config = _config(registry)
    await sync_catalog(session, config)
    registry.tools.append(_tool("second"))
    registry.generation += 1
    await sync_catalog(session, config)
    session.provider_view.clear()
    await reset_catalog_notifications(session, config)

    registry.tools[-1].input_schema = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
    }
    registry.generation += 1
    await sync_catalog(session, config)

    rows = session.meta["linch.prefix"]["tools"]
    assert rows[1]["input_schema"]["properties"]["path"]["type"] == "string"


@pytest.mark.asyncio
async def test_skill_notice_is_not_repeated_after_restart_and_unrelated_tool_change(
    tmp_path: Path,
) -> None:
    from linch.config import FeatureFlags
    from linch.sessions import InMemorySessionStore
    from linch.types import CatalogChangeBlock
    from tests.support.agents import make_agent
    from tests.support.providers import ScriptedProvider, TextTurn
    from tests.support.tools import StubTool, tool_registry

    skill = tmp_path / ".linch" / "skills" / "alpha"
    skill.mkdir(parents=True)
    definition = skill / "SKILL.md"
    definition.write_text("---\ndescription: Alpha old.\n---\nBody.\n", encoding="utf-8")
    sessions = InMemorySessionStore()
    flags = FeatureFlags(skills=True, subagents=False, mcp=False)
    first = make_agent(
        ScriptedProvider([TextTurn("one"), TextTurn("two")]),
        cwd=str(tmp_path),
        config_dir=".linch",
        features=flags,
        session_store=sessions,
        tools=tool_registry(StubTool("Read")),
    )
    session = await first.session(id="notice-restart")
    [event async for event in session.run("one")]
    definition.write_text("---\ndescription: Alpha new.\n---\nBody.\n", encoding="utf-8")
    await first.reload_skills()
    [event async for event in session.run("two")]
    await first.close()

    second = make_agent(
        ScriptedProvider([TextTurn("three")]),
        cwd=str(tmp_path),
        config_dir=".linch",
        features=flags,
        session_store=sessions,
        tools=tool_registry(StubTool("Read")),
    )
    restored = await second.session(id="notice-restart")
    second.tools.register(StubTool("Append"))
    [event async for event in restored.run("three")]

    notices = [
        block
        for message in restored.provider_view
        for block in message.content
        if isinstance(block, CatalogChangeBlock)
    ]
    assert sum("alpha: Alpha new." in block.text for block in notices) == 1
    assert sum("Append" in block.text for block in notices) == 1
