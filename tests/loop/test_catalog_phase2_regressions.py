"""Focused regressions for the phase-2 catalog snapshot contract."""

from __future__ import annotations

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
async def test_reset_with_no_pending_notifications_does_not_write_meta() -> None:
    from linch.catalog import reset_catalog_notifications, sync_catalog

    registry = _Registry([_tool("first")])
    session = _Session(registry)
    await sync_catalog(session, _config(registry))
    session.meta_updates.clear()

    assert not await reset_catalog_notifications(session)
    assert session.meta_updates == []
