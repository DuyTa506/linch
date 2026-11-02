from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from tests.support.modules import load_module
from tests.support.providers import ScriptedProvider, TextTurn, ToolUseTurn

_EXAMPLES = Path(__file__).resolve().parents[2] / "examples" / "core"


def _load(name: str):
    path = _EXAMPLES / f"{name}.py"
    if not path.exists():
        path = _EXAMPLES.parent / "internals" / f"{name}.py"
    return load_module(path, f"core_example_{name}")


_ASK = {
    "questions": [
        {
            "id": "framework",
            "header": "Framework",
            "question": "Which test framework?",
            "options": [
                {"label": "pytest", "description": "Fast, fixtures, plugins."},
                {"label": "unittest", "description": "Stdlib, zero deps."},
            ],
        }
    ]
}


async def test_ask_user_handler_answer_rides_back_on_tool_result() -> None:
    example = _load("ask_user_agent")
    from linch.tools import AskUserResponse

    provider = ScriptedProvider(
        [
            ToolUseTurn(tool_name="AskUser", tool_input=_ASK),
            TextTurn(text="Using pytest as you chose."),
        ]
    )
    agent = await example.build_ask_user_agent(
        lambda request, ctx: AskUserResponse(accepted=True, answers={"framework": "pytest"}),
        provider=provider,
        model="m",
    )
    session = await agent.session()

    tool_results = []
    final = None
    async for event in session.run("Set up testing."):
        if event.type == "tool_call_end" and event.tool_name == "AskUser":
            tool_results.append(event.tool_result)
        elif event.type == "result":
            final = event

    assert tool_results, "AskUser should have been called"
    assert json.loads(tool_results[0].content)["answers"] == {"framework": "pytest"}
    assert final is not None and final.final_text == "Using pytest as you chose."


def _align_script() -> ScriptedProvider:
    return ScriptedProvider(
        [
            ToolUseTurn(tool_name="long_task", tool_input={}),
            TextTurn(text="done"),
        ]
    )


async def test_alignment_injects_message_at_next_boundary() -> None:
    example = _load("aligning_agent")
    from linch.events import ResultEvent, UserEvent

    agent, tool = example.build_aligning_agent(provider=_align_script(), model="m")
    session = await agent.session()
    events: list[Any] = []

    async def collect() -> None:
        async for event in session.run("Refactor the auth module."):
            events.append(event)

    run_task = asyncio.create_task(collect())
    await asyncio.wait_for(tool.started.wait(), timeout=5)
    # align() resolves at the next turn boundary (after release), so fire it
    # concurrently rather than awaiting it before releasing the tool.
    align_task = asyncio.create_task(
        session.align("Prioritize correctness over speed.", timeout_s=5)
    )
    tool.release.set()
    await run_task
    await align_task

    injected = [e for e in events if isinstance(e, UserEvent) and e.subtype == "alignment"]
    assert len(injected) == 1
    assert isinstance(events[-1], ResultEvent) and events[-1].subtype == "success"
