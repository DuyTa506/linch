from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from tests.support.agents import make_agent
from tests.support.tools import StubTool, tool_registry


class FlakyProvider:
    id = "fake"

    def __init__(
        self,
        fail_on: str = "",
        backup_behaviors: tuple[str, ...] = ("text",),
        fail_calls: int = 0,
    ) -> None:
        self.fail_on = fail_on
        self.backup_behaviors = list(backup_behaviors)
        self.models_seen: list[str] = []
        self._backup_calls = 0
        self.fail_calls = fail_calls
        self._calls_made = 0

    def context_window(self, model: str) -> int:
        return 1_000_000

    async def stream(self, req: Any) -> AsyncIterator[dict[str, object]]:
        from linch.errors import ProviderError
        from linch.types import Usage

        self.models_seen.append(req.model)
        self._calls_made += 1
        if self._calls_made <= self.fail_calls or req.model == self.fail_on:
            raise ProviderError("overloaded", status=529, retryable=True)

        idx = min(self._backup_calls, len(self.backup_behaviors) - 1)
        behavior = self.backup_behaviors[idx]
        self._backup_calls += 1

        yield {"type": "message_start", "model": req.model}
        if behavior == "tool":
            yield {"type": "tool_use_start", "id": "t1", "name": "noop"}
            yield {"type": "tool_use_input_delta", "id": "t1", "json_delta": "{}"}
            yield {"type": "tool_use_end", "id": "t1"}
            stop_reason = "tool_use"
        else:
            yield {"type": "text_delta", "text": "done"}
            stop_reason = "end_turn"
        yield {"type": "message_end", "stop_reason": stop_reason, "usage": Usage()}


def _agent(provider: FlakyProvider, **kwargs: Any) -> Any:
    return make_agent(
        provider,
        model="primary",
        tools=tool_registry(StubTool("noop")),
        run_store=None,
        **kwargs,
    )


def _texts(message: Any) -> str:
    return "".join(b.text for b in message.content if hasattr(b, "text"))


async def _run(provider: FlakyProvider, **kwargs: Any) -> list[Any]:
    session = await _agent(provider, **kwargs).session()
    return [event async for event in session.run("go")]


async def test_overload_falls_back_to_next_model() -> None:
    provider = FlakyProvider(fail_on="primary")
    events = await _run(provider, fallback_models=["backup"])

    fallbacks = [e for e in events if e.type == "model_fallback"]
    assert len(fallbacks) == 1
    assert fallbacks[0].from_model == "primary"
    assert fallbacks[0].to_model == "backup"
    assert provider.models_seen == ["primary", "backup"]
    assert events[-1].type == "result"
    assert events[-1].subtype == "success"
    assistants = [e for e in events if e.type == "assistant"]
    assert _texts(assistants[-1].message) == "done"


@pytest.mark.parametrize(
    ("provider_kwargs", "expected_models", "subtype"),
    [
        ({"fail_on": "primary"}, ["primary"] * 3, "error"),
        ({"fail_calls": 1}, ["primary"] * 2, "success"),
    ],
    ids=["permanent-overload", "transient-overload"],
)
async def test_same_model_retry_is_bounded_or_recovers(
    provider_kwargs: dict[str, Any], expected_models: list[str], subtype: str
) -> None:
    provider = FlakyProvider(**provider_kwargs)
    events = await _run(provider, max_retries=2)
    assert not any(e.type == "model_fallback" for e in events)
    assert provider.models_seen == expected_models
    assert events[-1].type == "result"
    assert events[-1].subtype == subtype
    if subtype == "success":
        assistants = [e for e in events if e.type == "assistant"]
        assert _texts(assistants[-1].message) == "done"


async def test_fallback_persists_for_the_rest_of_the_run() -> None:
    provider = FlakyProvider(fail_on="primary", backup_behaviors=("tool", "text"))
    events = await _run(provider, fallback_models=["backup"])

    assert provider.models_seen == ["primary", "backup", "backup"]
    assert len([e for e in events if e.type == "model_fallback"]) == 1
    assert events[-1].subtype == "success"


async def test_fallback_exhausted_surfaces_error() -> None:
    provider = FlakyProvider(fail_on="primary")
    events = await _run(provider, fallback_models=["primary"])

    assert events[-1].type == "result"
    assert events[-1].subtype == "error"
