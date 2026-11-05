"""Catalog edits preserve schema order and correctly handle restored tools."""

from __future__ import annotations

from tests.support.agents import make_agent
from tests.support.providers import ScriptedProvider, TextTurn
from tests.support.tools import StubTool, tool_registry


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
