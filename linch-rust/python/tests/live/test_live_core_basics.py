"""Live: a text turn, streaming, usage accounting, thinking and effort."""

from __future__ import annotations

import pytest
from live_core_support import (
    DEEPSEEK,
    ONLY_DEEPSEEK,
    ONLY_VLLM,
    PROVIDERS,
    Live,
    ask,
    make_config,
    message_text,
    retry_once,
)

from linch_rust import ErrorEvent, InitEvent, PartialAssistantEvent, UsageEvent

pytestmark = pytest.mark.live


@pytest.mark.parametrize("live", PROVIDERS)
@retry_once
async def test_a_text_turn_streams_and_accounts_usage(live: Live) -> None:
    trace = await ask(live, "Reply with exactly the word: pong")

    [init] = trace.of(InitEvent)
    assert init.provider == live.provider["id"] and init.model == live.model
    result = trace.result
    assert result.subtype == "success" and result.usage_complete
    assert "pong" in (result.final_text or "").lower()

    # Streaming: text deltas arrive, in order, and rebuild the final message.
    deltas = [d.delta for d in trace.of(PartialAssistantEvent)]
    streamed = "".join(d["text"] for d in deltas if d["kind"] == "text")
    [assistant] = trace.assistants()
    assert streamed and streamed == message_text(assistant.message)
    seqs = [e.seq for e in trace.envelopes]
    assert seqs == sorted(seqs)
    partial_seqs = [e.seq for e in trace.envelopes if isinstance(e.event, PartialAssistantEvent)]
    assistant_seq = next(e.seq for e in trace.envelopes if e.event is assistant)
    assert all(seq < assistant_seq for seq in partial_seqs)

    # Accounting: one usage event per call, the run total is their sum.
    usages = trace.of(UsageEvent)
    assert len(usages) == 1
    total = result.total_usage
    assert total["input_tokens"] > 0 and total["output_tokens"] > 0
    assert total["input_tokens"] == sum(u.usage["input_tokens"] for u in usages)
    assert total["output_tokens"] == sum(u.usage["output_tokens"] for u in usages)
    assert usages[0].cumulative["output_tokens"] == total["output_tokens"]
    # The built-in price table holds Anthropic models only (docs/usage/events.md):
    # a vLLM or DeepSeek run is honestly unpriced.
    assert result.total_cost_usd is None and usages[0].cost_usd is None


@pytest.mark.parametrize("live", ONLY_VLLM)
@retry_once
async def test_vllm_thinking_on_streams_reasoning_off_does_not(live: Live) -> None:
    on = await ask(
        live,
        "What is 17 + 25? Answer with the number only.",
        config=make_config(live, live.spec(thinking={"type": "enabled"}, max_output_tokens=1500)),
    )
    off = await ask(live, "What is 17 + 25? Answer with the number only.")

    kinds_on = {d.delta["kind"] for d in on.of(PartialAssistantEvent)}
    kinds_off = {d.delta["kind"] for d in off.of(PartialAssistantEvent)}
    assert "thinking" in kinds_on
    assert "thinking" not in kinds_off
    [assistant] = off.assistants()
    assert all(b["type"] != "thinking" for b in assistant.message["content"])
    assert "42" in (off.result.final_text or "")
    assert on.result.total_usage["output_tokens"] > off.result.total_usage["output_tokens"]


@pytest.mark.parametrize("effort", ["low", "medium", "xhigh"])
@pytest.mark.parametrize("live", ONLY_VLLM)
@retry_once
async def test_vllm_reasoning_effort_is_accepted(live: Live, effort: str) -> None:
    config = make_config(
        live,
        live.spec(thinking={"type": "enabled"}, effort=effort, max_output_tokens=1500),
    )
    trace = await ask(live, "What is 6 times 7? Answer with the number only.", config=config)
    assert trace.result.subtype == "success"
    assert "42" in (trace.result.final_text or "")
    thinking = [d for d in trace.of(PartialAssistantEvent) if d.delta["kind"] == "thinking"]
    assert thinking, "vLLM streamed no reasoning with thinking enabled"


@pytest.mark.parametrize("live", ONLY_DEEPSEEK)
@retry_once
async def test_deepseek_thinking_on_streams_reasoning_off_does_not(live: Live) -> None:
    prompt = "What is 17 + 25? Answer with the number only."
    on = await ask(
        live,
        prompt,
        config=make_config(live, live.spec(thinking={"type": "enabled"}, max_output_tokens=800)),
    )
    off = await ask(DEEPSEEK, prompt)

    kinds_on = {d.delta["kind"] for d in on.of(PartialAssistantEvent)}
    kinds_off = {d.delta["kind"] for d in off.of(PartialAssistantEvent)}
    assert "thinking" in kinds_on
    assert "thinking" not in kinds_off
    assert "42" in (on.result.final_text or "") and "42" in (off.result.final_text or "")


@pytest.mark.parametrize("effort", ["high", "max"])
@pytest.mark.parametrize("live", ONLY_VLLM)
async def test_vllm_qwen_refuses_effort_high_and_max_with_a_clean_error(
    live: Live, effort: str
) -> None:
    # Finding: the Qwen chat template behind this vLLM accepts only low, medium
    # and xhigh; linch-rust forwards `high` and `max` as they are, so the run
    # ends with a non-retryable 400 instead of mapping them. The failure is
    # clean (an error event and a result of subtype error, nothing billed).
    config = make_config(
        live, live.spec(thinking={"type": "enabled"}, effort=effort, max_output_tokens=1500)
    )
    trace = await ask(live, "What is 6 times 7? Answer with the number only.", config=config)
    [error] = trace.of(ErrorEvent)
    assert error.error["status"] == 400 and error.error["retryable"] is False
    assert trace.result.subtype == "error" and trace.result.total_usage["output_tokens"] == 0


@pytest.mark.parametrize("effort", ["low", "medium", "max"])
@pytest.mark.parametrize("live", ONLY_DEEPSEEK)
@retry_once
async def test_deepseek_effort_is_accepted_while_thinking(live: Live, effort: str) -> None:
    config = make_config(live, live.spec(thinking={"type": "enabled"}, effort=effort))
    trace = await ask(live, "What is 6 times 7? Answer with the number only.", config=config)
    assert trace.result.subtype == "success" and "42" in (trace.result.final_text or "")
