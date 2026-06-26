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
