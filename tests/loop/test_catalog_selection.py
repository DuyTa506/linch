from __future__ import annotations

from typing import Any, cast

import pytest

from tests.support.agents import make_agent
from tests.support.providers import ScriptedProvider, TextTurn
from tests.support.tools import StubTool, tool_registry


def _tool(name: str, tags: tuple[str, ...] = ()) -> StubTool:
    tool = StubTool(name, parallel=True)
    cast(Any, tool).tags = tags
    return tool


async def _request_fixture() -> tuple[Any, Any, Any]:
    from linch.catalog import sync_catalog
    from linch.config import FeatureFlags
    from linch.profiles import _base_run_config

    agent = make_agent(
        ScriptedProvider([TextTurn("done")]),
        tools=tool_registry(_tool("SearchDocs", ("rag",)), _tool("WriteNote")),
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        result_offload=None,
    )
    session = await agent.session()
    config = _base_run_config(session)
    await sync_catalog(session, config, None)
    return agent, session, config


def _opts() -> Any:
    from linch.session import RunOptions

    return RunOptions()


@pytest.mark.asyncio
async def test_snapshot_filters_tools_selected_by_context_tag() -> None:
    from linch import ContextBuildResult
    from linch.loop.request import _build_turn_request

    _, session, config = await _request_fixture()
    request = _build_turn_request(
        session,
        _opts(),
        context=ContextBuildResult(selected_tools={"tags": ["rag"]}),
        config=config,
    )

    assert [schema["name"] for schema in request.tools] == ["SearchDocs"]


@pytest.mark.asyncio
async def test_snapshot_filters_tools_selected_by_registry() -> None:
    from linch import ContextBuildResult
    from linch.loop.request import _build_turn_request

    agent, session, config = await _request_fixture()
    selected = agent.tools.select(names={"WriteNote"})
    request = _build_turn_request(
        session,
        _opts(),
        context=ContextBuildResult(selected_tools=selected),
        config=config,
    )

    assert [schema["name"] for schema in request.tools] == ["WriteNote"]


@pytest.mark.asyncio
async def test_snapshot_intersects_allowed_tools_with_context_selection() -> None:
    from linch import ContextBuildResult
    from linch.loop.request import _build_turn_request

    _, session, config = await _request_fixture()
    session.current_turn_allowed_tools = ["SearchDocs"]
    request = _build_turn_request(
        session,
        _opts(),
        context=ContextBuildResult(selected_tools={"names": ["WriteNote"]}),
        config=config,
    )

    assert request.tools == []


@pytest.mark.asyncio
async def test_snapshot_keeps_volatile_context_system_blocks_uncacheable() -> None:
    from linch import ContextBuildResult
    from linch.loop.request import _build_turn_request
    from linch.types import SystemBlock

    _, session, config = await _request_fixture()
    request = _build_turn_request(
        session,
        _opts(),
        context=ContextBuildResult(system_blocks=[SystemBlock(text="fresh", cacheable=True)]),
        config=config,
    )

    assert request.system[-1].text == "fresh"
    assert request.system[-1].cacheable is False


@pytest.mark.asyncio
async def test_snapshot_without_context_selection_keeps_frozen_tools() -> None:
    from linch.loop.request import _build_turn_request

    agent, session, config = await _request_fixture()
    agent.tools.unregister("WriteNote")
    request = _build_turn_request(session, _opts(), config=config)

    assert [schema["name"] for schema in request.tools] == ["SearchDocs", "WriteNote"]
