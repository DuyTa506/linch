"""Catalog edits preserve schema order and correctly handle restored tools."""

from __future__ import annotations

from tests.support.agents import make_agent
from tests.support.providers import ScriptedProvider, TextTurn
from tests.support.tools import StubTool, tool_registry


async def test_multiple_tool_additions_preserve_live_registration_order() -> None:
    provider = ScriptedProvider([TextTurn("before"), TextTurn("after")])
    agent = make_agent(provider, tools=tool_registry(StubTool("original")))
    session = await agent.session()
    [event async for event in session.run("before")]
    agent.tools.register(StubTool("z_added"))
    agent.tools.register(StubTool("a_added"))

    [event async for event in session.run("after")]

    assert [schema["name"] for schema in provider.requests[-1].tools] == [
        "original",
        "z_added",
        "a_added",
    ]


async def test_schema_edit_replaces_tool_in_its_original_slot() -> None:
    provider = ScriptedProvider([TextTurn("before"), TextTurn("after")])
    agent = make_agent(provider, tools=tool_registry(StubTool("first"), StubTool("last")))
    session = await agent.session()
    [event async for event in session.run("before")]
    replacement = StubTool("first")
    replacement.input_schema = {"type": "object", "properties": {"count": {"type": "integer"}}}
    agent.tools.replace(replacement)

    events = [event async for event in session.run("after")]

    schemas = provider.requests[-1].tools
    assert [schema["name"] for schema in schemas] == ["first", "last"]
    assert schemas[0]["input_schema"] == {
        "type": "object",
        "properties": {"count": {"type": "integer"}},
    }
    assert (
        sum(
            event.type == "prompt_cache_advisory" and event.reason == "tool_set_changed"
            for event in events
        )
        == 1
    )


async def test_restored_tool_notifies_and_replaces_retained_schema_without_duplicates() -> None:
    from linch.types import CatalogChangeBlock

    provider = ScriptedProvider([TextTurn("before"), TextTurn("removed"), TextTurn("restored")])
    agent = make_agent(provider, tools=tool_registry(StubTool("first"), StubTool("restored")))
    session = await agent.session()
    [event async for event in session.run("before")]
    agent.tools.unregister("restored")
    [event async for event in session.run("remove")]
    replacement = StubTool("restored")
    replacement.input_schema = {"type": "object", "required": ["path"]}
    agent.tools.register(replacement)

    [event async for event in session.run("restore")]

    schemas = provider.requests[-1].tools
    assert [schema["name"] for schema in schemas] == ["first", "restored"]
    assert schemas[1]["input_schema"] == {"type": "object", "required": ["path"]}
    changes = [
        block
        for message in session.provider_view
        for block in message.content
        if isinstance(block, CatalogChangeBlock)
    ]
    assert len(changes) == 2
    assert changes[-1].added_tools == ("restored",)
