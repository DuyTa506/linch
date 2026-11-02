"""Keep live request assembly and reminder dedupe on snapshot bypass paths."""

from __future__ import annotations

import json
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from tests.support.agents import make_agent
from tests.support.providers import ScriptedProvider, TextTurn
from tests.support.tools import StubTool, tool_registry


@pytest.mark.parametrize("bypass", ["profile", "tools", "system", "session_system", "catalog"])
async def test_catalog_bypass_preserves_live_request_bytes(bypass: str) -> None:
    from linch.catalog import sync_catalog
    from linch.loop.request import _build_turn_request
    from linch.profiles import _base_run_config, _RunConfig
    from linch.session import RunOptions
    from linch.types import SystemBlock

    agent = make_agent(ScriptedProvider([]), tools=tool_registry(StubTool("live")))
    session = await agent.session()
    config = _base_run_config(session)
    if bypass == "profile":
        config = _RunConfig(agent, _values=config._snapshot())
    elif bypass == "tools":
        session.tools_override = tool_registry(StubTool("override"))
    elif bypass == "system":
        values = config._snapshot()
        # The effective config override is independent of the Session override.
        config = SimpleNamespace(
            **values,
            _values=None,
            system_blocks_override=[SystemBlock(text="configured system")],
            _build_system_blocks=config._build_system_blocks,
        )
    elif bypass == "session_system":
        session.system_blocks_override = [SystemBlock(text="session system")]
    else:
        from linch.tools.search import ToolSearchPlugin

        await agent.tool_search.dispose()
        await agent.use(ToolSearchPlugin(max_tools=0))

    baseline = _build_turn_request(session, RunOptions(), config=config)
    expected = json.dumps(
        {"system": [asdict(block) for block in baseline.system], "tools": baseline.tools}
    )
    poisoned = {
        "version": 1,
        "system": [{"text": "stale prefix", "cacheable": True}],
        "tools": [{"name": "stale", "description": "stale", "input_schema": {}}],
        "base": {"tools": {}, "skills": {}, "subagents": None},
        "notified": {"tools": {}, "skills": {}, "subagents": None},
    }
    await session.update_meta({"linch.prefix": poisoned})

    await sync_catalog(session, config, None)
    request = _build_turn_request(session, RunOptions(), config=config)

    assert (
        json.dumps({"system": [asdict(block) for block in request.system], "tools": request.tools})
        == expected
    )
    assert session.meta["linch.prefix"] == poisoned


async def test_bypassed_runs_deduplicate_only_the_exact_skill_reminder() -> None:
    from linch.skills.system_reminder import wrap_in_system_reminder
    from linch.types import Message, SystemBlock, TextBlock

    provider = ScriptedProvider([TextTurn("one"), TextTurn("two")])
    agent = make_agent(provider)
    agent.skill_listing_text = "- demo: a demo workflow"
    session = await agent.session()
    session.system_blocks_override = [SystemBlock(text="explicit system")]
    await session.append([Message(role="user", content=[TextBlock(text="- demo: a demo")])])

    first = [event async for event in session.run("first")]
    second = [event async for event in session.run("second")]

    assert first[-1].subtype == second[-1].subtype == "success"
    reminder = wrap_in_system_reminder("- demo: a demo workflow")
    assert (
        sum(
            block.text == reminder
            for message in provider.requests[-1].messages
            for block in message.content
            if isinstance(block, TextBlock)
        )
        == 1
    )
    assert "linch.prefix" not in session.meta


async def test_unchanged_catalog_does_not_scan_schemas_after_first_turn(monkeypatch) -> None:
    import linch.catalog as catalog
    from linch.profiles import _base_run_config

    agent = make_agent(ScriptedProvider([]), tools=tool_registry(StubTool("stable")))
    session = await agent.session()
    config = _base_run_config(session)
    await catalog.sync_catalog(session, config, None)
    assert "linch.prefix" in session.meta

    def unexpected_diff(*args, **kwargs):
        raise AssertionError("unchanged catalog entered the schema diff")

    monkeypatch.setattr(catalog, "_schemas", unexpected_diff)
    monkeypatch.setattr(catalog, "_digest", unexpected_diff)
    monkeypatch.setattr(catalog.copy, "deepcopy", unexpected_diff)
    monkeypatch.setattr(agent.tools, "list", unexpected_diff)
    await catalog.sync_catalog(session, config, None)
