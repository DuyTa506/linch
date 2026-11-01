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
from linch.session_log import SessionLog
from linch.types import Message, TextBlock
from tests.support.providers import ScriptedProvider, TextTurn


class _LoggedViews:
    """Mixin: back a fake session's views with a real single-log (Phase 4)."""

    session_log: SessionLog

    def _seed_views(self, provider_view, full_history=None) -> None:
        self.session_log = SessionLog.seed(
            historical=provider_view if full_history is None else full_history,
            visible=provider_view,
        )

    @property
    def provider_view(self):
        return self.session_log.provider_view

    @property
    def full_history(self):
        return self.session_log.full_history


# ── Fake provider that is NOT the OpenAI Responses provider ──────────────────


class _FakeNonOpenAIProvider(ScriptedProvider):
    id = "not-openai"

    def __init__(self, text: str = "Summary of conversation.") -> None:
        super().__init__([TextTurn(text)], context_window=1024)

    @property
    def _stream_calls(self):
        return self.requests


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
    def __init__(self) -> None:
        super().__init__(_DETAILED_SUMMARY)


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
    assert provider._stream_calls[0].model == "model-x"


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
@pytest.mark.parametrize("prompt_kind", ["default", "custom", "general", "detailed"])
async def test_compaction_prompts(prompt_kind: str):
    """Compaction strategies forward built-in and host-supplied prompts."""
    from linch import GENERAL_SUMMARY_PROMPT

    custom = "Summarize for a customer-support transcript. Capture open tickets only."
    if prompt_kind == "detailed":
        provider = _FakeDetailedSummaryProvider()
        strategy = DetailedCompaction(prompt="Custom continuation summary instructions.")
        expected = "Custom continuation summary instructions."
    else:
        provider = _FakeNonOpenAIProvider()
        prompt = {"custom": custom, "general": GENERAL_SUMMARY_PROMPT}.get(prompt_kind)
        strategy = DefaultCompaction(prompt=prompt) if prompt is not None else DefaultCompaction()
        expected = prompt
    ctx = CompactionContext(messages=_make_messages(30), model="model-x", signal=AbortContext())
    result = await strategy.compact(ctx, provider)

    sent = provider._stream_calls[0].system[0].text
    assert provider._stream_calls[0].model == "model-x"
    if prompt_kind == "default":
        assert "Summarize the conversation so far" in sent
        assert any(
            isinstance(message.content[0], TextBlock)
            and "summary" in message.content[0].text.lower()
            for message in result
        )
        assert len(result) < len(ctx.messages)
    elif prompt_kind == "detailed":
        from linch.compaction import _DETAILED_SUMMARY_PROMPT

        assert sent == expected
        assert "CRITICAL: Respond with TEXT ONLY" in _DETAILED_SUMMARY_PROMPT
        for coding_term in ("Code Sections", "API names", "public interfaces", "test results"):
            assert coding_term not in _DETAILED_SUMMARY_PROMPT
    else:
        assert sent == expected
    if prompt_kind == "general":
        assert "Files" not in sent


@pytest.mark.asyncio
@pytest.mark.parametrize("large", [True, False])
async def test_maybe_compact_usage_thresholds(large: bool):
    provider = _FakeNonOpenAIProvider()
    session = _FakeSession()
    session.last_usage = None
    agent = _FakeAgent(provider)
    agent.max_output_tokens = 10
    if large:
        history_before = list(session.full_history)
    else:
        session._seed_views(_make_messages(2))
        agent.token_estimator = lambda messages, model: 1
    session.agent = agent

    fired = await maybe_compact(session, agent, AbortContext())

    if large:
        assert fired is True
        assert provider._stream_calls[0].model == "model-x"
        assert list(session.full_history) == history_before
        assert all(a is not b for a, b in zip(session.full_history, history_before, strict=True))
    else:
        assert fired is False
        assert len(session.provider_view) == 2
        assert provider._stream_calls == []


@pytest.mark.asyncio
async def test_compaction_uses_active_fallback_model_consistently():
    class TrackingProvider(_FakeNonOpenAIProvider):
        def __init__(self) -> None:
            super().__init__()
            self.window_models: list[str] = []

        def context_window(self, model: str) -> int:
            self.window_models.append(model)
            return 1024

    provider = TrackingProvider()
    estimated_models: list[str] = []

    agent = _FakeAgent(provider)
    agent.model = "primary-model"
    agent.max_output_tokens = 10
    agent.token_estimator = lambda messages, model: (
        estimated_models.append(model)
        or sum(
            len(block.text)
            for message in messages
            for block in message.content
            if isinstance(block, TextBlock)
        )
        // 4
    )
    session = _FakeSession()
    session.last_usage = None
    session.active_model = "fallback-model"
    session.agent = agent

    fired = await maybe_compact(session, agent, AbortContext())

    assert fired is True
    assert provider.window_models
    assert set(provider.window_models) == {"fallback-model"}
    assert set(estimated_models) == {"fallback-model"}
    assert provider._stream_calls[0].model == "fallback-model"
    assert session.last_compaction_info["model"] == "fallback-model"


class _FakeAgent:
    model = "model-x"
    max_retries = 5
    max_output_tokens = None
    compaction = None
    compaction_ladder = None
    token_estimator = None

    def __init__(self, provider):
        from linch import Context

        self.provider = provider
        self.context = Context()


class _FakeSession(_LoggedViews):
    def __init__(self, n_messages: int = 40):
        self._seed_views(_make_messages(n_messages))
        self.id = "fake-session"
        self.active_model = None
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
@pytest.mark.parametrize("case", ["recovers", "exhausted"])
async def test_maybe_compact_resilient_provider_errors(case: str):
    from linch.loop.streaming import maybe_compact_resilient

    provider = _FlakyThenOkProvider(fail_times=2) if case == "recovers" else _AlwaysFailsProvider()
    agent = _FakeAgent(provider)
    if case == "exhausted":
        agent.max_retries = 2
    session = _FakeSession()
    session.agent = agent
    original_view = list(session.provider_view)
    fired = await maybe_compact_resilient(session, agent, AbortContext())

    if case == "recovers":
        assert fired is True
        assert provider._call_count == 3
    else:
        assert fired is False
        assert list(session.provider_view) == original_view


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
    session.agent = agent
    # 12 turns, keep_recent_turns=2 -> 10 old tool results elided but the 2 kept
    # (2000 chars each) still push the projection back over the 0.8*1024 limit,
    # so maybe_compact falls through to the (always-failing) summarization call.
    session._seed_views(_history_with_tool_results(12, result_size=2000))
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
