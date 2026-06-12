"""Compaction ladder tests — micro-compact, reactive recovery, circuit breaker.

linch imports happen inside test functions / provider methods (not at module
level) because tests/loop/test_hardening.py pops all ``linch*`` modules from
``sys.modules``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

# ── helpers ───────────────────────────────────────────────────────────────────


def _history(turns: int, result_size: int = 100) -> list[Any]:
    """Build a provider_view of *turns* assistant tool-use turns + results."""
    from linch.types import Message, TextBlock, ToolResultBlock, ToolUseBlock

    messages: list[Any] = [Message(role="user", content=[TextBlock(text="go")])]
    for i in range(turns):
        messages.append(
            Message(
                role="assistant",
                content=[ToolUseBlock(id=f"call_{i}", name="BigTool", input={"n": result_size})],
            )
        )
        messages.append(
            Message(
                role="user",
                content=[ToolResultBlock(tool_use_id=f"call_{i}", content="x" * result_size)],
            )
        )
    return messages


def _elided_results(messages: list[Any]) -> list[Any]:
    from linch.compaction import _ELIDED
    from linch.types import ToolResultBlock

    out = []
    for msg in messages:
        for block in msg.content:
            if isinstance(block, ToolResultBlock) and block.content == _ELIDED:
                out.append(block)
    return out


# ── micro_compact unit tests ──────────────────────────────────────────────────


def test_micro_compact_elides_old_tool_results_only() -> None:
    from linch.compaction import _ELIDED, micro_compact
    from linch.types import ToolResultBlock

    messages = _history(12, result_size=500)

    new_messages, n_elided = micro_compact(messages, keep_recent_turns=10)

    # 12 turns, keep 10 → the 2 oldest tool results are elided.
    assert n_elided == 2
    assert len(new_messages) == len(messages)
    results = [
        block for msg in new_messages for block in msg.content if isinstance(block, ToolResultBlock)
    ]
    assert [r.content for r in results[:2]] == [_ELIDED, _ELIDED]
    assert all(r.content == "x" * 500 for r in results[2:])
    # tool_use_id pairing preserved for every result.
    assert [r.tool_use_id for r in results] == [f"call_{i}" for i in range(12)]


def test_micro_compact_does_not_mutate_input_messages() -> None:
    from linch.compaction import micro_compact
    from linch.types import ToolResultBlock

    messages = _history(12, result_size=500)
    original_contents = [
        block.content
        for msg in messages
        for block in msg.content
        if isinstance(block, ToolResultBlock)
    ]

    new_messages, n_elided = micro_compact(messages, keep_recent_turns=10)

    assert n_elided == 2
    # Input messages and blocks are untouched (shared with full_history).
    after_contents = [
        block.content
        for msg in messages
        for block in msg.content
        if isinstance(block, ToolResultBlock)
    ]
    assert after_contents == original_contents
    # Untouched messages are reused by identity; changed ones are new objects.
    assert new_messages[0] is messages[0]
    assert new_messages[-1] is messages[-1]


def test_micro_compact_noop_returns_zero() -> None:
    from linch.compaction import micro_compact

    messages = _history(3, result_size=500)

    new_messages, n_elided = micro_compact(messages, keep_recent_turns=10)

    assert n_elided == 0
    assert new_messages is messages

    # Already-elided view: second pass finds nothing to elide.
    once, n = micro_compact(_history(12), keep_recent_turns=10)
    assert n == 2
    twice, n2 = micro_compact(once, keep_recent_turns=10)
    assert n2 == 0
    assert twice is once


# ── integration: providers and tools ─────────────────────────────────────────


class BigTool:
    """Returns a payload of caller-controlled size."""

    name = "BigTool"
    description = "Return n filler characters."
    input_schema = {"type": "object", "properties": {"n": {"type": "integer"}}}
    scope = "read"
    parallel = True

    def validate(self, raw: dict[str, object]) -> dict[str, object]:
        return raw

    def summarize(self, input: dict[str, object]) -> str:
        return f"BigTool({input.get('n')})"

    async def execute(self, input: dict[str, object], ctx: Any) -> Any:
        from linch.tools import ToolResult

        n = int(input.get("n", 0))  # type: ignore[arg-type]
        return ToolResult(content="x" * n, summary=f"{n} chars")


class LadderProvider:
    """Scripted main-turn behaviors; summarize calls answered separately.

    Behaviors: ``("tool", n)`` → BigTool call returning n chars;
    ``("raise_cle", 0)`` → raise ContextLengthError; ``("text", 0)`` → end turn.
    Summarize calls (req.tools == []) yield a fixed summary and are counted.
    """

    id = "fake"

    def __init__(self, behaviors: list[tuple[str, int]], window: int = 10_000_000) -> None:
        self.behaviors = behaviors
        self.window = window
        self.calls = 0
        self.summarize_calls = 0

    def context_window(self, model: str) -> int:
        return self.window

    async def stream(self, req: Any) -> AsyncIterator[dict[str, object]]:
        from linch.errors import ContextLengthError
        from linch.types import Usage

        if not req.tools:
            self.summarize_calls += 1
            yield {"type": "message_start", "model": req.model}
            yield {"type": "text_delta", "text": "summary of earlier work"}
            yield {"type": "message_end", "stop_reason": "end_turn", "usage": Usage()}
            return

        behavior, n = self.behaviors[self.calls]
        self.calls += 1
        if behavior == "raise_cle":
            raise ContextLengthError("prompt too long")
        yield {"type": "message_start", "model": req.model}
        if behavior == "tool":
            yield {"type": "tool_use_start", "id": f"call_{self.calls}", "name": "BigTool"}
            yield {
                "type": "tool_use_input_delta",
                "id": f"call_{self.calls}",
                "json_delta": f'{{"n":{n}}}',
            }
            yield {"type": "tool_use_end", "id": f"call_{self.calls}"}
            stop_reason = "tool_use"
        else:
            yield {"type": "text_delta", "text": "done"}
            stop_reason = "end_turn"
        yield {
            "type": "message_end",
            "stop_reason": stop_reason,
            "usage": Usage(input_tokens=10),
        }


def _char_estimator(messages: list[Any], model: str) -> int:
    """Token estimator that counts tool-result chars too (1 char = 1 token)."""
    from linch.types import TextBlock, ToolResultBlock

    total = 0
    for message in messages:
        for block in message.content:
            if isinstance(block, TextBlock):
                total += len(block.text)
            elif isinstance(block, ToolResultBlock) and isinstance(block.content, str):
                total += len(block.content)
    return total


def _make_agent(provider: Any, **kwargs: Any) -> Any:
    from linch import Agent
    from linch.sessions import InMemorySessionStore
    from linch.tools import ToolRegistry

    tools = ToolRegistry()
    tools.register(BigTool())
    return Agent(
        model="gpt-5",
        provider=provider,
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
        cwd=".",
        tools=tools,
        **kwargs,
    )


# ── integration: proactive rung in maybe_compact ─────────────────────────────


async def test_maybe_compact_micro_rung_avoids_llm_summarization() -> None:
    from linch.compaction import CompactionLadder

    # window 10_000, threshold 8_000.  Turn 1 leaves a 9_000-char result
    # (elidable once turn 2's assistant exists), turn 2 leaves 500.  At turn 3
    # the estimate (~9_540) is over threshold; eliding the old result drops it
    # under — no summarization needed.
    provider = LadderProvider(
        [("tool", 9_000), ("tool", 500), ("text", 0)],
        window=10_000,
    )
    agent = _make_agent(
        provider,
        compaction_ladder=CompactionLadder(keep_recent_turns=1),
        token_estimator=_char_estimator,
        max_output_tokens=10,
    )
    session = await agent.session()

    events = [event async for event in session.run("go")]

    compactions = [e for e in events if e.type == "compaction"]
    assert [e.strategy for e in compactions] == ["micro"]
    assert compactions[0].tokens_after < compactions[0].tokens_before
    assert provider.summarize_calls == 0
    assert len(_elided_results(session.provider_view)) == 1
    assert events[-1].type == "result"
    assert events[-1].subtype == "success"


async def test_maybe_compact_falls_through_to_full_strategy_when_micro_insufficient() -> None:
    from linch import DetailedCompaction
    from linch.compaction import CompactionLadder

    # Old result is small (500), the protected recent one is huge (9_000):
    # eliding can't get under threshold, so the full strategy summarizes.
    provider = LadderProvider(
        [("tool", 500), ("tool", 9_000), ("text", 0)],
        window=10_000,
    )
    agent = _make_agent(
        provider,
        compaction_ladder=CompactionLadder(keep_recent_turns=1),
        compaction=DetailedCompaction(keep_recent_turns=1),
        token_estimator=_char_estimator,
        max_output_tokens=10,
    )
    session = await agent.session()

    events = [event async for event in session.run("go")]

    compactions = [e for e in events if e.type == "compaction"]
    assert [e.strategy for e in compactions] == ["detailed-continuation-keep-recent-10"]
    assert provider.summarize_calls == 1
    assert events[-1].subtype == "success"


async def test_forced_compaction_resets_read_tracker() -> None:
    from linch import DetailedCompaction
    from linch.compaction import CompactionLadder

    provider = LadderProvider(
        [("tool", 500), ("tool", 9_000), ("text", 0)],
        window=10_000,
    )
    agent = _make_agent(
        provider,
        compaction_ladder=CompactionLadder(keep_recent_turns=1),
        compaction=DetailedCompaction(keep_recent_turns=1),
        token_estimator=_char_estimator,
        max_output_tokens=10,
    )
    session = await agent.session()
    session.file_read_tracker.add("/seen.py")

    events = [event async for event in session.run("go")]

    # A forced compaction fired, and the read tracker was cleared so the agent
    # must re-read a file whose contents may have left the context.
    assert [e.strategy for e in events if e.type == "compaction"] == [
        "detailed-continuation-keep-recent-10"
    ]
    assert "/seen.py" not in session.file_read_tracker


async def test_compaction_without_ladder_keeps_read_tracker() -> None:
    from linch import DetailedCompaction

    # Same forcing setup, but no ladder configured → default byte-identical:
    # the read tracker survives the compaction untouched.
    provider = LadderProvider(
        [("tool", 500), ("tool", 9_000), ("text", 0)],
        window=10_000,
    )
    agent = _make_agent(
        provider,
        compaction=DetailedCompaction(keep_recent_turns=1),
        token_estimator=_char_estimator,
        max_output_tokens=10,
    )
    session = await agent.session()
    session.file_read_tracker.add("/seen.py")

    events = [event async for event in session.run("go")]

    assert [e.strategy for e in events if e.type == "compaction"] == [
        "detailed-continuation-keep-recent-10"
    ]
    assert "/seen.py" in session.file_read_tracker


# ── integration: reactive rungs on ContextLengthError ────────────────────────


async def test_reactive_micro_then_forced_on_context_length_error() -> None:
    from linch import DetailedCompaction
    from linch.compaction import CompactionLadder

    provider = LadderProvider(
        [("tool", 100), ("tool", 100), ("raise_cle", 0), ("raise_cle", 0), ("text", 0)]
    )
    agent = _make_agent(
        provider,
        compaction_ladder=CompactionLadder(keep_recent_turns=1),
        compaction=DetailedCompaction(keep_recent_turns=1),
    )
    session = await agent.session()

    events = [event async for event in session.run("go")]

    compactions = [e for e in events if e.type == "compaction"]
    assert [e.strategy for e in compactions] == [
        "micro",
        "detailed-continuation-keep-recent-10",
    ]
    assert provider.summarize_calls == 1
    assert events[-1].type == "result"
    assert events[-1].subtype == "success"


async def test_reactive_compaction_resets_read_tracker() -> None:
    from linch import DetailedCompaction
    from linch.compaction import CompactionLadder

    provider = LadderProvider(
        [("tool", 100), ("tool", 100), ("raise_cle", 0), ("raise_cle", 0), ("text", 0)]
    )
    agent = _make_agent(
        provider,
        compaction_ladder=CompactionLadder(keep_recent_turns=1),
        compaction=DetailedCompaction(keep_recent_turns=1),
    )
    session = await agent.session()
    session.file_read_tracker.add("/seen.py")

    events = [event async for event in session.run("go")]

    # Reactive recovery ran (micro then forced); the tracker reset on the way out.
    assert [e.strategy for e in events if e.type == "compaction"] == [
        "micro",
        "detailed-continuation-keep-recent-10",
    ]
    assert "/seen.py" not in session.file_read_tracker


async def test_circuit_breaker_surfaces_error_after_max_forced() -> None:
    from linch import DetailedCompaction
    from linch.compaction import CompactionLadder

    # Provider raises on every main call of turn 3: micro rung once, then two
    # forced compactions, then the error surfaces.
    provider = LadderProvider(
        [("tool", 100), ("tool", 100)] + [("raise_cle", 0)] * 4,
    )
    agent = _make_agent(
        provider,
        compaction_ladder=CompactionLadder(keep_recent_turns=1, max_forced_compactions=2),
        compaction=DetailedCompaction(keep_recent_turns=1),
    )
    session = await agent.session()

    events = [event async for event in session.run("go")]

    compactions = [e for e in events if e.type == "compaction"]
    assert [e.strategy for e in compactions][:1] == ["micro"]
    assert len(compactions) == 3  # micro + 2 forced
    error_events = [e for e in events if e.type == "error"]
    assert any(e.error.get("name") == "ContextLengthError" for e in error_events)
    assert events[-1].type == "result"
    assert events[-1].subtype == "error"


# ── regression pin: ladder disabled is byte-identical ─────────────────────────


async def test_ladder_disabled_is_byte_identical() -> None:
    from linch import DetailedCompaction

    # Single forced retry succeeds (existing behavior).
    provider = LadderProvider([("tool", 100), ("raise_cle", 0), ("text", 0)])
    agent = _make_agent(provider, compaction=DetailedCompaction(keep_recent_turns=1))
    session = await agent.session()

    events = [event async for event in session.run("go")]
    compactions = [e for e in events if e.type == "compaction"]
    assert [e.strategy for e in compactions] == ["detailed-continuation-keep-recent-10"]
    assert events[-1].subtype == "success"

    # Second ContextLengthError in the same turn surfaces (existing behavior).
    provider2 = LadderProvider([("tool", 100), ("raise_cle", 0), ("raise_cle", 0)])
    agent2 = _make_agent(provider2, compaction=DetailedCompaction(keep_recent_turns=1))
    session2 = await agent2.session()

    events2 = [event async for event in session2.run("go")]
    compactions2 = [e for e in events2 if e.type == "compaction"]
    assert len(compactions2) == 1
    assert events2[-1].type == "result"
    assert events2[-1].subtype == "error"
