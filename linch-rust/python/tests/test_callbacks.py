"""Intercept callbacks answered in this process."""

from __future__ import annotations

from typing import Any

import pytest
from scripted import MODEL, config, text_turn, tool_turn

from linch_rust import (
    ContextBuildEvent,
    Envelope,
    HookEvent,
    PromptBlocked,
    ResultEvent,
    Server,
    ToolCallEndEvent,
    ToolCallStartEvent,
    UserEvent,
    callback,
    tool,
)


async def test_a_pre_tool_use_callback_blocks_a_call() -> None:
    ran: list[str] = []
    asked: list[dict[str, Any]] = []

    @tool(scope="read")
    async def lookup(q: str) -> str:
        ran.append(q)
        return "found"

    @callback("pre_tool_use", "no-lookups", 1, matcher=["look*"])
    async def guard(request: dict[str, Any]) -> dict[str, Any]:
        asked.append(request)
        return {"action": "block", "reason": "lookups are off today"}

    turns = [tool_turn("call_1", "lookup", {"q": "x"}), text_turn("Blocked, then.")]
    async with Server() as server:
        agent = await server.open_agent(config(turns), tools=[lookup], callbacks=[guard])
        run = await agent.run("s1", "Look up x")
        envelopes = [e async for e in run]

    assert ran == []
    [request] = asked
    assert request["tool_name"] == "lookup" and request["input"] == {"q": "x"}
    assert request["run_id"] == run.id
    [end] = [e.event for e in envelopes if isinstance(e.event, ToolCallEndEvent)]
    assert end.is_error and "lookups are off today" in str(end.result)
    hooks = [e.event for e in envelopes if isinstance(e.event, HookEvent)]
    assert [(h.point, h.policy_id, h.outcome["status"]) for h in hooks] == [
        ("pre_tool_use", "no-lookups", "answered")
    ]
    result = envelopes[-1].event
    assert isinstance(result, ResultEvent) and result.subtype == "success"


async def test_context_build_metadata_lands_on_the_event_not_the_request() -> None:
    @callback("context_build", "rag", 1)
    async def rag(request: dict[str, Any]) -> dict[str, Any]:
        return {"blocks": [{"text": "doc 7"}], "metadata": {"rag": "p1"}}

    async with Server() as server:
        agent = await server.open_agent(config([text_turn("ok")]), callbacks=[rag])
        run = await agent.run("s1", "go")
        envelopes = [e async for e in run]

    [build] = [e.event for e in envelopes if isinstance(e.event, ContextBuildEvent)]
    assert build.metadata == {"rag": "p1"}


async def test_a_user_prompt_submit_block_raises_prompt_blocked() -> None:
    @callback("user_prompt_submit", "no-secrets", 1)
    async def screen(request: dict[str, Any]) -> dict[str, Any]:
        return {"action": "block", "reason": "mentions a secret"}

    async with Server() as server:
        agent = await server.open_agent(config([text_turn("unused")]), callbacks=[screen])
        run = await agent.run("s1", "the password is hunter2")
        with pytest.raises(PromptBlocked) as raised:
            await run.result()

    assert raised.value.reason == "mentions a secret"


def _feedback(envelopes: list[Envelope]) -> list[dict[str, Any]]:
    """The messages sent back to the model after the prompt."""
    return [
        e.event.message
        for e in envelopes
        if isinstance(e.event, UserEvent) and e.event.subtype != "prompt"
    ]


def _verdicts(envelopes: list[Envelope], point: str) -> list[str]:
    return [
        e.event.outcome["verdict"]
        for e in envelopes
        if isinstance(e.event, HookEvent) and e.event.point == point
    ]


def _reminder(text: str) -> dict[str, Any]:
    body = f"<system-reminder>\n{text}\n</system-reminder>"
    return {"role": "user", "content": [{"type": "text", "text": body}]}


async def test_an_after_provider_call_retry_answers_the_tool_calls_and_takes_another_turn() -> None:
    ran: list[str] = []

    @tool(scope="read")
    async def lookup(q: str) -> str:
        ran.append(q)
        return "found"

    @callback("after_provider_call", "no-lookups", 1)
    async def judge(request: dict[str, Any]) -> dict[str, Any]:
        content = request["assembly"]["message"]["content"]
        if any(block["type"] == "tool_use" for block in content):
            return {"action": "retry", "feedback": "answer from what you know"}
        return {"action": "continue"}

    turns = [tool_turn("call_1", "lookup", {"q": "x"}), text_turn("From memory.")]
    async with Server() as server:
        agent = await server.open_agent(config(turns), tools=[lookup], callbacks=[judge])
        run = await agent.run("s1", "Look up x")
        envelopes = [e async for e in run]

    assert ran == []
    assert not any(isinstance(e.event, ToolCallStartEvent) for e in envelopes)
    [sent] = _feedback(envelopes)
    [result] = sent["content"]
    assert result["type"] == "tool_result" and result["tool_use_id"] == "call_1"
    assert result["is_error"] and "answer from what you know" in str(result["content"])
    assert _verdicts(envelopes, "after_provider_call") == ["retry", "continue"]
    final = envelopes[-1].event
    assert isinstance(final, ResultEvent) and final.final_text == "From memory."


async def test_force_continue_at_after_provider_call_is_a_retry() -> None:
    @callback("after_provider_call", "cite", 1)
    async def judge(request: dict[str, Any]) -> dict[str, Any]:
        if request["assembly"]["message"]["content"][0]["text"] == "first":
            return {"action": "force_continue", "feedback": "cite a source"}
        return {"action": "continue"}

    async with Server() as server:
        turns = [text_turn("first"), text_turn("second")]
        agent = await server.open_agent(config(turns), callbacks=[judge])
        run = await agent.run("s1", "go")
        envelopes = [e async for e in run]

    assert _feedback(envelopes) == [_reminder("cite a source")]
    final = envelopes[-1].event
    assert isinstance(final, ResultEvent) and final.final_text == "second"


async def test_a_before_provider_call_force_continue_skips_the_call() -> None:
    @callback("before_provider_call", "plan", 1)
    async def plan(request: dict[str, Any]) -> dict[str, Any]:
        if len(request["request"]["messages"]) == 1:
            return {"action": "force_continue", "feedback": "plan first"}
        return {"action": "continue"}

    async with Server() as server:
        agent = await server.open_agent(config([text_turn("Planned.")]), callbacks=[plan])
        run = await agent.run("s1", "go")
        envelopes = [e async for e in run]

    assert _verdicts(envelopes, "before_provider_call") == ["force_continue", "continue"]
    assert _feedback(envelopes) == [_reminder("plan first")]
    builds = [e for e in envelopes if isinstance(e.event, ContextBuildEvent)]
    assert len(builds) == 1, "the skipped turn makes no call"
    final = envelopes[-1].event
    assert isinstance(final, ResultEvent) and final.final_text == "Planned."


async def test_a_before_provider_call_force_continue_ends_at_max_turns() -> None:
    @callback("before_provider_call", "again", 1)
    async def again(request: dict[str, Any]) -> dict[str, Any]:
        return {"action": "force_continue", "feedback": "again"}

    async with Server() as server:
        limited = config([text_turn("unused")], spec={"model": MODEL, "max_turns": 2})
        agent = await server.open_agent(limited, callbacks=[again])
        run = await agent.run("s1", "go")
        envelopes = [e async for e in run]

    assert _verdicts(envelopes, "before_provider_call") == ["force_continue"] * 2
    assert _feedback(envelopes) == [_reminder("again")] * 2
    assert not any(isinstance(e.event, ContextBuildEvent) for e in envelopes)
    final = envelopes[-1].event
    assert isinstance(final, ResultEvent) and final.subtype == "error"


async def test_a_stop_retry_is_honoured_once_per_run() -> None:
    asked: list[str] = []

    @callback("stop", "judge", 1)
    def judge(request: dict[str, Any]) -> dict[str, Any]:
        asked.append(request["text"])
        return {"action": "retry", "feedback": "be precise"}

    turns = [text_turn("first"), text_turn("second"), text_turn("third")]
    async with Server() as server:
        agent = await server.open_agent(config(turns), callbacks=[judge])
        run = await agent.run("s1", "go")
        envelopes = [e async for e in run]

    assert asked == ["first", "second"]
    assert _feedback(envelopes) == [_reminder("be precise")]
    final = envelopes[-1].event
    assert isinstance(final, ResultEvent)
    assert final.subtype == "success" and final.final_text == "second"


async def test_a_stop_force_continue_keeps_the_run_going_until_max_turns() -> None:
    @callback("stop", "more", 1)
    async def more(request: dict[str, Any]) -> dict[str, Any]:
        return {"action": "force_continue", "feedback": "keep going"}

    turns = [text_turn("first"), text_turn("second"), text_turn("third")]
    async with Server() as server:
        limited = config(turns, spec={"model": MODEL, "max_turns": 2})
        agent = await server.open_agent(limited, callbacks=[more])
        run = await agent.run("s1", "go")
        envelopes = [e async for e in run]

    assert _verdicts(envelopes, "stop") == ["continue", "continue"]
    assert _feedback(envelopes) == [_reminder("keep going")] * 2
    final = envelopes[-1].event
    assert isinstance(final, ResultEvent) and final.subtype == "error"


async def test_points_that_do_not_honour_retry_refuse_it() -> None:
    @callback("pre_tool_use", "retry-here", 1, fail_mode="closed")
    async def guard(request: dict[str, Any]) -> dict[str, Any]:
        return {"action": "retry", "feedback": "again"}

    @tool(scope="read")
    async def lookup(q: str) -> str:
        return "found"

    turns = [tool_turn("call_1", "lookup", {"q": "x"}), text_turn("ok")]
    async with Server() as server:
        agent = await server.open_agent(config(turns), tools=[lookup], callbacks=[guard])
        run = await agent.run("s1", "Look up x")
        envelopes = [e async for e in run]

    [hook] = [e.event for e in envelopes if isinstance(e.event, HookEvent)]
    assert hook.outcome["status"] == "failed"
    assert "malformed hook answer" in hook.outcome["error"]
    [end] = [e.event for e in envelopes if isinstance(e.event, ToolCallEndEvent)]
    assert end.is_error
