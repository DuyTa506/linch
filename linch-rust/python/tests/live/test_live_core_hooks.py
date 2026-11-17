"""Live: Python callbacks at the loop's hook points, driven by a real model."""

from __future__ import annotations

import json
from typing import Any

import pytest
from live_core_support import PROVIDERS, Live, ask, make_config, message_text, retry_once, text_of

from linch_rust import ContextBuildEvent, HookEvent, UserEvent, callback, tool

pytestmark = pytest.mark.live


def make_echo(seen: list[str]) -> Any:
    @tool(scope="read")
    async def echo_tool(text: str) -> str:
        """Echo the text back."""
        seen.append(text)
        return f"echo:{text}"

    return echo_tool


@pytest.mark.parametrize("live", PROVIDERS)
@retry_once
async def test_pre_tool_use_blocks_a_call_before_the_tool_runs(live: Live) -> None:
    seen: list[str] = []
    asked: list[dict[str, Any]] = []

    @callback("pre_tool_use", "no-evil", 1, matcher=["echo_tool"])
    async def guard(request: dict[str, Any]) -> dict[str, Any]:
        asked.append(request)
        if "evil" in request["input"]["text"]:
            return {"action": "block", "reason": "evil text is not allowed"}
        return {"action": "continue"}

    trace = await ask(
        live,
        "Call echo_tool with text 'evil plan'. If it is refused, quote the refusal reason.",
        tools=(make_echo(seen),),
        callbacks=(guard,),
    )
    assert seen == [] and asked and asked[0]["tool_name"] == "echo_tool"
    [end] = trace.ends("echo_tool")
    assert end.is_error and "evil text is not allowed" in text_of(end)
    hooks = [h for h in trace.of(HookEvent) if h.point == "pre_tool_use"]
    assert hooks and hooks[0].policy_id == "no-evil" and hooks[0].tool_use_id


@pytest.mark.parametrize("live", PROVIDERS)
@retry_once
async def test_pre_tool_use_rewrites_the_input_the_tool_receives(live: Live) -> None:
    seen: list[str] = []

    @callback("pre_tool_use", "rewrite", 1, matcher=["echo_tool"])
    async def rewrite(_request: dict[str, Any]) -> dict[str, Any]:
        return {"action": "rewrite", "input": {"text": "REWRITTEN-31"}}

    trace = await ask(
        live,
        "Call echo_tool with text 'original' and report what it returned.",
        tools=(make_echo(seen),),
        callbacks=(rewrite,),
    )
    assert seen == ["REWRITTEN-31"]
    [start] = trace.starts("echo_tool")
    assert start.input == {"text": "REWRITTEN-31"}
    assert "echo:REWRITTEN-31" in text_of(trace.ends("echo_tool")[0])
    assert "REWRITTEN-31" in (trace.result.final_text or "")


@pytest.mark.parametrize("live", PROVIDERS)
@retry_once
async def test_post_tool_use_replaces_the_result_the_model_reads(live: Live) -> None:
    @tool(scope="read")
    async def get_code() -> str:
        """Return the access code."""
        return "REAL-CODE-111"

    @callback("post_tool_use", "swap", 1, matcher=["get_code"])
    async def swap(_request: dict[str, Any]) -> dict[str, Any]:
        return {"action": "replace", "result": {"output": {"value": "ZEBRA-77"}}}

    trace = await ask(
        live,
        "Call get_code and tell me the code exactly.",
        tools=(get_code,),
        callbacks=(swap,),
    )
    [end] = trace.ends("get_code")
    assert "ZEBRA-77" in text_of(end) and "REAL-CODE-111" not in text_of(end)
    assert "ZEBRA-77" in (trace.result.final_text or "")
    assert "REAL-CODE-111" not in (trace.result.final_text or "")


@pytest.mark.parametrize("live", PROVIDERS)
@retry_once
async def test_before_provider_call_sees_every_request_and_can_rewrite_it(live: Live) -> None:
    requests: list[dict[str, Any]] = []
    seen: list[str] = []

    @callback("before_provider_call", "spy", 1)
    async def spy(request: dict[str, Any]) -> dict[str, Any]:
        requests.append(request)
        # Rewrite every request: add a system line the model must obey.
        body = json.loads(json.dumps(request["request"]))
        body["system"] = [*body.get("system", []), {"text": "End your answer with ENDMARK-9."}]
        return {"action": "rewrite", "request": body}

    trace = await ask(
        live,
        "Call echo_tool with text 'hi', then reply with the word done.",
        tools=(make_echo(seen),),
        callbacks=(spy,),
    )
    assert len(requests) >= 2
    turns = [r["turn"] for r in requests]
    assert turns == sorted(turns) and len(set(turns)) == len(turns)
    assert requests[0]["request"]["model"] == live.model
    assert any(t["name"] == "echo_tool" for t in requests[0]["request"]["tools"])
    assert seen == ["hi"] and trace.result.subtype == "success"
    # The rewrite is what was sent: the model obeyed the line the callback added.
    assert "ENDMARK-9" in (trace.result.final_text or "")


@pytest.mark.parametrize("live", PROVIDERS)
@retry_once
async def test_context_build_injects_context_the_model_then_uses(live: Live) -> None:
    @callback("context_build", "codeword", 1)
    async def inject(_request: dict[str, Any]) -> dict[str, Any]:
        return {
            "blocks": [{"text": "Company fact: the codeword is PAPAYA-91."}],
            "metadata": {"source": "live-test"},
        }

    trace = await ask(
        live,
        "What is the company codeword? Answer with the codeword only.",
        callbacks=(inject,),
    )
    assert "PAPAYA-91" in (trace.result.final_text or "")
    # Per-request context stays out of the session log: no user event carries it.
    users = [message_text(u.message) for u in trace.of(UserEvent)]
    assert not any("PAPAYA-91" in text for text in users)
    assert trace.of(ContextBuildEvent)[0].metadata == {"source": "live-test"}


@pytest.mark.parametrize("live", PROVIDERS)
@retry_once
async def test_a_stop_hook_forces_one_more_turn_with_its_feedback(live: Live) -> None:
    calls: list[str] = []

    @callback("stop", "verify", 1)
    async def verify(request: dict[str, Any]) -> dict[str, Any]:
        calls.append(request["text"])
        if len(calls) == 1:
            return {"action": "continue", "feedback": "Add the word VERIFIED-5 to your answer."}
        return {"action": "allow"}

    trace = await ask(live, "Say hello in one short sentence.", callbacks=(verify,))
    assert len(calls) == 2
    assert len(trace.assistants()) == 2
    notes = [u for u in trace.of(UserEvent) if u.subtype == "notification"]
    assert notes and "VERIFIED-5" in message_text(notes[0].message)
    assert "VERIFIED-5" in (trace.result.final_text or "")
    assert trace.result.subtype == "success"


@pytest.mark.parametrize("live", PROVIDERS)
@retry_once
async def test_a_callback_that_raises_fails_closed_at_pre_tool_use(live: Live) -> None:
    seen: list[str] = []

    @callback("pre_tool_use", "broken", 1, matcher=["echo_tool"])
    async def broken(_request: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("policy service is down")

    trace = await ask(
        live,
        "Call echo_tool with text 'x' and then say done.",
        tools=(make_echo(seen),),
        callbacks=(broken,),
        config=make_config(live, live.spec(max_turns=4)),
    )
    assert seen == []
    [end] = trace.ends("echo_tool")
    assert end.is_error
