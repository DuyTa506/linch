"""Opt-in output-truncation recovery (ROADMAP P6).

A text response cut off by the output-token limit (``stop_reason ==
"max_tokens"``) is, by default, returned as the final answer. With
``Agent(truncation_recovery=TruncationRecovery(...))`` the loop instead injects a
continuation nudge and runs again, bounded by ``max_attempts``.

linch imports happen inside the test bodies (sibling tests pop ``linch*`` from
``sys.modules``).
"""

from __future__ import annotations

from typing import Any

import pytest


def _agent(provider: Any, **kwargs: Any) -> Any:
    from linch import Agent
    from linch.config import FeatureFlags
    from linch.sessions import InMemorySessionStore

    return Agent(
        model="test-model",
        provider=provider,
        permissions={"mode": "skip-dangerous"},
        session_store=InMemorySessionStore(),
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        result_offload=None,
        **kwargs,
    )


async def _collect(session: Any, prompt: str = "go") -> list[Any]:
    return [event async for event in session.run(prompt)]


# ── Default behavior is byte-identical ────────────────────────────────────────


@pytest.mark.asyncio
async def test_truncated_text_is_finalized_by_default() -> None:
    from linch.evals import ScriptedProvider, TextTurn

    provider = ScriptedProvider([TextTurn(text="cut off here", stop_reason="max_tokens")])
    agent = _agent(provider)  # no truncation_recovery
    session = await agent.session()
    events = await _collect(session)

    result = events[-1]
    assert result.type == "result"
    assert result.subtype == "success"
    assert result.stop_reason == "max_tokens"
    assert result.final_text == "cut off here"
    assert provider._index == 1  # exactly one provider call; no recovery


# ── Recovery continues a truncated answer ─────────────────────────────────────


@pytest.mark.asyncio
async def test_recovery_continues_after_truncation() -> None:
    from linch import TruncationRecovery, UserEvent
    from linch.evals import ScriptedProvider, TextTurn

    provider = ScriptedProvider(
        [
            TextTurn(text="part one", stop_reason="max_tokens"),
            TextTurn(text="part two", stop_reason="end_turn"),
        ]
    )
    agent = _agent(provider, truncation_recovery=TruncationRecovery(max_attempts=2))
    session = await agent.session()
    events = await _collect(session)

    result = events[-1]
    assert result.subtype == "success"
    assert result.stop_reason == "end_turn"
    assert result.final_text == "part two"
    assert provider._index == 2  # one recovery turn was spent
    # A continuation nudge was injected as a user turn.
    assert any(
        isinstance(event, UserEvent)
        and "cut off" in "".join(b.text for b in event.message.content if hasattr(b, "text"))
        for event in events
    )


@pytest.mark.asyncio
async def test_recovery_is_bounded_by_max_attempts() -> None:
    from linch import TruncationRecovery
    from linch.evals import ScriptedProvider, TextTurn

    # Always truncated: one recovery attempt, then the truncated answer stands.
    provider = ScriptedProvider(
        [
            TextTurn(text="chunk one", stop_reason="max_tokens"),
            TextTurn(text="chunk two", stop_reason="max_tokens"),
        ]
    )
    agent = _agent(provider, truncation_recovery=TruncationRecovery(max_attempts=1))
    session = await agent.session()
    events = await _collect(session)

    result = events[-1]
    assert result.subtype == "success"
    assert result.stop_reason == "max_tokens"  # gave up gracefully
    assert result.final_text == "chunk two"
    assert provider._index == 2  # one attempt only


@pytest.mark.asyncio
async def test_recovery_does_not_trigger_on_normal_completion() -> None:
    from linch import TruncationRecovery
    from linch.evals import ScriptedProvider, TextTurn

    provider = ScriptedProvider([TextTurn(text="all done", stop_reason="end_turn")])
    agent = _agent(provider, truncation_recovery=TruncationRecovery(max_attempts=3))
    session = await agent.session()
    events = await _collect(session)

    result = events[-1]
    assert result.subtype == "success"
    assert result.final_text == "all done"
    assert provider._index == 1  # no recovery on a non-truncated response


@pytest.mark.asyncio
async def test_custom_feedback_is_used() -> None:
    from linch import TruncationRecovery, UserEvent
    from linch.evals import ScriptedProvider, TextTurn

    provider = ScriptedProvider(
        [
            TextTurn(text="x", stop_reason="max_tokens"),
            TextTurn(text="y", stop_reason="end_turn"),
        ]
    )
    agent = _agent(
        provider,
        truncation_recovery=TruncationRecovery(max_attempts=1, feedback="KEEP GOING NOW"),
    )
    session = await agent.session()
    events = await _collect(session)

    assert any(
        isinstance(event, UserEvent)
        and "KEEP GOING NOW" in "".join(b.text for b in event.message.content if hasattr(b, "text"))
        for event in events
    )


# ── Config validation + public API ────────────────────────────────────────────


def test_invalid_max_attempts_raises() -> None:
    from linch import TruncationRecovery

    with pytest.raises(ValueError):
        TruncationRecovery(max_attempts=0)


def test_empty_feedback_raises() -> None:
    from linch import TruncationRecovery

    with pytest.raises(ValueError):
        TruncationRecovery(feedback="   ")


def test_agent_rejects_invalid_truncation_recovery() -> None:
    from linch import ConfigError
    from linch.evals import ScriptedProvider, TextTurn

    invalid: Any = object()
    with pytest.raises(ConfigError, match="truncation_recovery"):
        _agent(ScriptedProvider([TextTurn(text="x")]), truncation_recovery=invalid)


@pytest.mark.asyncio
async def test_recovery_attempts_are_restored_on_resume() -> None:
    from linch import Agent, TruncationRecovery
    from linch.config import FeatureFlags
    from linch.evals import ScriptedProvider, TextTurn
    from linch.run_store import InMemoryRunStore
    from linch.sessions import InMemorySessionStore

    provider = ScriptedProvider(
        [
            TextTurn(text="chunk one", stop_reason="max_tokens"),
            TextTurn(text="chunk two", stop_reason="max_tokens"),
            TextTurn(text="chunk three", stop_reason="end_turn"),
        ]
    )
    session_store = InMemorySessionStore()
    run_store = InMemoryRunStore()
    agent = Agent(
        model="test-model",
        provider=provider,
        permissions={"mode": "skip-dangerous"},
        session_store=session_store,
        run_store=run_store,
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        result_offload=None,
        truncation_recovery=TruncationRecovery(max_attempts=1),
    )
    session = await agent.session(id="s1")

    run_id = ""
    truncated_assistants = 0
    async for event in session.run("go"):
        if event.type == "system":
            run_id = event.run_id
        if event.type == "assistant" and event.stop_reason == "max_tokens":
            truncated_assistants += 1
            if truncated_assistants == 2:
                break

    assert run_id
    assert provider._index == 2
    run = await run_store.load_run(run_id)
    assert run is not None
    assert run.checkpoint is not None
    assert run.checkpoint.truncation_attempts == 1

    restarted = Agent(
        model="test-model",
        provider=provider,
        permissions={"mode": "skip-dangerous"},
        session_store=session_store,
        run_store=run_store,
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        result_offload=None,
        truncation_recovery=TruncationRecovery(max_attempts=1),
    )
    resumed = await restarted.session(id="s1")
    events = [event async for event in resumed.resume(run_id)]

    result = events[-1]
    assert result.type == "result"
    assert result.stop_reason == "max_tokens"
    assert result.final_text == "chunk two"
    assert provider._index == 2


def test_truncation_recovery_is_public() -> None:
    import linch

    assert "TruncationRecovery" in linch.__all__
