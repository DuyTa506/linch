"""Run-level model fallback and same-model retry on provider overload.

The loop recovers from a context-length error, but a provider *overload*
(``ProviderError(retryable=True)`` — e.g. HTTP 529) currently kills the run.
``Agent(fallback_models=[...])`` swaps the active model for the rest of the run
on an overload and retries, emitting a ``ModelFallbackEvent``.

With no ``fallback_models`` configured (or once they're exhausted), a
retryable ``ProviderError`` retries the SAME model/request up to
``agent.max_retries`` times before surfacing — see ``_retry_same_model`` in
``loop/streaming.py``. This covers transport-level hiccups (e.g. an
OpenAI-compatible server emitting a malformed mid-stream tool-call delta)
that have nothing to do with which model is configured.

linch imports happen inside test functions because sibling tests pop ``linch*``
modules from ``sys.modules``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any


class FlakyProvider:
    """Raises an overload error for ``fail_on`` (permanent, keyed on model) and/or
    the first ``fail_calls`` calls (transient, any model); serves scripted turns
    otherwise.

    ``fail_on`` models the fallback-swap scenario (a specific model is
    permanently overloaded). ``fail_calls`` models a transport-level hiccup (the
    same model recovers after N failures) — used for the same-model-retry tests.

    ``backup_behaviors`` drives successive non-failing calls: ``"text"`` ends the
    turn, ``"tool"`` emits a ``noop`` tool call (to force a second turn so we can
    assert the swap persists across turns).
    """

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
    from linch import Agent
    from linch.sessions import InMemorySessionStore
    from linch.tools import ToolRegistry, tool

    @tool
    def noop() -> str:
        """A no-op tool."""
        return "ok"

    tools = ToolRegistry()
    tools.register(noop)
    return Agent(
        model="primary",
        provider=provider,
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
        cwd=".",
        tools=tools,
        **kwargs,
    )


def _texts(message: Any) -> str:
    return "".join(b.text for b in message.content if hasattr(b, "text"))


async def test_overload_falls_back_to_next_model() -> None:
    provider = FlakyProvider(fail_on="primary")
    agent = _agent(provider, fallback_models=["backup"])
    session = await agent.session()

    events = [event async for event in session.run("go")]

    fallbacks = [e for e in events if e.type == "model_fallback"]
    assert len(fallbacks) == 1
    assert fallbacks[0].from_model == "primary"
    assert fallbacks[0].to_model == "backup"
    assert provider.models_seen == ["primary", "backup"]
    assert events[-1].type == "result"
    assert events[-1].subtype == "success"
    assistants = [e for e in events if e.type == "assistant"]
    assert _texts(assistants[-1].message) == "done"


async def test_overload_without_fallback_retries_same_model_then_surfaces_error() -> None:
    # "primary" is PERMANENTLY overloaded (fail_on), so same-model retry can't
    # recover — but it must still be ATTEMPTED (max_retries+1 total calls)
    # before the error surfaces, and no model_fallback event is emitted (no
    # fallback_models configured).
    provider = FlakyProvider(fail_on="primary")
    agent = _agent(provider, max_retries=2)
    session = await agent.session()

    events = [event async for event in session.run("go")]

    assert not any(e.type == "model_fallback" for e in events)
    assert provider.models_seen == ["primary", "primary", "primary"]  # 1 + max_retries
    assert events[-1].type == "result"
    assert events[-1].subtype == "error"


async def test_no_fallback_retries_same_model_and_recovers() -> None:
    # A transient hiccup (e.g. a malformed mid-stream tool-call delta from an
    # OpenAI-compatible server): the SAME model fails once, then succeeds.
    provider = FlakyProvider(fail_calls=1)
    agent = _agent(provider, max_retries=2)
    session = await agent.session()

    events = [event async for event in session.run("go")]

    assert not any(e.type == "model_fallback" for e in events)
    assert provider.models_seen == ["primary", "primary"]
    assert events[-1].type == "result"
    assert events[-1].subtype == "success"
    assistants = [e for e in events if e.type == "assistant"]
    assert _texts(assistants[-1].message) == "done"


async def test_same_model_retry_bounded_by_max_retries() -> None:
    # A hiccup that never clears within the retry budget: exhausts exactly
    # max_retries retries (1 + max_retries total calls), then surfaces.
    provider = FlakyProvider(fail_calls=10)
    agent = _agent(provider, max_retries=2)
    session = await agent.session()

    events = [event async for event in session.run("go")]

    assert provider.models_seen == ["primary", "primary", "primary"]
    assert events[-1].type == "result"
    assert events[-1].subtype == "error"


async def test_fallback_persists_for_the_rest_of_the_run() -> None:
    # backup does a tool call on its first turn, forcing a second turn. The
    # primary must NOT be retried on turn 2 — the swap is run-level.
    provider = FlakyProvider(fail_on="primary", backup_behaviors=("tool", "text"))
    agent = _agent(provider, fallback_models=["backup"])
    session = await agent.session()

    events = [event async for event in session.run("go")]

    assert provider.models_seen == ["primary", "backup", "backup"]
    assert len([e for e in events if e.type == "model_fallback"]) == 1
    assert events[-1].subtype == "success"


async def test_fallback_exhausted_surfaces_error() -> None:
    # Both the primary and its only fallback overload → the error surfaces.
    provider = FlakyProvider(fail_on="primary")
    agent = _agent(provider, fallback_models=["primary"])  # fallback also fails
    session = await agent.session()

    events = [event async for event in session.run("go")]

    assert events[-1].type == "result"
    assert events[-1].subtype == "error"
