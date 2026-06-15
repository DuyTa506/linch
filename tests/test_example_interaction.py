"""Smoke tests for the interaction examples (examples/core/).

Drive both examples with a deterministic ScriptedProvider (no live key) to prove
they wire up:

  * ask_user_agent — the model calls AskUser, the handler answers, and the answer
    rides back on the tool result. Also covers fail-closed decline and timeout.
  * aligning_agent — session.align() injects a message at the next turn boundary;
    session.interrupt() ends the run with subtype="interrupted".
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
from typing import Any

from linch.evals import ScriptedProvider, TextTurn, ToolUseTurn

_EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "core"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"core_example_{name}", _EXAMPLES / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
    agent = example.build_ask_user_agent(
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


async def test_ask_user_malformed_handler_fails_closed() -> None:
    example = _load("ask_user_agent")

    provider = ScriptedProvider(
        [
            ToolUseTurn(tool_name="AskUser", tool_input=_ASK),
            TextTurn(text="No answer — proceeding with assumptions."),
        ]
    )
    # Handler returns nothing (e.g. the user dismissed the dialog).
    agent = example.build_ask_user_agent(lambda request, ctx: None, provider=provider, model="m")
    session = await agent.session()

    summaries = [
        event.tool_result.summary
        async for event in session.run("Set up testing.")
        if event.type == "tool_call_end" and event.tool_name == "AskUser"
    ]
    assert summaries == ["User declined AskUser"]


async def test_ask_user_timeout_declines_instead_of_hanging() -> None:
    example = _load("ask_user_agent")

    provider = ScriptedProvider(
        [
            ToolUseTurn(tool_name="AskUser", tool_input=_ASK),
            TextTurn(text="Timed out — proceeding."),
        ]
    )

    async def never_answers(request: Any, ctx: Any):
        await asyncio.sleep(10)
        return {"accepted": True}

    agent = example.build_ask_user_agent(
        never_answers, provider=provider, model="m", timeout_s=0.05
    )
    session = await agent.session()

    summaries = [
        event.tool_result.summary
        async for event in session.run("Set up testing.")
        if event.type == "tool_call_end" and event.tool_name == "AskUser"
    ]
    assert summaries == ["User declined AskUser"]


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


async def test_interrupt_ends_run_cleanly() -> None:
    example = _load("aligning_agent")
    from linch.events import ResultEvent

    agent, tool = example.build_aligning_agent(provider=_align_script(), model="m")
    session = await agent.session()
    events: list[Any] = []

    async def collect() -> None:
        async for event in session.run("Start a huge migration."):
            events.append(event)

    run_task = asyncio.create_task(collect())
    await asyncio.wait_for(tool.started.wait(), timeout=5)
    session.interrupt()
    tool.release.set()
    await run_task

    assert isinstance(events[-1], ResultEvent) and events[-1].subtype == "interrupted"
