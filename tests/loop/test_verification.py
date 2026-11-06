"""Tests for closed-loop verification: structured-output repair, the live
Verifier gate, the evals-scorer bridge, and the stop_when predicate.

Unit tests cover the verification module in isolation; integration tests run
a ScriptedProvider through run_loop to verify the gates actually close the
loop.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.support.agents import make_agent
from tests.support.providers import ScriptedProvider, TextTurn, ToolUseTurn
from tests.support.tools import StubTool, tool_registry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_agent(provider: Any, *, loop_guard: Any = None, **kwargs: Any):
    from linch.config import FeatureFlags

    return make_agent(
        provider,
        tools=tool_registry(StubTool("FakeTool", scope="read", parallel=True)),
        run_store=None,
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


def test_normalize_verifiers_accepts_none_single_and_list():
    from linch.errors import ConfigError
    from linch.verification import Verdict, normalize_verifiers

    verdict = Verdict()
    assert verdict.action == "pass"
    assert verdict.feedback == ""

    class _V:
        name = "v"

        def verify(self, ctx):
            return Verdict()

    assert normalize_verifiers(None) == []
    single = _V()
    assert normalize_verifiers(single) == [single]
    pair = [_V(), _V()]
    assert normalize_verifiers(pair) == pair

    with pytest.raises(ConfigError):
        normalize_verifiers(object())


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
@pytest.mark.parametrize(
    ("turns", "retry_count", "expected_actions"),
    [
        ([TextTurn(text="not json at all")], None, []),
        (
            [TextTurn(text="still not json"), TextTurn(text="again not json")],
            1,
            ["retry", "exhausted"],
        ),
    ],
    ids=["no-retry", "exhausted"],
)
async def test_structured_output_invalid_json_paths(turns, retry_count, expected_actions):
    from linch.types import OutputSchema

    provider = ScriptedProvider(turns=turns)
    retry_options = {} if retry_count is None else {"structured_output_retries": retry_count}
    agent = _make_agent(
        provider,
        output_schema=OutputSchema(name="ans", schema=_SCHEMA),
        **retry_options,
    )
    session = await agent.session()
    events = await _collect(session)

    result = _result(events)
    assert result.subtype == "success"
    assert result.structured_output is None
    assert result.structured_error is not None
    vevents = _verification_events(events)
    assert [event.action for event in vevents] == expected_actions
    if vevents:
        assert vevents[-1].verifier == "output_schema"


@pytest.mark.asyncio
async def test_final_tool_structured_output_retry_repairs_schema_error():
    from linch.types import OutputSchema

    provider = ScriptedProvider(
        turns=[
            ToolUseTurn(tool_name="ans", tool_input={"answer": "wrong"}),
            ToolUseTurn(tool_name="ans", tool_input={"answer": 42}),
        ]
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

    retries = _verification_events(events)
    assert len(retries) == 1
    assert retries[0].verifier == "output_schema"
    assert retries[0].action == "retry"


@pytest.mark.asyncio
async def test_final_tool_retry_pairs_tool_use_with_tool_result():
    """Regression: a final-tool schema-repair retry must answer the pending
    terminal tool_use with a tool_result.  Otherwise the next provider request
    carries a tool_use with no matching tool_result and real providers reject
    it.  This provider asserts pairing on every call."""
    from linch.types import OutputSchema, ToolResultBlock, ToolUseBlock

    class PairingProvider(ScriptedProvider):
        async def stream(self, req):
            tool_use_ids = {
                b.id
                for m in req.messages
                for b in (m.content if isinstance(m.content, list) else [])
                if isinstance(b, ToolUseBlock)
            }
            answered = {
                b.tool_use_id
                for m in req.messages
                for b in (m.content if isinstance(m.content, list) else [])
                if isinstance(b, ToolResultBlock)
            }
            unmatched = tool_use_ids - answered
            assert not unmatched, f"unmatched tool_use in request: {unmatched}"
            async for ev in super().stream(req):
                yield ev

    provider = PairingProvider(
        turns=[
            ToolUseTurn(tool_name="ans", tool_input={"answer": "wrong"}, tool_id="t1"),
            ToolUseTurn(tool_name="ans", tool_input={"answer": 42}, tool_id="t2"),
        ]
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
    assert len(provider.requests) == 2  # both turns consumed; retry did not crash


# ---------------------------------------------------------------------------
# Integration — live verifier gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verifier_stop_fails_run():
    from linch.hooks import FinalAnswerVerifierHook
    from linch.verification import Verdict

    class _Reject:
        name = "reject"

        def verify(self, ctx):
            return Verdict(action="stop", reason="unacceptable")

    provider = ScriptedProvider(turns=[TextTurn(text="anything")])
    agent = _make_agent(provider, hooks=[FinalAnswerVerifierHook(_Reject())])
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
    from linch.hooks import FinalAnswerVerifierHook
    from linch.verification import Verdict

    class _AlwaysRetry:
        name = "never_happy"

        def verify(self, ctx):
            return Verdict(action="retry", feedback="more")

    provider = ScriptedProvider(turns=[TextTurn(text="one"), TextTurn(text="two")])
    agent = _make_agent(provider, hooks=[FinalAnswerVerifierHook(_AlwaysRetry(), max_retries=1)])
    session = await agent.session()
    events = await _collect(session)

    result = _result(events)
    assert result.subtype == "success"
    assert result.final_text == "two"

    vevents = _verification_events(events)
    assert [e.action for e in vevents] == ["retry", "exhausted"]


@pytest.mark.asyncio
async def test_scorer_verifier_in_live_run():
    from linch.ext.evals.scorers import text_contains
    from linch.hooks import FinalAnswerVerifierHook
    from linch.verification import ScorerVerifier

    provider = ScriptedProvider(turns=[TextTurn(text="working on it"), TextTurn(text="done!")])
    agent = _make_agent(
        provider,
        hooks=[
            FinalAnswerVerifierHook(
                ScorerVerifier(text_contains("done"), feedback="Finish and say done.")
            )
        ],
    )
    session = await agent.session()
    events = await _collect(session)

    result = _result(events)
    assert result.subtype == "success"
    assert result.final_text == "done!"
    assert len(_verification_events(events)) == 1


# ---------------------------------------------------------------------------
# Integration — stop_when predicate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_when_faulty_predicate_does_not_crash():
    from linch.hooks import StopPredicateHook

    def _boom(sess) -> bool:
        raise RuntimeError("predicate exploded")

    provider = ScriptedProvider(turns=[TextTurn(text="fine")])
    agent = _make_agent(provider, hooks=[StopPredicateHook(_boom)])
    session = await agent.session()
    events = await _collect(session)

    result = _result(events)
    assert result.subtype == "success"
    assert result.final_text == "fine"


# ---------------------------------------------------------------------------
# VerificationEvent serialization round-trip
# ---------------------------------------------------------------------------
