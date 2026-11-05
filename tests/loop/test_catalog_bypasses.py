"""Keep live request assembly and reminder dedupe on snapshot bypass paths."""

from __future__ import annotations

from tests.support.agents import make_agent
from tests.support.providers import ScriptedProvider, TextTurn


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
