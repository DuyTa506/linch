"""Tests for provider-agnostic compaction (decoupled from openai_responses)."""

from __future__ import annotations

import pytest

from linch.abort import AbortContext
from linch.compaction import (
    CompactionContext,
    DefaultCompaction,
    DetailedCompaction,
    maybe_compact,
    summarize_with_provider,
)
from linch.types import Message, TextBlock, Usage

# ── Fake provider that is NOT the OpenAI Responses provider ──────────────────


class _FakeNonOpenAIProvider:
    id = "not-openai"

    _stream_calls: list

    def __init__(self) -> None:
        self._stream_calls = []

    def context_window(self, model: str) -> int:
        return 1024  # small so compaction triggers easily

    async def stream(self, req):
        self._stream_calls.append(req)
        yield {"type": "message_start", "model": req.model}
        yield {"type": "text_delta", "text": "Summary of conversation."}
        yield {
            "type": "message_end",
            "stop_reason": "end_turn",
            "usage": Usage(),
            "provider_metadata": None,
        }


_DETAILED_SUMMARY = """\
<summary>
1. Primary Request and Intent: Implement the requested change.
2. Key Information and Artifacts: src/example.py was inspected.
3. Errors and Fixes: None.
4. Pending Tasks: Run tests.
5. Current Work: Preparing verification.
6. Next Step: Run pytest.
</summary>"""


class _FakeDetailedSummaryProvider(_FakeNonOpenAIProvider):
    async def stream(self, req):
        self._stream_calls.append(req)
        yield {"type": "message_start", "model": req.model}
        yield {"type": "text_delta", "text": _DETAILED_SUMMARY}
        yield {
            "type": "message_end",
            "stop_reason": "end_turn",
            "usage": Usage(),
            "provider_metadata": None,
        }


def _make_messages(n: int = 30) -> list[Message]:
    """Create alternating user/assistant messages."""
    msgs = []
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        msgs.append(Message(role=role, content=[TextBlock(text="word " * 100)]))
    return msgs


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_summarize_uses_provider_stream_directly():
    """summarize_with_provider calls provider.stream() without re-wrapping."""
    provider = _FakeNonOpenAIProvider()
    signal = AbortContext()
    older = _make_messages(6)

    summary = await summarize_with_provider(provider, "model-x", older, signal)

    assert summary == "Summary of conversation."
    assert len(provider._stream_calls) == 1
    # The request model must match what was passed
    assert provider._stream_calls[0].model == "model-x"


@pytest.mark.asyncio
async def test_default_compaction_uses_non_openai_provider():
    """DefaultCompaction.compact works with a non-OpenAI provider."""
    provider = _FakeNonOpenAIProvider()
    signal = AbortContext()
    messages = _make_messages(30)  # enough for compaction to have an "older" portion

    strategy = DefaultCompaction()
    ctx = CompactionContext(messages=messages, model="model-x", signal=signal)
    result = await strategy.compact(ctx, provider)

    # Should have compacted; the summary message should be present
    assert any(
        isinstance(m.content[0], TextBlock) and "summary" in m.content[0].text.lower()
        for m in result
    )
    assert len(result) < len(messages)
    assert len(provider._stream_calls) == 1


@pytest.mark.asyncio
async def test_detailed_compaction_uses_no_tool_provider_request():
    provider = _FakeDetailedSummaryProvider()
    signal = AbortContext()
    messages = _make_messages(30)

    strategy = DetailedCompaction()
    ctx = CompactionContext(messages=messages, model="model-x", signal=signal)
    result = await strategy.compact(ctx, provider)

    assert len(provider._stream_calls) == 1
    request = provider._stream_calls[0]
    assert request.model == "model-x"
    assert request.tools == []
    assert "CRITICAL: Respond with TEXT ONLY" in request.system[0].text

    summary = result[0].content[0]
    assert isinstance(summary, TextBlock)
    assert "<detailed summary of earlier conversation>" in summary.text
    for section in (
        "Primary Request and Intent",
        "Key Information and Artifacts",
        "Errors and Fixes",
        "Pending Tasks",
        "Current Work",
        "Next Step",
    ):
        assert section in summary.text


@pytest.mark.asyncio
async def test_default_compaction_accepts_custom_prompt():
    """A non-coding embedder can reword the summary without writing a strategy."""
    provider = _FakeNonOpenAIProvider()
    signal = AbortContext()
    messages = _make_messages(30)

    custom = "Summarize for a customer-support transcript. Capture open tickets only."
    strategy = DefaultCompaction(prompt=custom)
    ctx = CompactionContext(messages=messages, model="model-x", signal=signal)
    await strategy.compact(ctx, provider)

    assert provider._stream_calls[0].system[0].text == custom


@pytest.mark.asyncio
async def test_default_compaction_prompt_defaults_unchanged():
    """Omitting prompt keeps the coding-oriented default (byte-identical)."""
    provider = _FakeNonOpenAIProvider()
    signal = AbortContext()
    messages = _make_messages(30)

    strategy = DefaultCompaction()
    ctx = CompactionContext(messages=messages, model="model-x", signal=signal)
    await strategy.compact(ctx, provider)

    assert "Summarize the conversation so far" in provider._stream_calls[0].system[0].text


@pytest.mark.asyncio
async def test_general_summary_prompt_is_domain_neutral():
    """A ready-made non-coding default: importable and free of file/path framing."""
    from linch import GENERAL_SUMMARY_PROMPT

    provider = _FakeNonOpenAIProvider()
    signal = AbortContext()
    messages = _make_messages(30)

    strategy = DefaultCompaction(prompt=GENERAL_SUMMARY_PROMPT)
    ctx = CompactionContext(messages=messages, model="model-x", signal=signal)
    await strategy.compact(ctx, provider)

    sent = provider._stream_calls[0].system[0].text
    assert sent == GENERAL_SUMMARY_PROMPT
    assert "Files" not in sent  # no coding/filesystem assumption


@pytest.mark.asyncio
async def test_detailed_compaction_accepts_custom_prompt():
    provider = _FakeDetailedSummaryProvider()
    signal = AbortContext()
    messages = _make_messages(30)

    custom = "Custom continuation summary instructions."
    strategy = DetailedCompaction(prompt=custom)
    ctx = CompactionContext(messages=messages, model="model-x", signal=signal)
    await strategy.compact(ctx, provider)

    assert provider._stream_calls[0].system[0].text == custom


def test_detailed_prompt_is_domain_neutral():
    """The opt-in detailed handoff must not assume a coding host, while keeping
    its continuation-safe structure (6 sections + text-only guard)."""
    from linch.compaction import _DETAILED_SUMMARY_PROMPT

    p = _DETAILED_SUMMARY_PROMPT
    assert "CRITICAL: Respond with TEXT ONLY" in p
    for section in (
        "Primary Request and Intent",
        "Errors and Fixes",
        "Pending Tasks",
        "Current Work",
        "Next Step",
    ):
        assert section in p
    for coding_term in ("Code Sections", "API names", "public interfaces", "test results"):
        assert coding_term not in p


@pytest.mark.asyncio
async def test_maybe_compact_uses_provider_context_window():
    """maybe_compact calls agent.provider.context_window, not openai_responses.context_window."""
    fake_provider = _FakeNonOpenAIProvider()
    # fake_provider.context_window returns 1024, so with big messages compaction fires

    # Build a minimal fake agent/session
    class FakeAgent:
        model = "model-x"
        provider = fake_provider
        max_output_tokens = None
        compaction = None
        token_estimator = None

    class FakeSession:
        provider_view: list
        last_usage: object
        last_compaction_info: dict | None
        compaction_retry_used_this_turn = False

        def __init__(self):
            # Many big messages to exceed 80% of the 1024 token limit
            self.provider_view = _make_messages(40)
            self.last_usage = object()  # non-None so maybe_compact proceeds
            self.last_compaction_info = None

        def mark_compaction_used(self):
            self.compaction_retry_used_this_turn = True

    agent = FakeAgent()
    session = FakeSession()
    signal = AbortContext()

    fired = await maybe_compact(session, agent, signal)

    assert fired is True
    assert len(fake_provider._stream_calls) >= 1  # provider.stream was used for summarization


class _FakeAgent:
    model = "model-x"
    max_retries = 5
    max_output_tokens = None
    compaction = None
    compaction_ladder = None
    token_estimator = None

    def __init__(self, provider):
        self.provider = provider


class _FakeSession:
    def __init__(self, n_messages: int = 40):
        self.provider_view = _make_messages(n_messages)
        self.last_usage = object()  # non-None so maybe_compact proceeds
        self.last_compaction_info = None
        self.compaction_retry_used_this_turn = False

    def mark_compaction_used(self):
        self.compaction_retry_used_this_turn = True


class _FlakyThenOkProvider(_FakeNonOpenAIProvider):
    """Fails with a retryable ProviderError on the first N stream() calls."""

    def __init__(self, fail_times: int) -> None:
        super().__init__()
        self._fail_times = fail_times
        self._call_count = 0

    async def stream(self, req):
        from linch.errors import ProviderError

        self._call_count += 1
        if self._call_count <= self._fail_times:
            raise ProviderError("rate limited", retryable=True)
        async for ev in super().stream(req):
            yield ev


class _AlwaysFailsProvider(_FakeNonOpenAIProvider):
    async def stream(self, req):
        from linch.errors import ProviderError

        raise ProviderError("rate limited", retryable=True)
        yield {}  # pragma: no cover - unreachable, keeps this an async generator


@pytest.mark.asyncio
async def test_maybe_compact_resilient_retries_transient_provider_error():
    """A transient retryable ProviderError during proactive-compaction
    summarization must not crash the run — it should retry like the main
    turn loop does, and still compact once the provider recovers."""
    from linch.loop.streaming import maybe_compact_resilient

    provider = _FlakyThenOkProvider(fail_times=2)
    agent = _FakeAgent(provider)
    session = _FakeSession()
    signal = AbortContext()

    fired = await maybe_compact_resilient(session, agent, signal)

    assert fired is True
    assert provider._call_count == 3


@pytest.mark.asyncio
async def test_maybe_compact_resilient_skips_compaction_when_exhausted():
    """When every retry is exhausted, proactive compaction is skipped (not
    raised) so the run can proceed to the turn call instead of crashing."""
    from linch.loop.streaming import maybe_compact_resilient

    provider = _AlwaysFailsProvider()
    agent = _FakeAgent(provider)
    agent.max_retries = 2  # keep the test fast
    session = _FakeSession()
    original_view = list(session.provider_view)
    signal = AbortContext()

    fired = await maybe_compact_resilient(session, agent, signal)

    assert fired is False
    assert session.provider_view == original_view  # untouched, no partial mutation


@pytest.mark.asyncio
async def test_maybe_compact_resilient_resets_read_tracker_after_partial_micro_elision():
    """If micro-compaction mutates provider_view but the follow-up summarization
    exhausts retries and maybe_compact_resilient degrades to False, the read
    tracker must still be reset -- otherwise the Edit tool's has_read gate would
    let the model blind-edit content micro-compaction already elided."""
    from linch.compaction import CompactionLadder
    from linch.loop.streaming import maybe_compact_resilient
    from linch.types import Message, TextBlock, ToolResultBlock, ToolUseBlock

    def _history_with_tool_results(turns: int, result_size: int) -> list[Message]:
        messages: list[Message] = [Message(role="user", content=[TextBlock(text="go")])]
        for i in range(turns):
            messages.append(
                Message(
                    role="assistant",
                    content=[ToolUseBlock(id=f"call_{i}", name="BigTool", input={})],
                )
            )
            messages.append(
                Message(
                    role="user",
                    content=[ToolResultBlock(tool_use_id=f"call_{i}", content="x" * result_size)],
                )
            )
        return messages

    class _FakeTracker:
        def __init__(self):
            self.cleared = False

        def clear(self):
            self.cleared = True

    provider = _AlwaysFailsProvider()
    agent = _FakeAgent(provider)
    agent.max_retries = 1  # keep the test fast
    agent.compaction_ladder = CompactionLadder(keep_recent_turns=2)

    session = _FakeSession()
    # 12 turns, keep_recent_turns=2 -> 10 old tool results elided but the 2 kept
    # (2000 chars each) still push the projection back over the 0.8*1024 limit,
    # so maybe_compact falls through to the (always-failing) summarization call.
    session.provider_view = _history_with_tool_results(12, result_size=2000)
    session.file_read_tracker = _FakeTracker()
    signal = AbortContext()

    fired = await maybe_compact_resilient(session, agent, signal)

    assert fired is False
    assert session.file_read_tracker.cleared is True


def test_compaction_module_has_no_openai_responses_import():
    """Verify compaction.py no longer imports openai_responses at module level."""
    import ast
    import inspect

    import linch.compaction as mod

    src = inspect.getsource(mod)
    tree = ast.parse(src)

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [alias.name for alias in getattr(node, "names", [])]
            module = getattr(node, "module", "") or ""
            # Should not import from openai_responses at the top level
            if "openai_responses" in module:
                raise AssertionError(
                    f"compaction.py has a top-level import from openai_responses: "
                    f"module={module}, names={names}"
                )
