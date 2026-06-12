"""Tests for closed-loop verification: structured-output repair, the live
Verifier gate, the evals-scorer bridge, and the stop_when predicate.

Unit tests cover the verification module in isolation; integration tests run
a ScriptedProvider through run_loop to verify the gates actually close the
loop.
"""

from __future__ import annotations

from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_agent(provider: Any, *, loop_guard: Any = None, **kwargs: Any):
    from linch import Agent
    from linch.config import FeatureFlags
    from linch.sessions import InMemorySessionStore
    from linch.tools import ToolResult
    from linch.tools.registry import empty_tools

    class _DummyTool:
        name = "FakeTool"
        description = "Dummy tool"
        input_schema = {"type": "object", "properties": {}}
        scope = "read"
        parallel = True
        tags: tuple[str, ...] = ()

        def validate(self, raw):
            return raw

        def summarize(self, input):
            return "FakeTool"

        def resources(self, input):
            return []

        async def execute(self, input, ctx):
            return ToolResult(content="ok")

    return Agent(
        model="test-model",
        provider=provider,
        tools=empty_tools(_DummyTool()),
        permissions={"mode": "skip-dangerous"},
        session_store=InMemorySessionStore(),
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        loop_guard=loop_guard,
        **kwargs,
    )


async def _collect(session, prompt: str = "go", opts=None):
    events = []
    async for event in session.run(prompt, opts):
        events.append(event)
    return events


def _result(events):
    from linch.events import ResultEvent

    results = [e for e in events if isinstance(e, ResultEvent)]
    assert results, "run produced no ResultEvent"
    return results[-1]


def _verification_events(events):
    from linch.events import VerificationEvent

    return [e for e in events if isinstance(e, VerificationEvent)]


# ---------------------------------------------------------------------------
# Unit tests — verification module
# ---------------------------------------------------------------------------


def test_verdict_defaults_to_pass():
    from linch.verification import Verdict

    verdict = Verdict()
    assert verdict.action == "pass"
    assert verdict.feedback == ""


def test_normalize_verifiers_accepts_none_single_and_list():
    from linch.verification import Verdict, normalize_verifiers

    class _V:
        name = "v"

        def verify(self, ctx):
            return Verdict()

    assert normalize_verifiers(None) == []
    single = _V()
    assert normalize_verifiers(single) == [single]
    pair = [_V(), _V()]
    assert normalize_verifiers(pair) == pair


def test_normalize_verifiers_rejects_non_verifier():
    from linch.errors import ConfigError
    from linch.verification import normalize_verifiers

    with pytest.raises(ConfigError):
        normalize_verifiers(object())


@pytest.mark.asyncio
async def test_evaluate_verifiers_returns_first_non_pass():
    from linch.verification import Verdict, VerificationContext, evaluate_verifiers

    class _Pass:
        name = "ok"

        def verify(self, ctx):
            return Verdict()

    class _Retry:
        name = "strict"

        def verify(self, ctx):
            return Verdict(action="retry", feedback="try again")

    ctx = VerificationContext(
        final_text="x", structured_output=None, structured_error=None, turn_index=0, attempt=0
    )
    name, verdict = await evaluate_verifiers([_Pass(), _Retry()], ctx)
    assert name == "strict"
    assert verdict.action == "retry"
    assert verdict.feedback == "try again"


@pytest.mark.asyncio
async def test_evaluate_verifiers_supports_async_and_swallows_errors():
    from linch.verification import Verdict, VerificationContext, evaluate_verifiers

    class _Broken:
        name = "broken"

        def verify(self, ctx):
            raise RuntimeError("boom")

    class _AsyncStop:
        name = "judge"

        async def verify(self, ctx):
            return Verdict(action="stop", reason="bad answer")

    ctx = VerificationContext(
        final_text="x", structured_output=None, structured_error=None, turn_index=0, attempt=0
    )
    name, verdict = await evaluate_verifiers([_Broken(), _AsyncStop()], ctx)
    assert name == "judge"
    assert verdict.action == "stop"


def test_scorer_verifier_maps_outcomes():
    from linch.evals.scorers import text_contains
    from linch.verification import ScorerVerifier, VerificationContext

    verifier = ScorerVerifier(text_contains("done"), feedback="say done")
    ctx_fail = VerificationContext(
        final_text="nope", structured_output=None, structured_error=None, turn_index=0, attempt=0
    )
    ctx_pass = VerificationContext(
        final_text="all done",
        structured_output=None,
        structured_error=None,
        turn_index=0,
        attempt=0,
    )
    assert verifier.verify(ctx_fail).action == "retry"
    assert verifier.verify(ctx_fail).feedback == "say done"
    assert verifier.verify(ctx_pass).action == "pass"
    # None outcome (unknown) maps to pass.
    verifier_none = ScorerVerifier(lambda **_: None, feedback="n/a", name="unknown")
    assert verifier_none.verify(ctx_fail).action == "pass"


# ---------------------------------------------------------------------------
# Integration — structured-output repair loop
# ---------------------------------------------------------------------------

_SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "integer"}},
    "required": ["answer"],
}


@pytest.mark.asyncio
async def test_structured_output_retry_repairs_invalid_json():
    from linch.evals import ScriptedProvider, TextTurn
    from linch.types import OutputSchema

    provider = ScriptedProvider(
        turns=[TextTurn(text="not json at all"), TextTurn(text='{"answer": 42}')]
    )
    agent = _make_agent(
        provider,
        output_schema=OutputSchema(name="ans", schema=_SCHEMA),
        structured_output_retries=1,
    )
    session = await agent.session()
    events = await _collect(session)

    result = _result(events)
    assert result.subtype == "success"
    assert result.structured_output == {"answer": 42}
    assert result.structured_error is None

    retries = _verification_events(events)
    assert len(retries) == 1
    assert retries[0].verifier == "output_schema"
    assert retries[0].action == "retry"
    assert retries[0].attempt == 1


@pytest.mark.asyncio
async def test_structured_output_no_retry_by_default():
    from linch.evals import ScriptedProvider, TextTurn
    from linch.types import OutputSchema

    provider = ScriptedProvider(turns=[TextTurn(text="not json at all")])
    agent = _make_agent(provider, output_schema=OutputSchema(name="ans", schema=_SCHEMA))
    session = await agent.session()
    events = await _collect(session)

    result = _result(events)
    assert result.subtype == "success"
    assert result.structured_output is None
    assert result.structured_error is not None
    assert _verification_events(events) == []


@pytest.mark.asyncio
async def test_structured_output_retries_exhausted_surfaces_error():
    from linch.evals import ScriptedProvider, TextTurn
    from linch.types import OutputSchema

    provider = ScriptedProvider(
        turns=[TextTurn(text="still not json"), TextTurn(text="again not json")]
    )
    agent = _make_agent(
        provider,
        output_schema=OutputSchema(name="ans", schema=_SCHEMA),
        structured_output_retries=1,
    )
    session = await agent.session()
    events = await _collect(session)

    result = _result(events)
    assert result.subtype == "success"
    assert result.structured_output is None
    assert result.structured_error is not None
    assert len(_verification_events(events)) == 1


# ---------------------------------------------------------------------------
# Integration — live verifier gate
# ---------------------------------------------------------------------------


class _RequireFinal:
    """Retry until the answer contains the word FINAL."""

    name = "require_final"

    def verify(self, ctx):
        from linch.verification import Verdict

        if ctx.final_text and "FINAL" in ctx.final_text:
            return Verdict()
        return Verdict(action="retry", feedback="Prefix your answer with FINAL.")


@pytest.mark.asyncio
async def test_verifier_retry_then_pass():
    from linch.evals import ScriptedProvider, TextTurn

    provider = ScriptedProvider(turns=[TextTurn(text="draft"), TextTurn(text="FINAL answer")])
    agent = _make_agent(provider, verifiers=_RequireFinal())
    session = await agent.session()
    events = await _collect(session)

    result = _result(events)
    assert result.subtype == "success"
    assert result.final_text == "FINAL answer"

    retries = _verification_events(events)
    assert len(retries) == 1
    assert retries[0].verifier == "require_final"
    assert retries[0].action == "retry"
    assert retries[0].feedback == "Prefix your answer with FINAL."

    # The feedback must reach the model as a user message.
    from linch.events import UserEvent

    feedback_msgs = [
        e
        for e in events
        if isinstance(e, UserEvent)
        and any("FINAL" in getattr(b, "text", "") for b in e.message.content)
    ]
    assert feedback_msgs, "verifier feedback was not injected into the conversation"


@pytest.mark.asyncio
async def test_verifier_stop_fails_run():
    from linch.evals import ScriptedProvider, TextTurn
    from linch.verification import Verdict

    class _Reject:
        name = "reject"

        def verify(self, ctx):
            return Verdict(action="stop", reason="unacceptable")

    provider = ScriptedProvider(turns=[TextTurn(text="anything")])
    agent = _make_agent(provider, verifiers=_Reject())
    session = await agent.session()
    events = await _collect(session)

    result = _result(events)
    assert result.subtype == "error"
    stops = _verification_events(events)
    assert len(stops) == 1
    assert stops[0].action == "stop"
    assert stops[0].verifier == "reject"


@pytest.mark.asyncio
async def test_verifier_retries_exhausted_falls_through():
    from linch.evals import ScriptedProvider, TextTurn
    from linch.verification import Verdict

    class _AlwaysRetry:
        name = "never_happy"

        def verify(self, ctx):
            return Verdict(action="retry", feedback="more")

    provider = ScriptedProvider(turns=[TextTurn(text="one"), TextTurn(text="two")])
    agent = _make_agent(provider, verifiers=_AlwaysRetry(), max_verification_retries=1)
    session = await agent.session()
    events = await _collect(session)

    result = _result(events)
    assert result.subtype == "success"
    assert result.final_text == "two"

    vevents = _verification_events(events)
    assert [e.action for e in vevents] == ["retry", "exhausted"]


@pytest.mark.asyncio
async def test_scorer_verifier_in_live_run():
    from linch.evals import ScriptedProvider, TextTurn
    from linch.evals.scorers import text_contains
    from linch.verification import ScorerVerifier

    provider = ScriptedProvider(turns=[TextTurn(text="working on it"), TextTurn(text="done!")])
    agent = _make_agent(
        provider,
        verifiers=ScorerVerifier(text_contains("done"), feedback="Finish and say done."),
    )
    session = await agent.session()
    events = await _collect(session)

    result = _result(events)
    assert result.subtype == "success"
    assert result.final_text == "done!"
    assert len(_verification_events(events)) == 1


@pytest.mark.asyncio
async def test_forced_final_turn_bypasses_verifiers():
    """A loop-guard force_final answer must not be bounced back by a verifier,
    otherwise a stuck run could never produce its forced final answer."""
    import json as _json

    from linch.loop_guard import LoopGuard
    from linch.types import Usage
    from linch.verification import Verdict

    class _LoopingProvider:
        id = "looping"

        def __init__(self) -> None:
            self.call_count = 0

        def context_window(self, model: str) -> int:
            return 128_000

        async def stream(self, req):
            self.call_count += 1
            yield {"type": "message_start", "model": req.model}
            if not req.tools:  # force_final turn — tools stripped
                yield {"type": "text_delta", "text": "stuck, giving up"}
                yield {
                    "type": "message_end",
                    "stop_reason": "end_turn",
                    "usage": Usage(),
                    "provider_metadata": None,
                }
                return
            tool_id = f"t{self.call_count}"
            yield {"type": "tool_use_start", "id": tool_id, "name": "FakeTool"}
            yield {"type": "tool_use_input_delta", "id": tool_id, "json_delta": _json.dumps({})}
            yield {"type": "tool_use_end", "id": tool_id}
            yield {
                "type": "message_end",
                "stop_reason": "tool_use",
                "usage": Usage(),
                "provider_metadata": None,
            }

    class _AlwaysRetry:
        name = "never_happy"

        def verify(self, ctx):
            return Verdict(action="retry", feedback="more")

    provider = _LoopingProvider()
    agent = _make_agent(
        provider,
        loop_guard=LoopGuard(max_identical_tool_calls=2, force_final_answer=True),
        verifiers=_AlwaysRetry(),
    )
    session = await agent.session()
    events = await _collect(session)

    result = _result(events)
    assert result.subtype == "success"
    assert result.final_text == "stuck, giving up"
    # The verifier never ran against the forced final answer.
    assert _verification_events(events) == []


# ---------------------------------------------------------------------------
# Integration — stop_when predicate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_when_predicate_stops_run_early():
    from linch.evals import ScriptedProvider, TextTurn, ToolUseTurn
    from linch.session import RunOptions

    provider = ScriptedProvider(
        turns=[
            ToolUseTurn(tool_name="FakeTool", tool_input={}),
            TextTurn(text="never reached"),
        ]
    )
    agent = _make_agent(provider)
    session = await agent.session()

    def _saw_tool_result(sess) -> bool:
        return any(
            getattr(block, "type", None) == "tool_result"
            for message in sess.provider_view
            for block in message.content
        )

    events = await _collect(session, opts=RunOptions(stop_when=_saw_tool_result))

    result = _result(events)
    assert result.subtype == "success"
    # Only the first scripted turn was consumed — the predicate stopped the
    # run before the second provider call.
    assert provider._index == 1


@pytest.mark.asyncio
async def test_stop_when_faulty_predicate_does_not_crash():
    from linch.evals import ScriptedProvider, TextTurn
    from linch.session import RunOptions

    def _boom(sess) -> bool:
        raise RuntimeError("predicate exploded")

    provider = ScriptedProvider(turns=[TextTurn(text="fine")])
    agent = _make_agent(provider)
    session = await agent.session()
    events = await _collect(session, opts=RunOptions(stop_when=_boom))

    result = _result(events)
    assert result.subtype == "success"
    assert result.final_text == "fine"


# ---------------------------------------------------------------------------
# VerificationEvent serialization round-trip
# ---------------------------------------------------------------------------


def test_verification_event_round_trips():
    from linch.events import VerificationEvent, event_from_dict, event_to_dict

    event = VerificationEvent(verifier="judge", action="retry", feedback="fix it", attempt=2)
    raw = event_to_dict(event)
    restored = event_from_dict(raw)
    assert restored == event
