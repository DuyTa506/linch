"""Compaction token estimation must stay bounded for opaque multimodal payloads."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.support.agents import make_agent
from tests.support.providers import ScriptedProvider, TextTurn


def _estimator_agent() -> SimpleNamespace:
    return SimpleNamespace(model="test-model", token_estimator=None)


def _image(data: str):
    from linch.types import ImageBlock

    return ImageBlock(source={"type": "base64", "media_type": "image/png", "data": data})


@pytest.mark.parametrize("placement", ["top_level", "tool_result"])
def test_estimate_tokens_ignores_image_bytes_but_counts_each_image(placement: str) -> None:
    from linch.compaction import _estimate_tokens
    from linch.types import Message, ToolResultBlock

    def message(data: str, count: int):
        images = [_image(data) for _ in range(count)]
        if placement == "top_level":
            return Message(role="user", content=images)
        return Message(
            role="user",
            content=[ToolResultBlock(tool_use_id="call-1", content=images)],
        )

    agent = _estimator_agent()
    one_small = _estimate_tokens(agent, [message("a", 1)])
    one_large = _estimate_tokens(agent, [message("a" * 400_000, 1)])
    two_small = _estimate_tokens(agent, [message("a", 2)])

    assert one_large == one_small
    assert two_small > one_small


def test_estimate_tokens_still_counts_text_and_tool_inputs() -> None:
    from linch.compaction import _estimate_tokens
    from linch.types import Message, TextBlock, ToolUseBlock

    agent = _estimator_agent()
    short_text = _estimate_tokens(agent, [Message(role="user", content=[TextBlock(text="x")])])
    long_text = _estimate_tokens(agent, [Message(role="user", content=[TextBlock(text="x" * 400)])])
    short_input = _estimate_tokens(
        agent,
        [
            Message(
                role="assistant",
                content=[ToolUseBlock(id="call-1", name="search", input={"query": "x"})],
            )
        ],
    )
    long_input = _estimate_tokens(
        agent,
        [
            Message(
                role="assistant",
                content=[
                    ToolUseBlock(
                        id="call-1",
                        name="search",
                        input={"query": "x" * 400},
                    )
                ],
            )
        ],
    )

    assert long_text > short_text
    assert long_input > short_input


@pytest.mark.parametrize("kind", ["thinking_signature", "redacted_data"])
def test_estimate_tokens_ignores_opaque_thinking_payloads(kind: str) -> None:
    from linch.compaction import _estimate_tokens
    from linch.types import Message, RedactedThinkingBlock, ThinkingBlock

    def message(payload: str) -> Message:
        block = (
            ThinkingBlock(thinking="", signature=payload)
            if kind == "thinking_signature"
            else RedactedThinkingBlock(data=payload)
        )
        return Message(role="assistant", content=[block])

    agent = _estimator_agent()
    assert _estimate_tokens(agent, [message("x")]) == _estimate_tokens(
        agent, [message("x" * 400_000)]
    )


@pytest.mark.asyncio
async def test_large_base64_prompt_does_not_trigger_proactive_compaction() -> None:
    from linch.session import RunOptions
    from linch.types import Message, TextBlock

    provider = ScriptedProvider(
        [TextTurn("summary should not be requested"), TextTurn("done")],
        context_window=200_000,
    )
    agent = make_agent(
        provider,
        max_output_tokens=100,
        run_store=None,
    )
    session = await agent.session()

    history = [
        Message(
            role="assistant" if index % 2 == 0 else "user",
            content=[TextBlock(text=f"history-{index}")],
        )
        for index in range(21)
    ]
    await session.append(history)

    events = [
        event
        async for event in session.run(
            "describe the image",
            RunOptions(
                images=[
                    {
                        "media_type": "image/png",
                        "data": "A" * 800_000,
                    }
                ]
            ),
        )
    ]

    assert not any(event.type == "compaction" for event in events)
    assert len(provider.requests) == 1
    assert len(provider.requests[0].messages) == 22
    assert events[-1].type == "result"
    assert events[-1].subtype == "success"
