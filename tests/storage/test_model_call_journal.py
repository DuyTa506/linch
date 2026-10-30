"""Crash recovery exercises real durable stores, not checkpoint aggregates."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest


class _Crash(BaseException):
    pass


class _FaultStore:
    """Leave the durable state exactly where a process disappeared."""

    def __init__(self, store: Any, boundary: str) -> None:
        self.store = store
        self.boundary = boundary
        self.crashed = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self.store, name)

    def _check(self, boundary: str) -> None:
        if self.crashed or boundary == self.boundary:
            self.crashed = True
            raise _Crash(boundary)

    async def save_checkpoint(self, run_id: str, checkpoint: Any, **kw: Any) -> Any:
        self._check(f"checkpoint:{checkpoint.phase}")
        return await self.store.save_checkpoint(run_id, checkpoint, **kw)

    async def append_event(self, run_id: str, event: Any) -> int:
        kind = getattr(event, "record_kind", event.type)
        if kind == "projection" and event.record.get("projection_kind") == "compaction_source":
            kind = "compaction_source"
        self._check(f"event:{kind}")
        return await self.store.append_event(run_id, event)

    async def append_events(self, run_id: str, events: list[Any]) -> list[int]:
        return [await self.append_event(run_id, event) for event in events]

    async def save(self, run_id: str, attempt: int, request: Any) -> Any:
        self._check("snapshot")
        return await self.store.save(run_id, attempt, request)

    async def save_provider_snapshot(self, session_id: str, snapshot: Any) -> None:
        self._check("provider_snapshot")
        await self.store.save_provider_snapshot(session_id, snapshot)

    async def append_messages(self, session_id: str, messages: list[Any]) -> Any:
        if any(message.role == "assistant" for message in messages):
            self._check("assistant_append")
        elif self.crashed:
            raise _Crash("already crashed")
        return await self.store.append_messages(session_id, messages)


class _Provider:
    id = "durable-accounting"

    def __init__(self, *, compact: bool = False, native: bool = False) -> None:
        self.calls: list[Any] = []
        self.compact = compact
        self.native = native

    def context_window(self, model: str) -> int:
        return 1000 if self.compact else 100_000

    async def stream(self, req: Any) -> AsyncIterator[dict[str, Any]]:
        from linch.types import Usage

        self.calls.append(req)
        yield {"type": "message_start", "model": "claude-haiku-4-5"}
        if self.native:
            yield {"type": "tool_use_start", "id": "bad", "name": "NeverExecute"}
            yield {"type": "tool_use_input_delta", "id": "bad", "json_delta": '{"private":"secret"'}
            yield {"type": "tool_use_end", "id": "bad"}
        else:
            yield {"type": "text_delta", "text": "paid summary" if self.compact else "paid answer"}
        yield {
            "type": "message_end",
            "stop_reason": "tool_use" if self.native else "end_turn",
            "usage": Usage(11, 2),
        }


def _agent(session_store: Any, run_store: Any, provider: Any, **kw: Any) -> Any:
    from linch import Agent
    from linch.config import FeatureFlags
    from linch.durability import DurabilityOptions
    from linch.tools.registry import empty_tools

    return Agent(
        model="requested-unpriced-model",
        provider=provider,
        session_store=session_store,
        run_store=run_store,
        durability=kw.pop("durability", DurabilityOptions(exact_model_input=True)),
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        tools=empty_tools(),
        max_output_tokens=100,
        **kw,
    )


async def _stores(kind: str, tmp_path: Any) -> tuple[Any, Any]:
    from linch.run_store import InMemoryRunStore, SqliteRunStore
    from linch.sessions import InMemorySessionStore, SqliteSessionStore

    if kind == "memory":
        return InMemorySessionStore(), InMemoryRunStore()
    return SqliteSessionStore(tmp_path / "sessions.db"), SqliteRunStore(tmp_path / "runs.db")


async def _reopen(kind: str, tmp_path: Any, sessions: Any, runs: Any) -> tuple[Any, Any]:
    if kind == "sqlite":
        await sessions.close()
        await runs.close()
        return await _stores(kind, tmp_path)
    return sessions, runs


async def _close(*stores: Any) -> None:
    for store in stores:
        closer = getattr(store, "close", None)
        if closer is not None:
            await closer()


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
async def test_primary_append_after_hook_recovers_without_reinference(
    kind: str, tmp_path: Any
) -> None:
    """Losing the append checkpoint must not buy or mutate the accepted answer twice."""
    from linch.hooks import HookResult
    from linch.types import TextBlock, Usage

    class Mutate:
        resume_policy_id = "test.primary-post-hook"
        resume_policy_version = "1"
        resume_policy_config: dict[str, Any] = {}

        def __init__(self) -> None:
            self.calls = 0

        def on_after_provider_call(self, ctx: Any) -> Any:
            self.calls += 1
            ctx.assembly.message.content = [TextBlock(text="after hook")]
            return HookResult.mutate(assembly=ctx.assembly)

    sessions, runs = await _stores(kind, tmp_path)
    hook, provider = Mutate(), _Provider()
    fault = _FaultStore(runs, "checkpoint:assistant_appended")
    agent = _agent(sessions, fault, provider, hooks=[hook])
    session = await agent.session(id="session")
    run_id = ""
    with pytest.raises(_Crash):
        async for event in session.run("go"):
            if event.type == "system":
                run_id = event.run_id
    journal = [row.event for row in await runs.load_events(run_id)]
    projections = [
        event.record for event in journal if getattr(event, "record_kind", None) == "projection"
    ]
    assert len(projections) == 1
    assert projections[0]["projection_kind"] == "primary_append"
    assert projections[0]["assistant_stop_reason"] == "end_turn"
    assert hook.calls == 1
    sessions, runs = await _reopen(kind, tmp_path, sessions, runs)
    try:
        resumed_provider, resumed_hook = _Provider(), Mutate()
        resumed = await _agent(sessions, runs, resumed_provider, hooks=[resumed_hook]).session(
            id="session"
        )
        events = [event async for event in resumed.resume(run_id)]
        assert resumed_provider.calls == []
        assert resumed_hook.calls == 0
        assert events[-1].final_text == "after hook"
        assert events[-1].total_usage == Usage(11, 2)
    finally:
        await _close(sessions, runs)


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
async def test_frozen_replay_rejects_unbound_assistant_suffix(kind: str, tmp_path: Any) -> None:
    """A last-assistant heuristic must never accept somebody else's durable suffix."""
    from linch.types import Message, TextBlock

    sessions, runs = await _stores(kind, tmp_path)
    session = await _agent(sessions, _FaultStore(runs, "event:projection"), _Provider()).session(
        id="session"
    )
    run_id = ""
    with pytest.raises(_Crash):
        async for event in session.run("go"):
            if event.type == "system":
                run_id = event.run_id
    await sessions.append_messages(
        "session", [Message(role="assistant", content=[TextBlock(text="unrelated")])]
    )
    sessions, runs = await _reopen(kind, tmp_path, sessions, runs)
    try:
        provider = _Provider()
        resumed = await _agent(sessions, runs, provider).session(id="session")
        events = [event async for event in resumed.resume(run_id)]
        assert any(event.type == "error" for event in events)
        assert events[-1].subtype == "error"
        assert provider.calls == []
    finally:
        await _close(sessions, runs)


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
async def test_rejected_native_receipt_restores_terminal_error(kind: str, tmp_path: Any) -> None:
    """A reported rejected batch is terminal_error_v0 before its terminal checkpoint."""
    from linch.types import Usage

    sessions, runs = await _stores(kind, tmp_path)
    session = await _agent(
        sessions, _FaultStore(runs, "event:usage"), _Provider(native=True)
    ).session(id="session")
    run_id = ""
    with pytest.raises(_Crash):
        async for event in session.run("go"):
            if event.type == "system":
                run_id = event.run_id
    sessions, runs = await _reopen(kind, tmp_path, sessions, runs)
    try:
        provider = _Provider()
        resumed = await _agent(sessions, runs, provider).session(id="session")
        events = [event async for event in resumed.resume(run_id)]
        assert provider.calls == []
        assert events[-1].subtype == "error"
        assert events[-1].total_usage == Usage(11, 2)
        assert "malformed native tool arguments" in next(
            event.error["message"] for event in events if event.type == "error"
        )
        assert "secret" not in repr(await runs.load_events(run_id))
        assert not any(message.role == "assistant" for message in resumed.full_history)
    finally:
        await _close(sessions, runs)


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
@pytest.mark.parametrize("boundary", ["event:projection", "checkpoint:provider_pending"])
async def test_paid_compaction_recovers_after_cache_loss(
    kind: str, boundary: str, tmp_path: Any
) -> None:
    """Both receipt-only and consumed projections rebuild their paid summary from history."""
    from linch.types import Message, TextBlock, Usage

    sessions, runs = await _stores(kind, tmp_path)
    provider = _Provider(compact=True)
    session = await _agent(sessions, _FaultStore(runs, boundary), provider).session(id="session")
    await session.append(
        [
            Message(
                role="user" if index % 2 == 0 else "assistant",
                content=[TextBlock(text=f"seed {index} " * 120)],
            )
            for index in range(30)
        ]
    )
    run_id = ""
    with pytest.raises(_Crash):
        async for event in session.run("go"):
            if event.type == "system":
                run_id = event.run_id
    assert len(provider.calls) == 1
    if kind == "memory":
        sessions._snapshots.clear()
    else:
        await sessions._exec.run(
            lambda conn: conn.execute("delete from session_snapshots").connection.commit()
        )
    sessions, runs = await _reopen(kind, tmp_path, sessions, runs)
    try:
        provider = _Provider(compact=True)
        resumed = await _agent(sessions, runs, provider).session(id="session")
        events = [event async for event in resumed.resume(run_id)]
        assert len(provider.calls) == 1
        assert not any(
            "Summarize the conversation so far." in block.text for block in provider.calls[0].system
        )
        assert events[-1].total_usage == Usage(22, 4)
        projections = [
            row.event.record
            for row in await runs.load_events(run_id)
            if getattr(row.event, "record_kind", None) == "projection"
            and row.event.record.get("projection_kind") == "compaction"
        ]
        assert len(projections) == 1
        snapshot = await sessions.load_provider_snapshot("session")
        assert snapshot is not None
        assert snapshot.projection_id == projections[0]["projection_id"]
        assert "paid summary" in provider.calls[0].messages[0].content[0].text
    finally:
        await _close(sessions, runs)


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
@pytest.mark.parametrize(
    "boundary", ["event:intent", "event:dispatch_started", "event:projection", "assistant_append"]
)
@pytest.mark.parametrize("reuse_budget", [False, True])
async def test_primary_retry_uses_new_delivery_and_receipt_charges(
    kind: str, boundary: str, reuse_budget: bool, tmp_path: Any
) -> None:
    """A retry keeps its operation, never its physical id or aggregate-only charge."""
    from linch import RunBudget
    from linch.pricing import cost_usd
    from linch.types import Usage

    sessions, runs = await _stores(kind, tmp_path)
    budget = RunBudget(max_tokens=1000)
    run_fault = _FaultStore(runs, boundary)
    session_fault = _FaultStore(sessions, boundary)
    provider = _Provider()
    session = await _agent(session_fault, run_fault, provider, budget=budget).session(id="session")
    run_id = ""
    with pytest.raises(_Crash):
        async for event in session.run("go"):
            if event.type == "system":
                run_id = event.run_id
    before = await runs.load_run(run_id)
    assert before is not None and before.checkpoint is not None
    binding = before.checkpoint.extension_state["linch.model_calls.v1"]["active_primary"]
    old_delivery = binding["delivery_attempt_id"]
    dispatched = boundary in {"event:projection", "assistant_append"}
    assert len(provider.calls) == int(dispatched)
    if not reuse_budget:
        budget = RunBudget(max_tokens=1000)
    sessions, runs = await _reopen(kind, tmp_path, sessions, runs)
    try:
        provider = _Provider()
        resumed = await _agent(sessions, runs, provider, budget=budget).session(id="session")
        events = [event async for event in resumed.resume(run_id)]
        assert len(provider.calls) == 1
        receipts = [
            row.event.record
            for row in await runs.load_events(run_id)
            if getattr(row.event, "record_kind", None) == "receipt"
        ]
        assert len(receipts) == 1 + int(dispatched)
        assert receipts[-1]["logical_call_id"] == binding["logical_call_id"]
        assert receipts[-1]["delivery_attempt_id"] != old_delivery
        assert budget.spent_tokens == 13 * (1 + int(dispatched))
        assert events[-1].total_usage == Usage(
            11 * (1 + int(dispatched)), 2 * (1 + int(dispatched))
        )
        assert events[-1].total_cost_usd == pytest.approx(
            cost_usd(Usage(11, 2), "claude-haiku-4-5") * (1 + int(dispatched))
        )
    finally:
        await _close(sessions, runs)


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
async def test_started_without_receipt_blocks_capped_resume(kind: str, tmp_path: Any) -> None:
    """Observed but undurable spending cannot become a free capped retry."""
    from linch import RunBudget

    sessions, runs = await _stores(kind, tmp_path)
    budget = RunBudget(max_tokens=1000)
    provider = _Provider()
    session = await _agent(
        sessions, _FaultStore(runs, "event:receipt"), provider, budget=budget
    ).session(id="session")
    run_id = ""
    with pytest.raises(_Crash):
        async for event in session.run("go"):
            if event.type == "system":
                run_id = event.run_id
    assert len(provider.calls) == 1
    assert budget.spent_tokens == 13  # observed before the failed receipt append
    sessions, runs = await _reopen(kind, tmp_path, sessions, runs)
    try:
        provider = _Provider()
        resumed = await _agent(sessions, runs, provider, budget=RunBudget(max_tokens=1000)).session(
            id="session"
        )
        events = [event async for event in resumed.resume(run_id)]
        assert provider.calls == []
        assert events[-1].subtype == "error"
        assert events[-1].usage_complete is False
        assert "unknown use" in next(
            event.error["message"] for event in events if event.type == "error"
        )
    finally:
        await _close(sessions, runs)


@pytest.mark.parametrize("prefix", [0, 1, 2])
def test_shared_receipt_prefix_matches_only_its_fixed_order(prefix: int) -> None:
    """Reliability can share ordered identity checking without enabling correction."""
    from linch.loop.checkpoint import _message_digest, _receipt_message_prefix
    from linch.model_calls import _context_fingerprint
    from linch.types import Message, TextBlock

    base = [Message(role="user", content=[TextBlock(text="durable base")])]
    expected = [
        Message(role="assistant", content=[TextBlock(text="safe diagnostic")]),
        Message(role="user", content=[TextBlock(text="fixed feedback")]),
    ]
    assert (
        _receipt_message_prefix(
            [*base, *expected[:prefix]],
            base_count=1,
            base_fingerprint=_context_fingerprint(base),
            expected_digests=[_message_digest(message) for message in expected],
        )
        == prefix
    )
    wrong = [*base, Message(role="assistant", content=[TextBlock(text="wrong receipt")])]
    with pytest.raises(Exception, match="unexpected durable message suffix"):
        _receipt_message_prefix(
            wrong,
            base_count=1,
            base_fingerprint=_context_fingerprint(base),
            expected_digests=[_message_digest(message) for message in expected],
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("projection_kind", "future"),
        ("version", True),
        ("turn_index", False),
        ("assistant_stop_reason", "unknown"),
        ("after_action", "retry"),
        ("extra", "unknown"),
        ("assistant_sha256", "wrong"),
    ],
)
def test_primary_projection_codec_rejects_tampering(field: str, value: Any) -> None:
    """Required variants and digests are strict at public event ingress."""
    from linch._wire import UnsupportedWireVersionError
    from linch.events import ModelCallEvent
    from linch.model_calls import _ModelCallError, _projection_record

    record = _projection_record(
        projection_kind="primary_append",
        run_id="run",
        logical_call_id="operation",
        delivery_attempt_id="delivery",
        turn_index=0,
        snapshot_id="snapshot",
        request_fingerprint="request",
        full_history_base_count=1,
        full_history_base_fingerprint="a" * 64,
        assistant_sha256="b" * 64,
        assistant_stop_reason="end_turn",
        after_action=None,
        after_feedback=None,
        control_message_sha256=None,
    )
    record[field] = value
    error = UnsupportedWireVersionError if field == "version" else _ModelCallError
    with pytest.raises(error):
        ModelCallEvent("run", "projection", record)


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
async def test_accepted_assistant_recovers_even_after_budget_exhaustion(
    kind: str, tmp_path: Any
) -> None:
    """Admission is for another inference; an already paid assistant can finish."""
    from linch import RunBudget

    sessions, runs = await _stores(kind, tmp_path)
    budget = RunBudget(max_tokens=1)
    session = await _agent(
        sessions, _FaultStore(runs, "checkpoint:assistant_appended"), _Provider(), budget=budget
    ).session(id="session")
    run_id = ""
    with pytest.raises(_Crash):
        async for event in session.run("go"):
            if event.type == "system":
                run_id = event.run_id
    sessions, runs = await _reopen(kind, tmp_path, sessions, runs)
    try:
        provider = _Provider()
        resumed = await _agent(sessions, runs, provider, budget=RunBudget(max_tokens=1)).session(
            id="session"
        )
        events = [event async for event in resumed.resume(run_id)]
        assert provider.calls == []
        assert events[-1].subtype == "success"
        assert events[-1].final_text == "paid answer"
    finally:
        await _close(sessions, runs)


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
@pytest.mark.parametrize(
    "control,boundary",
    [
        ("continue", "checkpoint:assistant_appended"),
        ("continue", "checkpoint:turn_complete"),
        ("stop", "event:usage"),
    ],
)
async def test_primary_recovers_post_hook_control_without_rerunning_hook(
    kind: str, control: str, boundary: str, tmp_path: Any
) -> None:
    """Missing/existing control suffixes preserve the paid hook decision exactly once."""
    from linch.hooks import HookResult

    class Control:
        resume_policy_id = "test.durable-control"
        resume_policy_version = "1"
        resume_policy_config = {"action": control}

        def __init__(self) -> None:
            self.calls = 0

        def on_after_provider_call(self, ctx: Any) -> Any:
            self.calls += 1
            if ctx.turn_index > 0:
                return HookResult.continue_()
            return (
                HookResult.force_continue("finish next turn")
                if control == "continue"
                else HookResult.stop("paid stop")
            )

    sessions, runs = await _stores(kind, tmp_path)
    session = await _agent(
        sessions, _FaultStore(runs, boundary), _Provider(), hooks=[Control()]
    ).session(id="session")
    run_id = ""
    with pytest.raises(_Crash):
        async for event in session.run("go"):
            if event.type == "system":
                run_id = event.run_id
    sessions, runs = await _reopen(kind, tmp_path, sessions, runs)
    try:
        hook, provider = Control(), _Provider()
        resumed = await _agent(sessions, runs, provider, hooks=[hook]).session(id="session")
        events = [event async for event in resumed.resume(run_id)]
        if control == "stop":
            assert provider.calls == []
            assert hook.calls == 0
            assert events[-1].subtype == "error"
        else:
            assert len(provider.calls) == hook.calls == 1
            assert events[-1].subtype == "success"
            assert (
                sum(
                    "finish next turn" in getattr(block, "text", "")
                    for message in resumed.full_history
                    for block in message.content
                )
                == 1
            )
    finally:
        await _close(sessions, runs)


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
@pytest.mark.parametrize("consumed", [False, True])
@pytest.mark.parametrize("detailed", [False, True])
async def test_paid_projection_and_consumption_reconcile_independently(
    kind: str, consumed: bool, detailed: bool, tmp_path: Any
) -> None:
    """Either store may lead; consumption never substitutes for a projection identity."""
    from linch.compaction import DefaultCompaction, DetailedCompaction
    from linch.model_calls import _ModelCallLedger
    from linch.types import Message, TextBlock, Usage

    sessions, runs = await _stores(kind, tmp_path)
    strategy = DetailedCompaction(keep_recent_turns=2) if detailed else DefaultCompaction()
    fault = _FaultStore(runs, "event:projection" if consumed else "checkpoint:provider_pending")
    session = await _agent(sessions, fault, _Provider(compact=True), compaction=strategy).session(
        id="session"
    )
    await session.append(
        [
            Message(
                role="user" if index % 2 == 0 else "assistant",
                content=[TextBlock(text=f"seed {index} " * 120)],
            )
            for index in range(30)
        ]
    )
    run_id = ""
    with pytest.raises(_Crash):
        async for event in session.run("go"):
            if event.type == "system":
                run_id = event.run_id
    saved = await runs.load_run(run_id)
    assert saved is not None and saved.checkpoint is not None
    checkpoint = saved.checkpoint
    checkpoint.extension_state["linch.reliability"] = {"version": 1, "unrelated": "preserved"}
    checkpoint.extension_state["host.extension"] = {"owner": "host"}
    if consumed:
        ledger = _ModelCallLedger(run_id)
        await ledger.restore(
            None,
            [row.event for row in await runs.load_events(run_id) if row.event.type == "model_call"],
        )
        ledger.consume_auxiliary_receipts()
        checkpoint.extension_state["linch.model_calls.v1"] = {
            "version": 3,
            "ledger": ledger.snapshot(),
            "active_primary": None,
        }
        checkpoint.total_usage = ledger.total_usage
    await runs.save_checkpoint(run_id, checkpoint)
    sessions, runs = await _reopen(kind, tmp_path, sessions, runs)
    try:
        provider = _Provider(compact=True)
        resumed = await _agent(sessions, runs, provider, compaction=strategy).session(id="session")
        events = [event async for event in resumed.resume(run_id)]
        assert len(provider.calls) == 1
        assert events[-1].total_usage == Usage(22, 4)
        complete = await runs.load_run(run_id)
        assert sum(
            event.type == "usage" and event.source == "compaction" for event in events
        ) == int(not consumed)
        assert complete.checkpoint.extension_state["host.extension"] == {"owner": "host"}
        assert complete.checkpoint.extension_state["linch.reliability"] == {
            "version": 1,
            "unrelated": "preserved",
        }
        snapshot = await sessions.load_provider_snapshot("session")
        assert snapshot.projection_id
        assert provider.calls[0].messages[0].content[0].text == (
            "<detailed summary of earlier conversation>\n\npaid summary"
            if detailed
            else "<summary of earlier conversation>\n\npaid summary"
        )
    finally:
        await _close(sessions, runs)


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
@pytest.mark.parametrize(
    "field", ["snapshot_id", "request_fingerprint", "logical_call_id", "full_history_base_count"]
)
async def test_primary_binding_tampering_fails_before_dispatch(
    kind: str, field: str, tmp_path: Any
) -> None:
    """A different checkpoint binding cannot borrow a settled delivery's answer."""
    sessions, runs = await _stores(kind, tmp_path)
    session = await _agent(
        sessions, _FaultStore(runs, "checkpoint:assistant_appended"), _Provider()
    ).session(id="session")
    run_id = ""
    with pytest.raises(_Crash):
        async for event in session.run("go"):
            if event.type == "system":
                run_id = event.run_id
    saved = await runs.load_run(run_id)
    checkpoint = saved.checkpoint
    binding = checkpoint.extension_state["linch.model_calls.v1"]["active_primary"]
    binding[field] = 0 if field == "full_history_base_count" else "changed"
    await runs.save_checkpoint(run_id, checkpoint)
    sessions, runs = await _reopen(kind, tmp_path, sessions, runs)
    try:
        provider = _Provider()
        resumed = await _agent(sessions, runs, provider).session(id="session")
        events = [event async for event in resumed.resume(run_id)]
        assert events[-1].subtype == "error"
        assert provider.calls == []
    finally:
        await _close(sessions, runs)


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
async def test_custom_subclass_has_accounting_without_paid_transform_replay(
    kind: str, tmp_path: Any
) -> None:
    """A built-in id inherited by an arbitrary transform never declares determinism."""
    from linch.compaction import DefaultCompaction
    from linch.types import Message, TextBlock, Usage

    class Custom(DefaultCompaction):
        async def compact(self, ctx: Any, provider: Any) -> Any:
            messages = await super().compact(ctx, provider)
            return [
                Message(role="user", content=[TextBlock(text="arbitrary transform")]),
                *messages,
            ]

    sessions, runs = await _stores(kind, tmp_path)
    provider = _Provider(compact=True)
    session = await _agent(
        sessions, _FaultStore(runs, "checkpoint:provider_pending"), provider, compaction=Custom()
    ).session(id="session")
    await session.append(
        [
            Message(
                role="user" if index % 2 == 0 else "assistant",
                content=[TextBlock(text=f"seed {index} " * 120)],
            )
            for index in range(30)
        ]
    )
    run_id = ""
    with pytest.raises(_Crash):
        async for event in session.run("go"):
            if event.type == "system":
                run_id = event.run_id
    assert not any(
        getattr(row.event, "record_kind", None) == "projection"
        for row in await runs.load_events(run_id)
    )
    sessions, runs = await _reopen(kind, tmp_path, sessions, runs)
    try:
        provider = _Provider(compact=True)
        resumed = await _agent(sessions, runs, provider, compaction=Custom()).session(id="session")
        events = [event async for event in resumed.resume(run_id)]
        assert len(provider.calls) == 2
        assert events[-1].total_usage == Usage(33, 6)
        receipts = [
            row.event.record
            for row in await runs.load_events(run_id)
            if getattr(row.event, "record_kind", None) == "receipt"
            and row.event.record["kind"] == "compaction"
        ]
        assert receipts[0]["logical_call_id"] != receipts[1]["logical_call_id"]
    finally:
        await _close(sessions, runs)


async def test_conflicting_paid_projection_for_one_receipt_fails_closed() -> None:
    """One paid result cannot silently choose between different durable projections."""
    import hashlib

    from linch.model_calls import (
        ModelCallIntent,
        ModelCallReceipt,
        _ModelCallError,
        _ModelCallLedger,
        _projection_record,
    )
    from linch.types import Usage

    ledger = _ModelCallLedger("run")
    intent = ModelCallIntent(
        "operation", "delivery", None, "compaction", "run", 0, "model", "request", "default"
    )
    receipt = ModelCallReceipt(
        "operation",
        "delivery",
        "compaction",
        "run",
        0,
        "model",
        "reported",
        Usage(1, 2),
        None,
        "accepted",
        None,
        "paid",
        hashlib.sha256(b"paid").hexdigest(),
        0,
        "request",
        "default",
        "a" * 64,
    )
    values = {
        "projection_kind": "compaction",
        "source_receipt_ids": ["delivery"],
        "strategy_id": "default",
        "strategy_config": {
            "implementation": "default_v1",
            "keep_recent_turns": 10,
            "max_output_tokens": 4096,
            "prompt_sha256": "b" * 64,
        },
        "source_context_fingerprint": "a" * 64,
        "target_context_fingerprint": "c" * 64,
        "full_history_base_count": 1,
        "full_history_base_fingerprint": "d" * 64,
        "source_message_fingerprints": ["f" * 64],
        "reminder_fingerprints": [],
    }
    await ledger.begin(intent)
    await ledger.record_projection(
        _projection_record(
            projection_kind="compaction_source",
            run_id="run",
            logical_call_id="operation",
            delivery_attempt_id="delivery",
            turn_index=0,
            request_fingerprint="request",
            **{
                key: value
                for key, value in values.items()
                if key
                not in {"projection_kind", "source_receipt_ids", "target_context_fingerprint"}
            },
        )
    )
    await ledger.mark_dispatch_started(intent)
    await ledger.settle(receipt)
    first = _projection_record(**values)
    no_source = _ModelCallLedger("run")
    await no_source.restore(ledger.snapshot(), [])
    with pytest.raises(_ModelCallError, match="physical source"):
        await no_source.record_projection(first)
    await ledger.record_projection(first)
    values["target_context_fingerprint"] = "e" * 64
    with pytest.raises(_ModelCallError, match="conflicting"):
        await ledger.record_projection(_projection_record(**values))


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
@pytest.mark.parametrize("boundary", ["snapshot", "provider_snapshot"])
async def test_snapshot_boundary_never_loses_a_paid_compaction(
    kind: str, boundary: str, tmp_path: Any
) -> None:
    """Provider-view cache failure and exact-input failure both replay only the main call."""
    from linch.types import Message, TextBlock, Usage

    sessions, runs = await _stores(kind, tmp_path)
    sf, rf = _FaultStore(sessions, boundary), _FaultStore(runs, boundary)
    provider = _Provider(compact=True)
    session = await _agent(sf, rf, provider).session(id="session")
    await session.append(
        [
            Message(
                role="user" if index % 2 == 0 else "assistant",
                content=[TextBlock(text=f"seed {index} " * 120)],
            )
            for index in range(30)
        ]
    )
    run_id = ""
    with pytest.raises(_Crash):
        async for event in session.run("go"):
            if event.type == "system":
                run_id = event.run_id
    assert len(provider.calls) == 1
    sessions, runs = await _reopen(kind, tmp_path, sessions, runs)
    try:
        provider = _Provider(compact=True)
        resumed = await _agent(sessions, runs, provider).session(id="session")
        events = [event async for event in resumed.resume(run_id)]
        assert len(provider.calls) == 1
        assert events[-1].total_usage == Usage(22, 4)
    finally:
        await _close(sessions, runs)


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
async def test_changed_paid_projection_view_fails_without_dispatch(
    kind: str, tmp_path: Any
) -> None:
    """A provider-view cache with a different suffix is not a paid-result replay target."""
    from linch.sessions.store import ProviderViewSnapshot
    from linch.types import Message, TextBlock

    sessions, runs = await _stores(kind, tmp_path)
    session = await _agent(
        sessions, _FaultStore(runs, "checkpoint:provider_pending"), _Provider(compact=True)
    ).session(id="session")
    await session.append(
        [
            Message(
                role="user" if index % 2 == 0 else "assistant",
                content=[TextBlock(text=f"seed {index} " * 120)],
            )
            for index in range(30)
        ]
    )
    run_id = ""
    with pytest.raises(_Crash):
        async for event in session.run("go"):
            if event.type == "system":
                run_id = event.run_id
    rows = await sessions.load_messages("session")
    await sessions.save_provider_snapshot(
        "session",
        ProviderViewSnapshot(
            [Message(role="user", content=[TextBlock(text="changed fork context")])],
            rows[-1].seq,
            projection_id="wrong projection",
        ),
    )
    sessions, runs = await _reopen(kind, tmp_path, sessions, runs)
    try:
        provider = _Provider(compact=True)
        resumed = await _agent(sessions, runs, provider).session(id="session")
        events = [event async for event in resumed.resume(run_id)]
        assert events[-1].subtype == "error"
        assert provider.calls == []
    finally:
        await _close(sessions, runs)


@pytest.mark.parametrize("owner", ["nonzero", "mismatched"])
async def test_durable_primary_rejects_incompatible_budget_owner(owner: str, tmp_path: Any) -> None:
    """A capped host budget cannot silently reinterpret saved receipt spending."""
    from linch import RunBudget
    from linch.errors import ConfigError

    sessions, runs = await _stores("memory", tmp_path)
    session = await _agent(
        sessions,
        _FaultStore(runs, "checkpoint:assistant_appended"),
        _Provider(),
        budget=RunBudget(max_tokens=1000),
    ).session(id="session")
    run_id = ""
    with pytest.raises(_Crash):
        async for event in session.run("go"):
            if event.type == "system":
                run_id = event.run_id
    budget = RunBudget(max_tokens=1000, spent_tokens=1 if owner == "nonzero" else 0)
    if owner == "mismatched":
        budget._receipt_owner()
    provider = _Provider()
    resumed = await _agent(sessions, runs, provider, budget=budget).session(id="session")
    with pytest.raises(ConfigError):
        _ = [event async for event in resumed.resume(run_id)]
    assert provider.calls == []


async def test_contract_rejects_unsupported_accounting_semantics(tmp_path: Any) -> None:
    """An execution contract with old accounting cannot opt into new replay implicitly."""
    from linch.errors import ConfigError
    from linch.run_store import (
        RunContract,
        _run_contract_fingerprint,
        run_contract_from_meta,
        run_meta_with_contract,
    )
    from linch.session import RunOptions

    sessions, runs = await _stores("memory", tmp_path)
    session = await _agent(sessions, _FaultStore(runs, "event:intent"), _Provider()).session(
        id="session"
    )
    run_id = ""
    with pytest.raises(_Crash):
        async for event in session.run("go"):
            if event.type == "system":
                run_id = event.run_id
    record = runs._runs[run_id]
    contract = run_contract_from_meta(record.meta)
    payload = contract.payload
    payload["policies"]["model_call_accounting"]["native_rejection"] = "bounded_feedback_v1"
    record.meta = run_meta_with_contract(
        RunContract(
            contract.schema_version,
            payload,
            _run_contract_fingerprint(contract.schema_version, payload),
        ),
        record.meta,
    )
    provider = _Provider()
    resumed = await _agent(sessions, runs, provider).session(id="session")
    with pytest.raises(ConfigError, match="Cannot safely resume"):
        _ = [event async for event in resumed.resume(run_id, RunOptions(allow_legacy_resume=True))]
    assert provider.calls == []


async def test_recovered_accepted_assistant_defers_new_inbox_delivery(tmp_path: Any) -> None:
    """A new user message cannot be inserted into a recovered frozen turn."""
    from linch.durability import DurabilityOptions, InboxDelivery
    from linch.types import Message, TextBlock

    sessions, runs = await _stores("memory", tmp_path)
    durability = DurabilityOptions(durable_inbox=True, exact_model_input=True)
    session = await _agent(
        sessions,
        _FaultStore(runs, "checkpoint:assistant_appended"),
        _Provider(),
        durability=durability,
    ).session(id="session")
    run_id = ""
    with pytest.raises(_Crash):
        async for event in session.run("go"):
            if event.type == "system":
                run_id = event.run_id
    await sessions.enqueue_inbox(
        "session",
        InboxDelivery(
            "late", Message(role="user", content=[TextBlock(text="next turn")]), source="host"
        ),
    )
    resumed = await _agent(sessions, runs, _Provider(), durability=durability).session(id="session")
    events = [event async for event in resumed.resume(run_id)]
    assert not any(event.type == "user" and event.subtype == "notification" for event in events)
    assert len(resumed.full_history) == 2


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
@pytest.mark.parametrize("cache_present", [False, True])
@pytest.mark.parametrize("consumed_before_projection", [False, True])
@pytest.mark.parametrize("detailed", [False, True])
async def test_repeated_paid_projection_preserves_later_skill_reminder_order(
    kind: str,
    cache_present: bool,
    consumed_before_projection: bool,
    detailed: bool,
    tmp_path: Any,
) -> None:
    """A later skill reminder stays between its durable neighbors across two paid views."""
    from linch import RunBudget
    from linch.abort import AbortContext
    from linch.compaction import DefaultCompaction, DetailedCompaction, _run_compaction_impl
    from linch.loop.request import _re_inject_skill_context
    from linch.loop.runner import _build_run_contract
    from linch.model_calls import _AccountedAuxiliaryCaller, _ModelCallLedger
    from linch.run_store import RunCheckpoint, run_meta_with_contract
    from linch.session import RunOptions
    from linch.sessions.store import ProviderViewSnapshot
    from linch.types import InvokedSkillRecord, Message, TextBlock, Usage

    sessions, runs = await _stores(kind, tmp_path)
    strategy = DetailedCompaction(keep_recent_turns=3) if detailed else DefaultCompaction()
    budget = RunBudget(max_tokens=1000)
    agent = _agent(sessions, runs, _Provider(compact=True), compaction=strategy, budget=budget)
    agent.skill_listing_text = "stable skill listing"
    session = await agent.session(id="session")
    await session.append(
        [
            Message(
                role="user" if index % 2 == 0 else "assistant",
                content=[TextBlock(text=f"seed {index} " * 120)],
            )
            for index in range(30)
        ]
    )
    await runs.create_run(
        "session",
        id="run",
        meta=run_meta_with_contract(_build_run_contract(session, "go", RunOptions())),
    )
    reject_projection = False

    async def persist(event: Any) -> None:
        if (
            reject_projection
            and event.record_kind == "projection"
            and event.record.get("projection_kind") == "compaction"
        ):
            raise _Crash("second paid projection")
        await runs.append_event("run", event)

    ledger = _ModelCallLedger("run", budget=budget, persist=persist)
    session._model_call_ledger = ledger
    caller = _AccountedAuxiliaryCaller(agent.provider, ledger, turn_index=0)
    signal = AbortContext()
    await _run_compaction_impl(
        session, agent, signal, strategy, auxiliary_caller=caller, logical_call_id="first"
    )
    _re_inject_skill_context(session)
    first_snapshot = ledger.snapshot()
    await session.append([Message(role="user", content=[TextBlock(text="before later skill")])])
    session.invoked_skills.append(
        InvokedSkillRecord(name="later", substituted_body="later skill body", invoked_at=1.0)
    )
    await sessions.set_invoked_skills(
        "session", [{"name": "later", "substituted_body": "later skill body", "invoked_at": 1.0}]
    )
    _re_inject_skill_context(session)
    await session.append(
        [
            Message(role="assistant", content=[TextBlock(text="after later skill")]),
            Message(role="user", content=[TextBlock(text="latest durable user")]),
        ]
    )
    reject_projection = consumed_before_projection
    if consumed_before_projection:
        with pytest.raises(_Crash):
            await _run_compaction_impl(
                session, agent, signal, strategy, auxiliary_caller=caller, logical_call_id="second"
            )
    else:
        await _run_compaction_impl(
            session, agent, signal, strategy, auxiliary_caller=caller, logical_call_id="second"
        )
        _re_inject_skill_context(session)
    assert len(agent.provider.calls) == 2
    checkpoint = RunCheckpoint(
        phase="user_appended",
        prompt="go",
        turn_index=0,
        total_usage=Usage(11, 2),
        extension_state={
            "linch.model_calls.v1": {
                "version": 3,
                "ledger": first_snapshot,
                "active_primary": None,
            },
        },
    )
    if consumed_before_projection:
        consumed = _ModelCallLedger("run")
        await consumed.restore(None, [row.event for row in await runs.load_events("run")])
        consumed.consume_auxiliary_receipts()
        checkpoint.extension_state["linch.model_calls.v1"]["ledger"] = consumed.snapshot()
        checkpoint.total_usage = consumed.total_usage
    await runs.save_checkpoint("run", checkpoint)
    if cache_present:
        await sessions.save_provider_snapshot(
            "session",
            ProviderViewSnapshot(
                provider_view=list(session.provider_view),
                covers_seq=session._last_seq,
                projection_id=(session.last_compaction_info or {}).get("projection_id"),
            ),
        )
    sessions, runs = await _reopen(kind, tmp_path, sessions, runs)
    try:
        provider = _Provider(compact=True)
        resumed_budget = RunBudget(max_tokens=1000)
        resumed_agent = _agent(sessions, runs, provider, compaction=strategy, budget=resumed_budget)
        resumed_agent.skill_listing_text = "stable skill listing"
        resumed = await resumed_agent.session(id="session")
        events = [event async for event in resumed.resume("run")]
        assert events[-1].subtype == "success"
        assert len(provider.calls) == 1
        assert not any(
            "Summarize the conversation so far." in block.text for block in provider.calls[0].system
        )
        assert events[-1].total_usage == Usage(33, 6)
        assert resumed_budget.spent_tokens == 39
        texts = [
            block.text
            for message in provider.calls[0].messages
            for block in message.content
            if isinstance(block, TextBlock)
        ]
        reminder = next(text for text in texts if "later skill body" in text)
        assert (
            texts.index("before later skill")
            < texts.index(reminder)
            < texts.index("after later skill")
        )
        assert texts.count(reminder) == 1
        assert (
            len(
                [
                    row
                    for row in await runs.load_events("run")
                    if getattr(row.event, "record_kind", None) == "receipt"
                ]
            )
            == 3
        )
    finally:
        await _close(sessions, runs)


async def test_compaction_source_recipe_is_bound_to_the_pending_delivery() -> None:
    """Ordered reminder metadata cannot become authority for another physical request."""
    from linch.events import ModelCallEvent, event_from_dict, event_to_dict
    from linch.model_calls import (
        ModelCallIntent,
        _ModelCallError,
        _ModelCallLedger,
        _projection_record,
    )

    intent = ModelCallIntent(
        "operation", "delivery", None, "compaction", "run", 0, "model", "request", "default"
    )
    values = {
        "projection_kind": "compaction_source",
        "run_id": "run",
        "logical_call_id": "operation",
        "delivery_attempt_id": "delivery",
        "turn_index": 0,
        "request_fingerprint": "request",
        "strategy_id": "default",
        "strategy_config": {
            "implementation": "default_v1",
            "keep_recent_turns": 10,
            "max_output_tokens": 4096,
            "prompt_sha256": "a" * 64,
        },
        "full_history_base_count": 2,
        "full_history_base_fingerprint": "b" * 64,
        "source_context_fingerprint": "c" * 64,
        "source_message_fingerprints": ["d" * 64, "e" * 64, "f" * 64],
        "reminder_fingerprints": ["e" * 64],
    }
    source = _projection_record(**values)
    event = ModelCallEvent("run", "projection", source)
    assert event_to_dict(event_from_dict(event_to_dict(event))) == event_to_dict(event)
    journal = []

    async def persist(event: Any) -> None:
        journal.append(event)

    ledger = _ModelCallLedger("run", persist=persist)
    with pytest.raises(_ModelCallError):
        await ledger.record_projection(source)
    await ledger.begin(intent)
    await ledger.record_projection(source)
    values["request_fingerprint"] = "different physical request"
    with pytest.raises(_ModelCallError):
        await ledger.record_projection(_projection_record(**values))
    values["request_fingerprint"] = "request"
    restored = _ModelCallLedger("run")
    await restored.restore(ledger.snapshot(), journal)
    assert restored.projections("compaction_source") == [source]
    assert restored.total_usage.input_tokens == restored.total_usage.output_tokens == 0


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
@pytest.mark.parametrize("boundary", ["skill_records", "source_recipe"])
async def test_compaction_reminder_durability_failure_prevents_dispatch(
    kind: str,
    boundary: str,
    tmp_path: Any,
) -> None:
    """A recipe never outlives missing reminder bodies or authorizes an unmarked dispatch."""
    from linch.abort import AbortContext
    from linch.compaction import DefaultCompaction, _run_compaction_impl
    from linch.model_calls import _AccountedAuxiliaryCaller, _ModelCallError, _ModelCallLedger
    from linch.types import InvokedSkillRecord, Message, TextBlock

    sessions, runs = await _stores(kind, tmp_path)

    class SkillWriteFailure:
        def __getattr__(self, name: str) -> Any:
            return getattr(sessions, name)

        async def set_invoked_skills(self, session_id: str, skills: Any) -> None:
            raise OSError("skill write failed")

    provider = _Provider(compact=True)
    agent = _agent(SkillWriteFailure() if boundary == "skill_records" else sessions, runs, provider)
    session = await agent.session(id="session")
    session.invoked_skills.append(InvokedSkillRecord("later", "body", 1.0))
    await session.append(
        [
            Message(
                role="user" if index % 2 == 0 else "assistant", content=[TextBlock(text=str(index))]
            )
            for index in range(30)
        ]
    )
    await runs.create_run("session", id="run")

    async def persist(event: Any) -> None:
        if (
            boundary == "source_recipe"
            and event.record_kind == "projection"
            and event.record.get("projection_kind") == "compaction_source"
        ):
            raise OSError("recipe write failed")
        await runs.append_event("run", event)

    ledger = _ModelCallLedger("run", persist=persist)
    caller = _AccountedAuxiliaryCaller(provider, ledger)
    try:
        with pytest.raises(_ModelCallError, match="durability failed"):
            await _run_compaction_impl(
                session,
                agent,
                AbortContext(),
                DefaultCompaction(),
                auxiliary_caller=caller,
                logical_call_id="operation",
            )
        assert provider.calls == []
        journal = [row.event for row in await runs.load_events("run")]
        assert not any(event.record_kind in {"dispatch_started", "receipt"} for event in journal)
        assert not any(event.record_kind == "projection" for event in journal)
        assert ledger.total_usage.input_tokens == ledger.total_usage.output_tokens == 0
        with pytest.raises(_ModelCallError, match="durability failed"):
            ledger.assert_may_start()
    finally:
        await _close(sessions, runs)


async def test_changed_skill_listing_contract_blocks_resume(tmp_path: Any) -> None:
    """Regenerated listing reminders must have the same durable execution identity."""
    from linch.errors import ConfigError

    sessions, runs = await _stores("memory", tmp_path)
    agent = _agent(sessions, _FaultStore(runs, "event:intent"), _Provider())
    agent.skill_listing_text = "original skill listing"
    session = await agent.session(id="session")
    run_id = ""
    with pytest.raises(_Crash):
        async for event in session.run("go"):
            if event.type == "system":
                run_id = event.run_id
    provider = _Provider()
    changed = _agent(sessions, runs, provider)
    changed.skill_listing_text = "changed skill listing"
    resumed = await changed.session(id="session")
    with pytest.raises(ConfigError, match="Cannot safely resume"):
        _ = [event async for event in resumed.resume(run_id)]
    assert provider.calls == []
