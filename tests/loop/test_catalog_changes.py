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


@pytest.mark.asyncio
async def test_sync_catalog_creates_durable_prefix_snapshot() -> None:
    from linch.catalog import sync_catalog

    registry = _Registry([_tool("first")])
    session = _Session(registry)

    await sync_catalog(session, _config(registry), None)

    snapshot = session.meta["linch.prefix"]
    assert snapshot["version"] == 1
    assert [tool["name"] for tool in snapshot["tools"]] == ["first"]
    assert set(snapshot["skills"]) == {"demo"}


@pytest.mark.asyncio
async def test_tool_addition_is_appended_and_notified() -> None:
    from linch.catalog import sync_catalog

    registry = _Registry([_tool("first")])
    session = _Session(registry)
    config = _config(registry)
    await sync_catalog(session, config, None)

    registry.tools.append(_tool("second"))
    registry.generation += 1
    session.agent._catalog_revision += 1
    await sync_catalog(session, config, None)

    snapshot = session.meta["linch.prefix"]
    assert [tool["name"] for tool in snapshot["tools"]] == ["first", "second"]
    assert len(session.appended) == 1
    assert "second" in session.appended[0].content[0].text


@pytest.mark.asyncio
async def test_removed_tool_keeps_snapshot_schema_and_notifies() -> None:
    from linch.catalog import sync_catalog

    registry = _Registry([_tool("first"), _tool("second")])
    session = _Session(registry)
    config = _config(registry)
    await sync_catalog(session, config, None)

    registry.tools.pop()
    registry.generation += 1
    session.agent._catalog_revision += 1
    await sync_catalog(session, config, None)

    assert [tool["name"] for tool in session.meta["linch.prefix"]["tools"]] == [
        "first",
        "second",
    ]
    assert "second" in session.appended[0].content[0].text


def test_snapshot_request_uses_frozen_system_and_tools() -> None:
    from linch.loop.request import _build_turn_request

    registry = _Registry([_tool("live", description="changed")])
    session = _Session(registry)
    session.meta["linch.prefix"] = {
        "version": 1,
        "system": [{"text": "frozen", "cacheable": True}],
        "tools": [{"name": "old", "description": "old", "input_schema": {"type": "object"}}],
        "skills": {},
        "notified": {"tools": {}, "subagents": None},
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
async def test_compaction_snapshot_rebuild_clears_pending_catalog_notice() -> None:
    from linch.catalog import sync_catalog
    from linch.compaction import _run_compaction_impl
    from linch.loop.request import _re_inject_skill_context
    from linch.profiles import _base_run_config
    from linch.types import CatalogChangeBlock
    from tests.support.agents import make_agent
    from tests.support.providers import ScriptedProvider
    from tests.support.tools import StubTool, tool_registry

    registry = tool_registry(StubTool("first", output="old"), StubTool("second"))
    agent = make_agent(ScriptedProvider([]), tools=registry)
    session = await agent.session()
    config = _base_run_config(session)
    await sync_catalog(session, config, None)
    replacement = StubTool("first", output="new")
    replacement.description = "new description"
    agent.tools.replace(replacement)
    agent.tools.unregister("second")
    agent._catalog_revision += 1
    await sync_catalog(session, config, None)

    before = session.meta["linch.prefix"]["tools"]
    assert before[0]["description"] == "first test tool"
    assert [tool["name"] for tool in before] == ["first", "second"]

    class _DropNotice:
        id = "drop-notice"

        async def compact(self, ctx, _provider):
            return [
                message
                for message in ctx.messages
                if not any(isinstance(block, CatalogChangeBlock) for block in message.content)
            ]

    await _run_compaction_impl(
        session,
        agent,
        signal=__import__("linch.abort", fromlist=["AbortContext"]).AbortContext(),
        strategy=_DropNotice(),
        _run_config=config,
    )
    await _re_inject_skill_context(session, config=config)

    promoted = session.meta["linch.prefix"]["tools"]
    assert promoted == [
        {
            "name": "first",
            "description": "new description",
            "input_schema": {"type": "object"},
        }
    ]
    assert not any(
        isinstance(block, CatalogChangeBlock)
        for message in session.provider_view
        for block in message.content
    )


@pytest.mark.asyncio
async def test_reinject_is_durable_and_skips_listing_for_a_prefix_snapshot() -> None:
    from linch.loop.request import _re_inject_skill_context
    from linch.types import InvokedSkillRecord

    registry = _Registry([])
    session = _Session(registry)
    session.agent.skills = {}
    session.meta["linch.prefix"] = {
        "version": 1,
        "system": [{"text": "- demo: a demo skill", "cacheable": True}],
        "tools": [],
        "skills": {},
        "notified": {"tools": {}, "subagents": None},
    }
    session.invoked_skills = [
        InvokedSkillRecord(name="demo", substituted_body="demo body", invoked_at=1.0)
    ]

    await _re_inject_skill_context(session, config=_config(registry))

    assert len(session.appended) == 1
    assert "demo body" in session.appended[0].content[0].text
    assert "- demo: a demo skill" not in session.appended[0].content[0].text


@pytest.mark.asyncio
async def test_compaction_with_stable_catalog_keeps_snapshot_bytes() -> None:
    from linch.catalog import sync_catalog
    from linch.compaction import _run_compaction_impl
    from linch.profiles import _base_run_config
    from tests.support.agents import make_agent
    from tests.support.providers import ScriptedProvider
    from tests.support.tools import StubTool, tool_registry

    registry = tool_registry(StubTool("first"))
    agent = make_agent(ScriptedProvider([]), tools=registry)
    session = await agent.session()
    config = _base_run_config(session)
    await sync_catalog(session, config, None)
    before = repr(session.meta["linch.prefix"])

    class _Noop:
        id = "noop"

        async def compact(self, ctx, _provider):
            return ctx.messages

    await _run_compaction_impl(
        session,
        agent,
        signal=__import__("linch.abort", fromlist=["AbortContext"]).AbortContext(),
        strategy=_Noop(),
        _run_config=config,
    )

    assert repr(session.meta["linch.prefix"]) == before


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


async def test_skill_body_change_notifies_without_exposing_the_body(tmp_path) -> None:
    from linch.config import FeatureFlags
    from linch.types import CatalogChangeBlock
    from tests.support.agents import make_agent
    from tests.support.providers import ScriptedProvider, TextTurn

    directory = tmp_path / ".linch" / "skills" / "alpha"
    directory.mkdir(parents=True)
    definition = directory / "SKILL.md"
    frontmatter = "---\ndescription: Alpha workflow skill.\n---\n"
    definition.write_text(frontmatter + "Original private body.", encoding="utf-8")
    agent = make_agent(
        ScriptedProvider([TextTurn("before"), TextTurn("after")]),
        cwd=str(tmp_path),
        features=FeatureFlags(skills=True, subagents=False, mcp=False),
    )
    session = await agent.session()
    [event async for event in session.run("before")]
    definition.write_text(frontmatter + "Changed private body.", encoding="utf-8")
    await agent.reload_skills()

    [event async for event in session.run("after")]

    notices = [
        block
        for message in session.provider_view
        for block in message.content
        if isinstance(block, CatalogChangeBlock)
    ]
    assert len(notices) == 1
    assert "alpha: Alpha workflow skill." in notices[0].text
    assert "private body" not in notices[0].text
