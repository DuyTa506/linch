from __future__ import annotations

import pytest

from linch.abort import AbortContext
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


def _make_messages(n: int = 30) -> list[Message]:
    """Create alternating user/assistant messages."""
    msgs = []
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        msgs.append(Message(role=role, content=[TextBlock(text="word " * 100)]))
    return msgs


# ── Tests ─────────────────────────────────────────────────────────────────────


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


class _AlwaysFailsProvider(_FakeNonOpenAIProvider):
    async def stream(self, req):
        from linch.errors import ProviderError

        raise ProviderError("rate limited", retryable=True)
        yield {}  # pragma: no cover - unreachable, keeps this an async generator


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
