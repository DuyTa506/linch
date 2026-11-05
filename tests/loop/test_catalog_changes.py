"""Phase 2 catalog snapshot and notification contract tests."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest


def _tool(name: str, description: str = "tool", schema: dict[str, Any] | None = None) -> Any:
    return SimpleNamespace(
        name=name,
        description=description,
        input_schema=schema or {"type": "object"},
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
            tools=registry,
            skill_listing_text="- demo: a demo skill",
            skills={
                "demo": Skill(
                    name="demo",
                    dir="",
                    body="",
                    frontmatter=SkillFrontmatter(name="demo", description="a demo skill"),
                )
            },
            _catalog_revision=0,
        )
        self.tools_override = None
        self.system_blocks_override = None
        self.appended: list[Any] = []

        class _Log:
            def append(inner, message: Any, *, historical: bool = True) -> None:
                self.provider_view.append(message)

        self.session_log = _Log()

    async def append(self, messages: list[Any]) -> None:
        self.appended.extend(messages)
        self.provider_view.extend(messages)

    async def update_meta(self, patch: dict[str, Any]) -> None:
        self.meta.update(patch)


def _config(registry: _Registry) -> Any:
    return SimpleNamespace(
        _values=None,
        tools=registry,
        system_blocks_override=None,
        context={},
        output_schema=None,
        include_partial_messages=False,
        _build_system_blocks=lambda names, tool_sections=None: [],
    )


def test_snapshot_request_uses_frozen_system_and_tools() -> None:
    from linch.loop.request import _build_turn_request

    registry = _Registry([_tool("live", description="changed")])
    session = _Session(registry)
    session.meta["linch.prefix"] = {
        "version": 1,
        "system": [{"text": "frozen", "cacheable": True}],
        "tools": [{"name": "old", "description": "old", "input_schema": {"type": "object"}}],
        "base": {"tools": {"old": "digest"}, "skills": {}, "subagents": None},
        "notified": {"tools": {"old": "digest"}, "skills": {}, "subagents": None},
    }
    session.agent = SimpleNamespace(
        model="model",
        tools=registry,
        provider=SimpleNamespace(),
        context={},
        output_schema=None,
        include_partial_messages=False,
        max_output_tokens=None,
        tool_choice=None,
        max_retries=1,
        cache_ttl=None,
    )
    opts = SimpleNamespace(
        max_output_tokens=None,
        temperature=None,
        stream_partials=None,
        thinking=None,
        effort=None,
        output_schema=None,
        tool_choice=None,
    )
    req = _build_turn_request(session, opts)
    assert [block.text for block in req.system] == ["frozen"]
    assert req.tools[0]["name"] == "old"


@pytest.mark.asyncio
async def test_catalog_sync_repeats_at_most_once_after_meta_write_crash() -> None:
    from linch.catalog import sync_catalog

    registry = _Registry([_tool("first")])
    session = _Session(registry)
    config = _config(registry)
    await sync_catalog(session, config, None)
    registry.tools.append(_tool("second"))
    registry.generation += 1
    session.agent._catalog_revision += 1

    real_update = session.update_meta
    failed = True

    async def fail_once(patch: dict[str, Any]) -> None:
        nonlocal failed
        if failed:
            failed = False
            raise RuntimeError("simulated crash")
        await real_update(patch)

    session.update_meta = fail_once
    with pytest.raises(RuntimeError, match="simulated crash"):
        await sync_catalog(session, config, None)
    assert len(session.appended) == 1
    session.update_meta = real_update
    await sync_catalog(session, config, None)
    assert len(session.appended) == 2
    assert "second" in session.appended[-1].content[0].text


@pytest.mark.asyncio
async def test_reload_skills_notifies_and_new_skill_can_run_next_request(tmp_path) -> None:
    from linch.config import FeatureFlags
    from tests.support.agents import make_agent
    from tests.support.providers import ScriptedProvider, TextTurn, ToolUseTurn

    root = tmp_path / ".linch" / "skills"
    alpha = root / "alpha"
    alpha.mkdir(parents=True)
    (alpha / "SKILL.md").write_text(
        "---\ndescription: Alpha workflow skill.\n---\nAlpha body.\n", encoding="utf-8"
    )
    provider = ScriptedProvider(
        [
            TextTurn("first"),
            ToolUseTurn("Skill", {"skill": "beta", "args": ""}),
            TextTurn("done"),
        ]
    )
    agent = make_agent(
        provider,
        cwd=str(tmp_path),
        config_dir=".linch",
        features=FeatureFlags(skills=True, subagents=False, mcp=False),
    )
    session = await agent.session()
    [event async for event in session.run("first")]

    (alpha / "SKILL.md").unlink()
    alpha.rmdir()
    beta = root / "beta"
    beta.mkdir()
    (beta / "SKILL.md").write_text(
        "---\ndescription: Beta workflow skill.\n---\nBeta body.\n", encoding="utf-8"
    )
    await agent.reload_skills()
    events = [event async for event in session.run("second")]

    assert any(
        "beta" in getattr(block, "text", "")
        for message in session.provider_view
        for block in message.content
    )
    assert len(provider.requests) == 3
    first_request, second_request = provider.requests[0], provider.requests[1]
    assert [block.text for block in second_request.system] == [
        block.text for block in first_request.system
    ]
    assert second_request.tools == first_request.tools
    from linch.types import CatalogChangeBlock

    assert (
        sum(
            isinstance(block, CatalogChangeBlock)
            for message in second_request.messages
            for block in message.content
        )
        == 1
    )
    notice = next(
        block
        for message in second_request.messages
        for block in message.content
        if isinstance(block, CatalogChangeBlock)
    )
    assert "beta: Beta workflow skill." in notice.text
    assert "Beta body." not in notice.text
    tool_events = [event for event in events if event.type == "tool_call_end"]
    assert tool_events and tool_events[0].is_error is False
