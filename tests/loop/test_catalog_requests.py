from __future__ import annotations

import json
import platform
from pathlib import Path
from typing import Any

import pytest

from tests.support.agents import make_agent
from tests.support.providers import ScriptedProvider, TextTurn, ToolUseTurn
from tests.support.tools import StubTool, tool_registry


def _skill_file(root: Path, name: str, description: str) -> None:
    directory = root / ".linch" / "skills" / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"---\ndescription: {description}\n---\n# {name}\n",
        encoding="utf-8",
    )


def _request_prefix(request: Any) -> bytes:
    payload = {
        "system": [
            {"text": block.text, "cacheable": block.cacheable, "type": block.type}
            for block in request.system
        ],
        "tools": request.tools,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


async def _run(session: Any, prompt: str) -> list[Any]:
    return [event async for event in session.run(prompt)]


@pytest.mark.asyncio
async def test_two_runs_have_one_skill_listing_block_and_no_prompt_listing(tmp_path: Path) -> None:
    from linch.config import FeatureFlags

    _skill_file(tmp_path, "alpha", "The alpha workflow skill for this test.")
    provider = ScriptedProvider([TextTurn("first"), TextTurn("second")])
    agent = make_agent(
        provider,
        cwd=str(tmp_path),
        config_dir=".linch",
        features=FeatureFlags(skills=True, subagents=False, mcp=False),
    )
    session = await agent.session(id="skill-listing")

    await _run(session, "first prompt")
    await _run(session, "second prompt")

    listing = agent.skill_listing_text
    assert listing
    assert all(
        sum(listing in block.text for block in request.system) == 1 for request in provider.requests
    )
    assert all(
        not any(
            listing in getattr(block, "text", "")
            for message in request.messages
            for block in message.content
        )
        for request in provider.requests
    )
    await agent.close()


@pytest.mark.asyncio
async def test_stable_catalog_keeps_system_and_tools_bytes_identical_across_turns() -> None:
    provider = ScriptedProvider([TextTurn("first"), TextTurn("second")])
    agent = make_agent(
        provider,
        tools=tool_registry(StubTool("Read")),
        run_store=None,
    )
    session = await agent.session(id="stable-prefix")

    await _run(session, "first")
    await _run(session, "second")

    assert len(provider.requests) == 2
    assert _request_prefix(provider.requests[0]) == _request_prefix(provider.requests[1])
    await agent.close()


@pytest.mark.asyncio
async def test_restart_uses_persisted_prefix_when_platform_changes(monkeypatch: Any) -> None:
    from linch.sessions import InMemorySessionStore

    store = InMemorySessionStore()
    first_provider = ScriptedProvider([TextTurn("first")])
    first_agent = make_agent(
        first_provider,
        tools=tool_registry(StubTool("Read")),
        session_store=store,
        run_store=None,
    )
    first_session = await first_agent.session(id="restart-prefix")
    await _run(first_session, "first")
    first_prefix = _request_prefix(first_provider.requests[0])
    await first_agent.close()

    monkeypatch.setattr(platform, "system", lambda: "DifferentOS")
    monkeypatch.setattr(platform, "release", lambda: "99.0")
    monkeypatch.setattr(platform, "machine", lambda: "different-arch")
    monkeypatch.setattr(platform, "python_version", lambda: "99.0.0")

    second_provider = ScriptedProvider([TextTurn("second")])
    second_agent = make_agent(
        second_provider,
        tools=tool_registry(StubTool("Read")),
        session_store=store,
        run_store=None,
    )
    second_session = await second_agent.session(id="restart-prefix")
    await _run(second_session, "second")

    assert _request_prefix(second_provider.requests[0]) == first_prefix
    await second_agent.close()


@pytest.mark.asyncio
async def test_added_tool_is_appended_and_emits_one_tool_set_advisory() -> None:
    from linch.events import PromptCacheAdvisoryEvent

    provider = ScriptedProvider([TextTurn("first"), TextTurn("second")])
    registry = tool_registry(StubTool("Read"), StubTool("Write"))
    agent = make_agent(provider, tools=registry, run_store=None)
    session = await agent.session(id="tool-added")
    await _run(session, "first")
    agent.tools.register(StubTool("Append"))

    events = await _run(session, "second")

    before = [tool["name"] for tool in provider.requests[0].tools]
    after = [tool["name"] for tool in provider.requests[1].tools]
    assert after[: len(before)] == before
    assert after[-1] == "Append"
    advisories = [
        event
        for event in events
        if isinstance(event, PromptCacheAdvisoryEvent) and event.reason == "tool_set_changed"
    ]
    assert len(advisories) == 1
    await agent.close()


@pytest.mark.asyncio
async def test_removed_tool_keeps_schema_but_unknown_call_returns_error() -> None:
    provider = ScriptedProvider(
        [TextTurn("first"), ToolUseTurn("Write", {}), TextTurn("recovered")]
    )
    registry = tool_registry(StubTool("Read"), StubTool("Write"))
    agent = make_agent(provider, tools=registry, run_store=None)
    session = await agent.session(id="tool-removed")
    await _run(session, "first")
    previous_tools = provider.requests[0].tools
    agent.tools.unregister("Write")

    events = await _run(session, "second")

    assert provider.requests[1].tools == previous_tools
    tool_end = next(event for event in events if event.type == "tool_call_end")
    assert tool_end.tool_result is not None
    assert tool_end.tool_result.is_error is True
    assert "not registered" in tool_end.tool_result.content
    await agent.close()


@pytest.mark.asyncio
async def test_description_only_tool_change_keeps_frozen_schema() -> None:
    provider = ScriptedProvider([TextTurn("first"), TextTurn("second")])
    registry = tool_registry(StubTool("Read"))
    agent = make_agent(provider, tools=registry, run_store=None)
    session = await agent.session(id="tool-description")
    await _run(session, "first")
    replacement = StubTool("Read")
    replacement.description = "A changed description that should be notified"
    agent.tools.replace(replacement)

    await _run(session, "second")

    assert provider.requests[1].tools == provider.requests[0].tools
    await agent.close()


@pytest.mark.asyncio
async def test_catalog_notification_survives_session_store_reload() -> None:
    from linch.sessions import InMemorySessionStore
    from linch.types import CatalogChangeBlock

    store = InMemorySessionStore()
    first_provider = ScriptedProvider([TextTurn("first"), TextTurn("second")])
    registry = tool_registry(StubTool("Read"))
    first_agent = make_agent(
        first_provider,
        tools=registry,
        session_store=store,
        run_store=None,
    )
    first_session = await first_agent.session(id="notification-reload")
    await _run(first_session, "first")
    first_agent.tools.register(StubTool("Append"))
    await _run(first_session, "second")
    await first_agent.close()

    second_agent = make_agent(
        ScriptedProvider([TextTurn("unused")]),
        tools=tool_registry(StubTool("Read")),
        session_store=store,
        run_store=None,
    )
    reloaded = await second_agent.session(id="notification-reload")

    assert any(
        isinstance(block, CatalogChangeBlock)
        for message in reloaded.provider_view
        for block in message.content
    )
    await second_agent.close()
