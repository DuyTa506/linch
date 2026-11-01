"""Custom estimator invocation is shared; each caller owns its fallback policy."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest


def _adapters(estimator: Any) -> tuple[int, int, int, int]:
    from linch.compaction import _estimate_tokens
    from linch.context.builder import _estimate_context_tokens, _estimate_one_message
    from linch.filesystem.offload import estimate_tokens
    from linch.types import Message, SystemBlock, TextBlock

    message = Message(role="user", content=[TextBlock(text="é")])
    return (
        estimate_tokens("é", estimator, "chosen-model"),
        _estimate_one_message(message, estimator, "chosen-model"),
        _estimate_context_tokens([SystemBlock(text="x")], [message], estimator, "chosen-model"),
        _estimate_tokens(
            SimpleNamespace(token_estimator=estimator, model="default"),
            [message],
            model="chosen-model",
        ),
    )


@pytest.mark.parametrize(("result", "expected"), [("7", 7), (2.9, 2), (-3, 0), (True, 1)])
def test_custom_estimator_conversion_model_and_message_adapters(result: Any, expected: int) -> None:
    calls = []

    def estimator(messages: Any, model: Any) -> Any:
        calls.append((messages, model))
        return result

    assert _adapters(estimator) == (expected, expected, expected + 1, expected)
    assert len(calls) == 4
    assert all(model == "chosen-model" for _, model in calls)
    assert all(messages[0].role == "user" for messages, _ in calls)
    assert all(messages[0].content[0].text == "é" for messages, _ in calls)


@pytest.mark.parametrize("failure", ["raise", "conversion", "noncallable"])
def test_estimator_failures_retain_distinct_fallbacks(failure: str) -> None:
    def estimator(messages: Any, model: Any) -> Any:
        if failure == "raise":
            raise RuntimeError("unavailable")
        return object()

    assert _adapters(None if failure == "noncallable" else estimator) == _adapters(None)
    offload, one_message, context, compaction = _adapters(None)
    assert (offload, one_message, context) == (0, 1, 2)
    # Compaction includes the normalized full message envelope, not just text.
    assert compaction > context
