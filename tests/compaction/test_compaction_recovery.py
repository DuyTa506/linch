"""Crash/resume regressions for paid compaction projection recovery."""

from __future__ import annotations

from typing import Any

import pytest

from tests.support.agents import make_agent
from tests.support.stores import memory_run_store, memory_session_store


class _Crash(BaseException):
    pass


class _CrashOnNthAssistant:
    def __init__(self, store: Any, n: int) -> None:
        self.store = store
        self.n = n

    def __getattr__(self, name: str) -> Any:
        return getattr(self.store, name)

    async def append_messages(self, session_id: str, messages: list[Any]) -> Any:
        if any(message.role == "assistant" for message in messages):
            self.n -= 1
            if self.n == 0:
                raise _Crash("process died")
        return await self.store.append_messages(session_id, messages)


class _CrashAfterMicroTail:
    def __init__(self, store: Any) -> None:
        self.store = store

    def __getattr__(self, name: str) -> Any:
        return getattr(self.store, name)

    async def save_checkpoint(self, run_id: str, checkpoint: Any, **kwargs: Any) -> Any:
        result = await self.store.save_checkpoint(run_id, checkpoint, **kwargs)
        self._check(checkpoint)
        return result

    async def save_checkpoint_with_events(
        self, run_id: str, checkpoint: Any, events: list[Any], **kwargs: Any
    ) -> Any:
        result = await self.store.save_checkpoint_with_events(run_id, checkpoint, events, **kwargs)
        self._check(checkpoint)
        return result

    def _check(self, checkpoint: Any) -> None:
        # Group commit holds intermediate phases, so crash after the first run
        # write that carries state past turn 2's tool results.
        if checkpoint.turn_index > 2 or (
            checkpoint.turn_index == 2
            and checkpoint.phase in ("tool_results_appended", "turn_complete")
        ):
            raise _Crash("process died after the micro snapshot's durable tail")


class _SummaryProvider:
    id = "summary-test"

    def __init__(self, *, crash_primary: bool = False) -> None:
        self.crash_primary = crash_primary
        self.calls: list[Any] = []
        self.primary: list[list[Any]] = []

    def context_window(self, model: str) -> int:
        return 1_000

    async def stream(self, request: Any):
        from linch.types import Usage

        self.calls.append(request)
        summary = any("Summarize" in block.text for block in request.system)
        if not summary:
            self.primary.append(list(request.messages))
            if self.crash_primary:
                raise _Crash("process died during primary call")
        yield {"type": "message_start", "model": "claude-haiku-4-5"}
        yield {"type": "text_delta", "text": "paid summary" if summary else "answer"}
        yield {"type": "message_end", "stop_reason": "end_turn", "usage": Usage(11, 2)}


class _ToolProvider:
    id = "tool-compaction-test"

    def __init__(self, *, final: bool = False) -> None:
        self.final = final
        self.calls: list[Any] = []
        self.primary = 0

    def context_window(self, model: str) -> int:
        return 1_000

    async def stream(self, request: Any):
        from linch.types import Usage

        self.calls.append(request)
        summary = any("Summarize" in block.text for block in request.system)
        yield {"type": "message_start", "model": "claude-haiku-4-5"}
        if summary:
            yield {"type": "text_delta", "text": "paid summary"}
            stop_reason = "end_turn"
        elif self.final:
            yield {"type": "text_delta", "text": "done"}
            stop_reason = "end_turn"
        else:
            self.primary += 1
            tool_id = f"tool-{self.primary}"
            yield {"type": "tool_use_start", "id": tool_id, "name": "Big"}
            yield {
                "type": "tool_use_input_delta",
                "id": tool_id,
                "json_delta": f'{{"turn":{self.primary}}}',
            }
            yield {"type": "tool_use_end", "id": tool_id}
            stop_reason = "tool_use"
        yield {
            "type": "message_end",
            "stop_reason": stop_reason,
            "usage": Usage(11, 2),
        }


def _agent(sessions: Any, runs: Any, provider: Any, **overrides: Any) -> Any:
    from linch.config import FeatureFlags

    return make_agent(
        provider,
        model="m",
        session_store=sessions,
        run_store=runs,
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        max_output_tokens=100,
        permissions=None,
        cwd=None,
        reliability=None,
        **overrides,
    )


def _seed_messages(count: int = 30):
    from linch.types import Message, TextBlock

    return [
        Message(
            role="user" if index % 2 == 0 else "assistant",
            content=[TextBlock(text=f"seed {index} " * 120)],
        )
        for index in range(count)
    ]


def test_paid_target_recipe_requires_an_ordered_retained_suffix() -> None:
    from linch.compaction import _paid_target_from_recipe, _source_message_fingerprints
    from linch.types import Message, TextBlock

    base = [
        Message(role=role, content=[TextBlock(text=text)])
        for role, text in [
            ("user", "prefix"),
            ("assistant", "first answer"),
            ("user", "first question"),
            ("assistant", "second answer"),
            ("user", "last question"),
        ]
    ]
    for corruption in ("no_prefix", "reordered_tail", "missing_tail_message"):
        history = list(base)
        source = list(base)
        keep = 2
        if corruption == "no_prefix":
            history = source = [base[1]]
            keep = 1
        elif corruption == "reordered_tail":
            source[1], source[3] = source[3], source[1]
        else:
            del source[2]
        recipe = {
            "source_message_fingerprints": _source_message_fingerprints(source),
            "strategy_config": {"keep_recent_turns": keep, "implementation": "default_v1"},
            "reminder_fingerprints": [],
        }

        assert _paid_target_from_recipe(recipe, history, {}, "paid summary", None) is None


@pytest.mark.parametrize(
    ("lose_snapshot", "paid_compactions"),
    [
        (False, 1),  # snapshot present: resume restores the materialized view directly.
        (True, 2),  # snapshot lost, two stacked compactions: hardest rebuild-from-log case.
    ],
)
async def test_paid_compaction_from_an_earlier_run_is_resumable(
    paid_compactions: int, lose_snapshot: bool
) -> None:
    from linch.types import TextBlock
    from tests.support.tools import StubTool, tool_registry

    sessions, runs = memory_session_store(), memory_run_store()
    first = await _agent(sessions, runs, _SummaryProvider()).session(id="s")
    await first.append(_seed_messages())
    [event async for event in first.run("go")]
    assert any(
        isinstance(block, TextBlock) and "paid summary" in block.text
        for message in first.provider_view
        for block in message.content
    )

    crashed_provider = _ToolProvider(final=paid_compactions == 1)
    crashed = await _agent(
        _CrashOnNthAssistant(sessions, paid_compactions),
        runs,
        crashed_provider,
        tools=tool_registry(StubTool("Big")),
    ).session(id="s")
    run_id = None
    with pytest.raises(_Crash):
        async for event in crashed.run("again"):
            if event.type == "system":
                run_id = event.run_id
    assert run_id is not None
    assert (
        sum(
            "Summarize" in block.text
            for request in crashed_provider.calls
            for block in request.system
        )
        == paid_compactions
    )
    assert await sessions.load_provider_snapshot("s") is not None
    if lose_snapshot:
        sessions._snapshots.clear()

    resumed_provider = _ToolProvider(final=True)
    resumed = await _agent(
        sessions, runs, resumed_provider, tools=tool_registry(StubTool("Big"))
    ).session(id="s")
    events = [event async for event in resumed.resume(run_id)]

    assert not [event for event in events if event.type == "error"]
    assert events[-1].type == "result"
    assert events[-1].subtype == "success"
    assert not any(
        "Summarize" in block.text for request in resumed_provider.calls for block in request.system
    )


@pytest.mark.parametrize(
    ("prior_compaction", "tail_after_micro"),
    [
        (False, True),
        (True, True),
        # Crashes inside the session-store append of an assistant message
        # (_CrashOnNthAssistant), not after a run-store checkpoint past the
        # micro tail: resume starts from a different durable state.
        (True, False),
    ],
)
async def test_paid_then_micro_compaction_snapshot_is_resumable(
    prior_compaction: bool, tail_after_micro: bool
) -> None:
    from linch import CompactionLadder
    from linch.types import Message, TextBlock
    from tests.support.tools import StubTool, tool_registry

    sessions, runs = memory_session_store(), memory_run_store()
    if prior_compaction:
        prior = await _agent(sessions, runs, _SummaryProvider()).session(id="s")
        await prior.append(_seed_messages())
        [event async for event in prior.run("earlier run")]
    provider = _ToolProvider()
    agent = _agent(
        sessions if tail_after_micro else _CrashOnNthAssistant(sessions, 4),
        _CrashAfterMicroTail(runs) if tail_after_micro else runs,
        provider,
        tools=tool_registry(StubTool("Big", output="R" * 600)),
        compaction_ladder=CompactionLadder(keep_recent_turns=1),
    )
    session = await agent.session(id="s")
    big, small = "old " * 750, "x"
    await session.append(
        [
            Message(
                role="user" if index % 2 == 0 else "assistant",
                content=[TextBlock(text=big if index < 3 else small)],
            )
            for index in range(22)
        ]
    )

    run_id = None
    compactions: list[str] = []
    with pytest.raises(_Crash):
        async for event in session.run("go"):
            if event.type == "system":
                run_id = event.run_id
            if event.type == "compaction":
                compactions.append(event.strategy)
    assert run_id is not None
    assert compactions == ["default-keep-recent-10", "micro"]
    snapshot = await sessions.load_provider_snapshot("s")
    assert snapshot is not None
    assert any(
        block.content.startswith("[tool result elided")
        for message in snapshot.provider_view
        for block in message.content
        if getattr(block, "type", None) == "tool_result" and isinstance(block.content, str)
    )

    resumed = await _agent(
        sessions,
        runs,
        _ToolProvider(final=True),
        tools=tool_registry(StubTool("Big", output="R" * 600)),
        compaction_ladder=CompactionLadder(keep_recent_turns=1),
    ).session(id="s")
    events = [event async for event in resumed.resume(run_id)]

    assert not [event for event in events if event.type == "error"]
    assert events[-1].type == "result"
    assert events[-1].subtype == "success"
    assert any(
        block.content.startswith("[tool result elided")
        for message in resumed.provider_view
        for block in message.content
        if getattr(block, "type", None) == "tool_result" and isinstance(block.content, str)
    )


async def test_paid_compaction_resume_preserves_inline_skill_listing_input() -> None:
    from linch.types import message_to_dict

    sessions, runs = memory_session_store(), memory_run_store()
    before_provider = _SummaryProvider(crash_primary=True)
    first = _agent(sessions, runs, before_provider)
    first.skill_listing_text = "- pdf: read PDFs"
    session = await first.session(id="s")
    await session.append(_seed_messages())

    run_id = None
    with pytest.raises(_Crash):
        async for event in session.run("go"):
            if event.type == "system":
                run_id = event.run_id
    assert run_id is not None
    assert before_provider.primary

    after_provider = _SummaryProvider()
    resumed_agent = _agent(sessions, runs, after_provider)
    resumed_agent.skill_listing_text = "- pdf: read PDFs"
    resumed = await resumed_agent.session(id="s")
    events = [event async for event in resumed.resume(run_id)]

    assert not [event for event in events if event.type == "error"]
    assert events[-1].type == "result"
    assert events[-1].subtype == "success"
    assert after_provider.primary
    assert [message_to_dict(message) for message in after_provider.primary[0]] == [
        message_to_dict(message) for message in before_provider.primary[0]
    ]
