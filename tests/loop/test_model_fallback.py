"""Run-level model fallback on provider overload (ROADMAP Phase 1.3).

The loop recovers from a context-length error, but a provider *overload*
(``ProviderError(retryable=True)`` — e.g. HTTP 529) currently kills the run.
``Agent(fallback_models=[...])`` swaps the active model for the rest of the run
on an overload and retries, emitting a ``ModelFallbackEvent``. Opt-in: with no
``fallback_models`` the error surfaces exactly as before (byte-identical).

linch imports happen inside test functions because sibling tests pop ``linch*``
modules from ``sys.modules``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any


class FlakyProvider:
    """Raises an overload error for ``fail_on``; serves scripted turns otherwise.

    ``backup_behaviors`` drives successive non-failing calls: ``"text"`` ends the
    turn, ``"tool"`` emits a ``noop`` tool call (to force a second turn so we can
    assert the swap persists across turns).
    """

    id = "fake"

    def __init__(self, fail_on: str, backup_behaviors: tuple[str, ...] = ("text",)) -> None:
        self.fail_on = fail_on
        self.backup_behaviors = list(backup_behaviors)
        self.models_seen: list[str] = []
        self._backup_calls = 0

    def context_window(self, model: str) -> int:
        return 1_000_000

    async def stream(self, req: Any) -> AsyncIterator[dict[str, object]]:
        from linch.errors import ProviderError
        from linch.types import Usage

        self.models_seen.append(req.model)
        if req.model == self.fail_on:
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


async def test_overload_without_fallback_surfaces_error() -> None:
    provider = FlakyProvider(fail_on="primary")
    agent = _agent(provider)  # no fallback_models → default byte-identical
    session = await agent.session()

    events = [event async for event in session.run("go")]

    assert not any(e.type == "model_fallback" for e in events)
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
