"""Workflow engine integration tests.

linch imports happen inside test functions / provider methods (not at module
level) because tests/loop/test_hardening.py pops all ``linch*`` modules from
``sys.modules``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest


class CountingTextProvider:
    """Returns 'result-N' per call; optionally raises on a given call number."""

    id = "fake"

    def __init__(self, fail_on_call: int | None = None, tokens_per_turn: int = 100) -> None:
        self.calls = 0
        self.fail_on_call = fail_on_call
        self.tokens_per_turn = tokens_per_turn

    def context_window(self, model: str) -> int:
        return 10_000_000

    async def stream(self, req: Any) -> AsyncIterator[dict[str, object]]:
        from linch.types import Usage

        self.calls += 1
        if self.fail_on_call is not None and self.calls == self.fail_on_call:
            raise RuntimeError("provider blew up")
        yield {"type": "message_start", "model": req.model}
        yield {"type": "text_delta", "text": f"result-{self.calls}"}
        yield {
            "type": "message_end",
            "stop_reason": "end_turn",
            "usage": Usage(input_tokens=self.tokens_per_turn),
        }


async def _run_workflow(agent: Any, fn: Any, **kwargs: Any) -> Any:
    from linch.ext.workflow import run_workflow

    return await run_workflow(agent, fn, **kwargs)


async def test_run_workflow_uses_supplied_lease_for_every_durable_write() -> None:
    from linch import InMemoryRunStore
    from linch._wire import WIRE_VERSION
    from linch.run_store import RunLease

    class TrackingStore(InMemoryRunStore):
        def __init__(self) -> None:
            super().__init__()
            self.claims = 0
            self.seen: list[tuple[str, str, object | None]] = []

        async def claim(self, run_id: str, owner: str, ttl_s: float) -> RunLease:
            self.claims += 1
            return await super().claim(run_id, owner, ttl_s)

        async def append_event(self, run_id: str, event: Any, *, lease: Any = None) -> int:
            self.seen.append(("append_event", run_id, lease))
            return await super().append_event(run_id, event, lease=lease)

        async def save_checkpoint(
            self, run_id: str, checkpoint: Any, *, status: str = "running", lease: Any = None
        ) -> Any:
            self.seen.append(("save_checkpoint", run_id, lease))
            return await super().save_checkpoint(run_id, checkpoint, status=status, lease=lease)

        async def save_checkpoint_with_events(
            self,
            run_id: str,
            checkpoint: Any,
            events: list[Any],
            *,
            status: str = "running",
            lease: Any = None,
        ) -> Any:
            self.seen.append(("save_checkpoint_with_events", run_id, lease))
            return await super().save_checkpoint_with_events(
                run_id, checkpoint, events, status=status, lease=lease
            )

        async def mark_completed(self, run_id: str, checkpoint: Any, *, lease: Any = None) -> Any:
            self.seen.append(("mark_completed", run_id, lease))
            return await super().mark_completed(run_id, checkpoint, lease=lease)

        async def release(self, lease: RunLease) -> None:
            self.seen.append(("release", lease.run_id, lease))
            await super().release(lease)

    store = TrackingStore()
    await store.create_run(
        "workflow-session", id="wf-leased", meta={"schema_version": WIRE_VERSION}
    )
    lease = await store.claim("wf-leased", "workflow-owner", 30.0)
    agent = _make_agent(CountingTextProvider(), run_store=store)

    async def flow(wf: Any) -> str:
        await wf.phase("leased")
        return await wf.step("answer", lambda: wf.agent("child"))

    assert (
        await _run_workflow(
            agent,
            flow,
            run_id="wf-leased",
            lease=lease,
            journal_snapshot_every=1,
        )
        == "result-1"
    )
    assert store.claims == 1
    parent_writes = [
        entry for entry in store.seen if entry[1] == "wf-leased" and entry[0] != "release"
    ]
    assert parent_writes
    assert all(seen_lease == lease for _, _, seen_lease in parent_writes)
    child_terminal_writes = [
        entry
        for entry in store.seen
        if entry[1] != "wf-leased" and entry[0] in {"mark_completed", "save_checkpoint_with_events"}
    ]
    assert child_terminal_writes
    assert all(seen_lease is None for _, _, seen_lease in child_terminal_writes)


async def test_auto_claim_preflights_leased_write_capabilities_before_setup() -> None:
    from linch import InMemoryRunStore

    class LegacyAppendStore(InMemoryRunStore):
        def __init__(self) -> None:
            super().__init__()
            self.create_calls = 0
            self.append_body_calls = 0

        async def create_run(self, session_id: str, **kwargs: Any) -> Any:
            self.create_calls += 1
            return await super().create_run(session_id, **kwargs)

        async def append_event(self, run_id: str, event: Any) -> int:
            self.append_body_calls += 1
            return await super().append_event(run_id, event)

    store = LegacyAppendStore()
    agent = _make_agent(CountingTextProvider(), run_store=store)
    body_calls = 0

    async def flow(wf: Any) -> str:
        nonlocal body_calls
        body_calls += 1
        await wf.phase("body")
        return "done"

    with pytest.raises(TypeError, match="append_event"):
        await _run_workflow(agent, flow, run_id="wf-capability")

    assert body_calls == 0
    assert store.create_calls == 0
    assert store.append_body_calls == 0
    assert agent._sessions == {}


async def test_auto_claim_rejects_opaque_leased_store_before_setup() -> None:
    from linch import InMemoryRunStore

    class OpaqueAppend:
        @property
        def __signature__(self) -> Any:
            raise ValueError("opaque callable")

        async def __call__(self, run_id: str, event: Any) -> int:
            return await self._append(run_id, event)

        def bind(self, callback: Any) -> None:
            self._append = callback

    class OpaqueStore(InMemoryRunStore):
        def __init__(self) -> None:
            super().__init__()
            self.create_calls = 0
            self.append_events = None
            self.append_event = OpaqueAppend()
            self.append_event.bind(self._append_event)

        async def create_run(self, session_id: str, **kwargs: Any) -> Any:
            self.create_calls += 1
            return await super().create_run(session_id, **kwargs)

        async def _append_event(self, run_id: str, event: Any) -> int:
            return await InMemoryRunStore.append_event(self, run_id, event)

    store = OpaqueStore()
    agent = _make_agent(CountingTextProvider(), run_store=store)
    body_calls = 0

    async def flow(wf: Any) -> str:
        nonlocal body_calls
        body_calls += 1
        await wf.phase("body")
        return "done"

    with pytest.raises(TypeError, match="does not support lease"):
        await _run_workflow(agent, flow, run_id="wf-opaque")

    assert body_calls == 0
    assert store.create_calls == 0
    assert agent._sessions == {}


async def test_unleased_legacy_workflow_store_keeps_old_call_shape() -> None:
    from linch import InMemoryRunStore

    class LegacyStore:
        claim = renew = release = None

        def __init__(self) -> None:
            self.inner = InMemoryRunStore()

        def __getattr__(self, name: str) -> Any:
            return getattr(self.inner, name)

        async def create_run(
            self,
            session_id: str,
            *,
            id: str | None = None,
            meta: dict[str, object] | None = None,
            initial_checkpoint: Any = None,
        ) -> Any:
            return await self.inner.create_run(
                session_id,
                id=id,
                meta=meta,
                initial_checkpoint=initial_checkpoint,
            )

        async def append_event(self, run_id: str, event: Any) -> int:
            return await self.inner.append_event(run_id, event)

    store = LegacyStore()
    agent = _make_agent(CountingTextProvider(), run_store=store)

    async def flow(wf: Any) -> str:
        await wf.phase("legacy")
        return "done"

    assert await _run_workflow(agent, flow, run_id="wf-legacy") == "done"


class SchemaNameProvider:
    """Returns JSON derived from the requested output schema name."""

    id = "fake"

    def __init__(self) -> None:
        self.calls = 0
        self.schema_names: list[str | None] = []

    def context_window(self, model: str) -> int:
        return 10_000_000

    async def stream(self, req: Any) -> AsyncIterator[dict[str, object]]:
        from linch.types import Usage

        self.calls += 1
        schema_name = req.output_schema.name if req.output_schema is not None else None
        self.schema_names.append(schema_name)
        yield {"type": "message_start", "model": req.model}
        yield {"type": "text_delta", "text": json.dumps({"schema": schema_name})}
        yield {
            "type": "message_end",
            "stop_reason": "end_turn",
            "usage": Usage(input_tokens=10),
        }


def _make_agent(provider: Any, **kwargs: Any) -> Any:
    from tests.support.agents import make_agent

    kwargs.setdefault("run_store", None)
    return make_agent(provider, model="gpt-5", cwd=".", **kwargs)


async def test_wf_agent_runs_subagent_and_returns_final_text() -> None:
    provider = CountingTextProvider()
    agent = _make_agent(provider)
    seen: list[Any] = []

    async def flow(wf: Any) -> str:
        return await wf.agent("summarize the repo", label="summarizer")

    result = await _run_workflow(agent, flow, on_event=seen.append)

    assert result == "result-1"
    kinds = [e.kind for e in seen if e.type == "workflow"]
    assert kinds == ["agent_start", "agent_end"]
    assert any(e.type == "subagent_event" for e in seen)
    end = [e for e in seen if e.type == "workflow" and e.kind == "agent_end"][0]
    assert end.result_text == "result-1"
    assert end.title == "summarizer"


async def test_wf_agent_structured_output_replays_from_journal() -> None:
    from linch import InMemoryRunStore, OutputSchema

    provider = SchemaNameProvider()
    agent = _make_agent(provider, run_store=InMemoryRunStore())
    seen: list[Any] = []
    schema = OutputSchema(
        name="alpha",
        schema={
            "type": "object",
            "properties": {"schema": {"type": "string"}},
            "required": ["schema"],
        },
    )

    async def flow(wf: Any) -> str:
        return await wf.agent("extract", output_schema=schema)

    first = await _run_workflow(agent, flow, run_id="wf-structured", on_event=seen.append)
    second = await _run_workflow(agent, flow, run_id="wf-structured", on_event=seen.append)

    assert first == second == '{"schema":"alpha"}'
    assert provider.calls == 1
    first_end = [e for e in seen if e.type == "workflow" and e.kind == "agent_end"][0]
    assert first_end.structured_output == {"schema": "alpha"}
    replay = [e for e in seen if e.type == "workflow" and e.kind == "agent_replayed"][0]
    assert replay.result_text == '{"schema":"alpha"}'
    assert replay.structured_output == {"schema": "alpha"}


async def test_wf_agent_run_options_are_part_of_replay_key() -> None:
    from linch import InMemoryRunStore, OutputSchema

    provider = SchemaNameProvider()
    agent = _make_agent(provider, run_store=InMemoryRunStore())
    schema_a = OutputSchema(name="alpha", schema={"type": "object"})
    schema_b = OutputSchema(name="beta", schema={"type": "object"})

    async def flow_a(wf: Any) -> str:
        return await wf.agent("same prompt", output_schema=schema_a)

    async def flow_b(wf: Any) -> str:
        return await wf.agent("same prompt", output_schema=schema_b)

    first = await _run_workflow(agent, flow_a, run_id="wf-key")
    second = await _run_workflow(agent, flow_b, run_id="wf-key")

    assert json.loads(first) == {"schema": "alpha"}
    assert json.loads(second) == {"schema": "beta"}
    assert provider.calls == 2


async def test_wf_agent_tool_filter_is_part_of_replay_key() -> None:
    from linch import InMemoryRunStore

    provider = CountingTextProvider()
    agent = _make_agent(provider, run_store=InMemoryRunStore())

    async def flow_with_defaults(wf: Any) -> str:
        return await wf.agent("same prompt")

    async def flow_with_no_tools(wf: Any) -> str:
        return await wf.agent("same prompt", tools=[])

    first = await _run_workflow(agent, flow_with_defaults, run_id="wf-tool-key")
    second = await _run_workflow(agent, flow_with_no_tools, run_id="wf-tool-key")
    replay = await _run_workflow(agent, flow_with_no_tools, run_id="wf-tool-key")

    assert first == "result-1"
    assert second == replay == "result-2"
    assert provider.calls == 2


async def test_wf_agent_explicit_key_replays_unencodable_dependencies() -> None:
    from linch import InMemoryRunStore, RunOptions

    store = InMemoryRunStore()
    provider1 = CountingTextProvider()
    agent1 = _make_agent(provider1, run_store=store)

    async def flow(wf: Any) -> str:
        return await wf.agent(
            "charge",
            key="tenant-acme-v1",
            run_options=RunOptions(deps=object()),
        )

    assert await _run_workflow(agent1, flow, run_id="wf-explicit-key") == "result-1"
    assert provider1.calls == 1

    provider2 = CountingTextProvider()
    agent2 = _make_agent(provider2, run_store=store)
    seen: list[Any] = []
    assert (
        await _run_workflow(agent2, flow, run_id="wf-explicit-key", on_event=seen.append)
        == "result-1"
    )
    assert provider2.calls == 0
    assert [event.kind for event in seen if event.type == "workflow"] == ["agent_replayed"]

    async def flow_with_changed_tools(wf: Any) -> str:
        return await wf.agent(
            "charge",
            key="tenant-acme-v1",
            tools=[],
            run_options=RunOptions(deps=object()),
        )

    assert (
        await _run_workflow(agent2, flow_with_changed_tools, run_id="wf-explicit-key") == "result-1"
    )
    assert provider2.calls == 1


async def test_wf_agent_rejects_unencodable_dependencies_without_key() -> None:
    from linch import InMemoryRunStore, RunOptions
    from linch.errors import ConfigError

    agent = _make_agent(CountingTextProvider(), run_store=InMemoryRunStore())

    async def flow(wf: Any) -> str:
        return await wf.agent("charge", run_options=RunOptions(deps=object()))

    with pytest.raises(ConfigError, match="not JSON-serializable"):
        await _run_workflow(agent, flow, run_id="wf-no-explicit-key")


async def test_step_replays_when_rebuilt_set_key_has_different_insertion_order() -> None:
    from linch import InMemoryRunStore

    store = InMemoryRunStore()
    agent = _make_agent(CountingTextProvider(), run_store=store)
    calls: list[str] = []

    def flow_for(order: tuple[int, ...]) -> Any:
        async def flow(wf: Any) -> str:
            return await wf.step("publish", lambda: calls.append("ran") or "ok", key=set(order))

        return flow

    assert await _run_workflow(agent, flow_for((0, 8, 16)), run_id="wf-set-replay") == "ok"
    assert await _run_workflow(agent, flow_for((16, 0, 8)), run_id="wf-set-replay") == "ok"
    assert calls == ["ran"]


async def test_run_workflow_setup_failure_releases_host_session() -> None:
    from linch import InMemoryRunStore

    class FailingLoadStore(InMemoryRunStore):
        fail_load_events = False

        async def load_events(self, run_id: str, *, after_seq: int = 0) -> Any:
            if self.fail_load_events:
                raise ConnectionResetError("journal load failed")
            return await super().load_events(run_id, after_seq=after_seq)

    store = FailingLoadStore()
    agent = _make_agent(CountingTextProvider(), run_store=store)

    async def flow(wf: Any) -> str:
        return "done"

    assert await _run_workflow(agent, flow, run_id="wf-setup-load") == "done"
    store.fail_load_events = True
    with pytest.raises(ConnectionResetError, match="journal load failed"):
        await _run_workflow(agent, flow, run_id="wf-setup-load")
    assert agent._sessions == {}


async def test_run_workflow_create_failure_releases_host_session() -> None:
    from linch import InMemoryRunStore

    class FailingCreateStore(InMemoryRunStore):
        async def create_run(self, *args: Any, **kwargs: Any) -> Any:
            raise ConnectionResetError("run creation failed")

    agent = _make_agent(CountingTextProvider(), run_store=FailingCreateStore())

    async def flow(wf: Any) -> str:
        return "done"

    with pytest.raises(ConnectionResetError, match="run creation failed"):
        await _run_workflow(agent, flow, run_id="wf-setup-create")
    assert agent._sessions == {}


async def test_partial_workflow_lease_capability_fails_closed() -> None:
    from linch import InMemoryRunStore
    from linch.errors import ConfigError

    class ClaimOnlyStore(InMemoryRunStore):
        renew = None
        release = None

    agent = _make_agent(CountingTextProvider(), run_store=ClaimOnlyStore())

    async def flow(wf: Any) -> str:
        return "done"

    with pytest.raises(ConfigError, match="claim, renew, release"):
        await _run_workflow(agent, flow, run_id="wf-partial-lease")
    assert agent._sessions == {}


async def test_cancelled_claim_releases_lease_acquired_before_return() -> None:
    from linch import InMemoryRunStore

    class GatedClaimStore(InMemoryRunStore):
        def __init__(self) -> None:
            super().__init__()
            self.claimed = asyncio.Event()
            self.allow_claim_return = asyncio.Event()

        async def claim(self, *args: Any, **kwargs: Any) -> Any:
            lease = await super().claim(*args, **kwargs)
            self.claimed.set()
            await self.allow_claim_return.wait()
            return lease

    store = GatedClaimStore()
    agent = _make_agent(CountingTextProvider(), run_store=store)

    async def flow(wf: Any) -> str:
        return "done"

    task = asyncio.create_task(_run_workflow(agent, flow, run_id="wf-claim-cancel"))
    try:
        await asyncio.wait_for(store.claimed.wait(), timeout=1)
        task.cancel()
        store.allow_claim_return.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        owner, _expires_at, _epoch = store._lease_state["wf-claim-cancel"]
        assert owner is None
        assert agent._sessions == {}
    finally:
        store.allow_claim_return.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await store.close()


async def test_mark_completed_failure_marks_workflow_failed() -> None:
    from linch import InMemoryRunStore
    from linch.events import ErrorEvent

    class FailingCompletionStore(InMemoryRunStore):
        async def mark_completed(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("terminal failed")

    store = FailingCompletionStore()
    agent = _make_agent(CountingTextProvider(), run_store=store)

    async def flow(wf: Any) -> str:
        return "done"

    with pytest.raises(RuntimeError, match="terminal failed"):
        await _run_workflow(agent, flow, run_id="wf-terminal-fail")

    record = await store.load_run("wf-terminal-fail")
    assert record is not None
    assert record.status == "failed"
    assert record.meta["errors"] == [
        {"name": "RuntimeError", "message": "terminal failed", "retryable": False}
    ]
    events = await store.load_events("wf-terminal-fail")
    assert [event.event for event in events] == [
        ErrorEvent(error={"name": "RuntimeError", "message": "terminal failed", "retryable": False})
    ]


@pytest.mark.parametrize("kind", ["memory", "postgres"])
async def test_concurrent_workflow_resumes_claim_one_lease(kind: str, pg_dsn: str) -> None:
    from linch import InMemoryRunStore
    from linch.errors import WorkflowSuspended
    from linch.run_store import RunLeaseConflictError

    if kind == "memory":
        store: Any = InMemoryRunStore()
    else:
        from linch import PostgresRunStore

        store = PostgresRunStore(pg_dsn)
    applied: list[str] = []

    async def apply() -> str:
        await asyncio.sleep(0.05)
        applied.append("applied")
        return "ok"

    async def flow(wf: Any) -> str:
        if await wf.interrupt("approve", {"plan": "drop column"}):
            return await wf.step("apply", apply)
        return "cancelled"

    try:
        first = _make_agent(CountingTextProvider(), run_store=store)
        with pytest.raises(WorkflowSuspended):
            await _run_workflow(first, flow, run_id="wf-concurrent-resume")

        agents = [
            _make_agent(CountingTextProvider(), run_store=store),
            _make_agent(CountingTextProvider(), run_store=store),
        ]
        results = await asyncio.gather(
            *(
                _run_workflow(agent, flow, run_id="wf-concurrent-resume", resume={"approve": True})
                for agent in agents
            ),
            return_exceptions=True,
        )

        assert applied == ["applied"]
        assert sum(isinstance(result, RunLeaseConflictError) for result in results) == 1
        assert sum(result == "ok" for result in results) == 1
    finally:
        await store.close()


@pytest.mark.parametrize("kind", ["memory", "postgres"])
async def test_workflow_step_end_persists_before_callback_failure(kind: str, pg_dsn: str) -> None:
    from linch import InMemoryRunStore

    if kind == "memory":
        store: Any = InMemoryRunStore()
    else:
        from linch import PostgresRunStore

        store = PostgresRunStore(pg_dsn)
    agent = _make_agent(CountingTextProvider(), run_store=store)
    side_effects: list[str] = []

    async def flow(wf: Any) -> str:
        async def charge() -> str:
            side_effects.append("charged")
            return "ok"

        return await wf.step("charge", charge)

    def callback(event: Any) -> None:
        if event.type == "workflow" and event.kind == "step_end":
            raise ConnectionResetError("websocket closed")

    try:
        with pytest.raises(ConnectionResetError, match="websocket closed"):
            await _run_workflow(agent, flow, run_id="wf-step-callback", on_event=callback)
        stored_kinds = [event.event.kind for event in await store.load_events("wf-step-callback")]
        assert stored_kinds == ["step_start", "step_end"]

        assert await _run_workflow(agent, flow, run_id="wf-step-callback") == "ok"
        assert side_effects == ["charged"]
    finally:
        await store.close()


@pytest.mark.parametrize("kind", ["memory", "postgres"])
async def test_cancelled_step_append_is_drained_before_resume(kind: str, pg_dsn: str) -> None:
    from linch import InMemoryRunStore

    if kind == "memory":
        inner_store: Any = InMemoryRunStore()
    else:
        from linch import PostgresRunStore

        inner_store = PostgresRunStore(pg_dsn)

    class DelayedEndStore:
        def __init__(self, inner: Any) -> None:
            self.inner = inner
            self.append_started = asyncio.Event()
            self.allow_append = asyncio.Event()

        def __getattr__(self, name: str) -> Any:
            return getattr(self.inner, name)

        async def append_event(self, run_id: str, event: Any, *, lease: Any = None) -> int:
            if getattr(event, "kind", None) == "step_end":
                self.append_started.set()
                await self.allow_append.wait()
            return await self.inner.append_event(run_id, event, lease=lease)

    store = DelayedEndStore(inner_store)
    agent = _make_agent(CountingTextProvider(), run_store=store)
    side_effects: list[str] = []
    charge_task: asyncio.Task[Any] | None = None

    async def charge() -> str:
        side_effects.append("charged")
        return "ok"

    async def fail() -> str:
        await store.append_started.wait()
        raise RuntimeError("reserve failed")

    async def flow(wf: Any) -> list[Any]:
        async def charge_branch() -> str:
            nonlocal charge_task
            charge_task = asyncio.current_task()
            return await wf.step("charge", charge)

        return await wf.parallel([charge_branch, fail])

    task: asyncio.Task[Any] | None = None
    try:
        task = asyncio.create_task(_run_workflow(agent, flow, run_id="wf-cancelled-end"))
        await asyncio.wait_for(store.append_started.wait(), timeout=2)

        async def wait_for_charge_cancel() -> None:
            while charge_task is None or charge_task.cancelling() == 0:
                await asyncio.sleep(0)

        await asyncio.wait_for(wait_for_charge_cancel(), timeout=2)
        store.allow_append.set()
        with pytest.raises(RuntimeError, match="reserve failed"):
            await task

        stored_kinds = [event.event.kind for event in await store.load_events("wf-cancelled-end")]
        assert stored_kinds.count("step_end") == 1
        assert side_effects == ["charged"]
    finally:
        store.allow_append.set()
        if task is not None and not task.done():
            task.cancel()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        await store.close()


async def test_cancelled_claimed_workflow_drains_workflow_task() -> None:
    from linch import InMemoryRunStore

    store = InMemoryRunStore()
    agent = _make_agent(CountingTextProvider(), run_store=store)
    started = asyncio.Event()
    cleaned: list[str] = []

    async def flow(wf: Any) -> str:
        started.set()
        try:
            await asyncio.sleep(5)
        finally:
            cleaned.append("cleaned")
        return "done"

    task = asyncio.create_task(_run_workflow(agent, flow, run_id="wf-cancelled-run"))
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert cleaned == ["cleaned"]
    current = asyncio.current_task()
    assert [
        candidate
        for candidate in asyncio.all_tasks()
        if not candidate.done() and candidate is not current
    ] == []


async def test_lease_loss_does_not_mark_workflow_failed(monkeypatch: Any) -> None:
    from linch import InMemoryRunStore
    from linch.ext.workflow import engine as workflow_engine
    from linch.run_store import LeaseLostError

    class LeaseLossStore(InMemoryRunStore):
        mark_failed_calls = 0

        async def renew(self, lease: Any) -> Any:
            raise LeaseLostError("lease expired")

        async def mark_failed(self, *args: Any, **kwargs: Any) -> Any:
            self.mark_failed_calls += 1
            return await super().mark_failed(*args, **kwargs)

    monkeypatch.setattr(workflow_engine, "_WORKFLOW_LEASE_TTL_S", 0.03)
    store = LeaseLossStore()
    agent = _make_agent(CountingTextProvider(), run_store=store)

    async def flow(wf: Any) -> str:
        await asyncio.sleep(1)
        return "done"

    with pytest.raises(LeaseLostError, match="lease expired"):
        await _run_workflow(agent, flow, run_id="wf-lease-loss")
    assert store.mark_failed_calls == 0
    record = await store.load_run("wf-lease-loss")
    assert record is not None
    assert record.status == "running"


async def test_repeated_cancellation_cannot_cancel_step_end_append() -> None:
    from linch import InMemoryRunStore

    class DelayedEndStore(InMemoryRunStore):
        def __init__(self) -> None:
            super().__init__()
            self.append_started = asyncio.Event()
            self.allow_append = asyncio.Event()

        async def append_event(self, run_id: str, event: Any, *, lease: Any = None) -> int:
            if getattr(event, "kind", None) == "step_end":
                self.append_started.set()
                await self.allow_append.wait()
            return await super().append_event(run_id, event, lease=lease)

    store = DelayedEndStore()
    agent = _make_agent(CountingTextProvider(), run_store=store)

    async def charge() -> str:
        return "ok"

    async def fail() -> str:
        await asyncio.sleep(0.01)
        raise RuntimeError("reserve failed")

    async def flow(wf: Any) -> list[Any]:
        return await wf.parallel([lambda: wf.step("charge", charge), fail])

    task = asyncio.create_task(_run_workflow(agent, flow, run_id="wf-repeated-cancel"))
    await asyncio.wait_for(store.append_started.wait(), timeout=1)
    await asyncio.sleep(0.03)
    task.cancel()
    store.allow_append.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    stored_kinds = [event.event.kind for event in await store.load_events("wf-repeated-cancel")]
    assert stored_kinds.count("step_end") == 1


async def test_double_cancellation_drains_workflow_cleanup() -> None:
    from linch import InMemoryRunStore

    store = InMemoryRunStore()
    agent = _make_agent(CountingTextProvider(), run_store=store)
    started = asyncio.Event()
    cleanup_started = asyncio.Event()
    cleanup_gate = asyncio.Event()
    cleanup_finished = asyncio.Event()

    async def flow(wf: Any) -> str:
        started.set()
        try:
            await asyncio.sleep(5)
        finally:
            cleanup_started.set()
            await cleanup_gate.wait()
            cleanup_finished.set()
        return "done"

    task = asyncio.create_task(_run_workflow(agent, flow, run_id="wf-double-cancel"))
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    await asyncio.wait_for(cleanup_started.wait(), timeout=1)

    async def release_cleanup() -> None:
        await asyncio.sleep(0.03)
        cleanup_gate.set()

    await asyncio.sleep(0)
    task.cancel()
    release_task = asyncio.create_task(release_cleanup())
    with pytest.raises(asyncio.CancelledError):
        await task
    await release_task

    assert cleanup_finished.is_set()
    assert agent._sessions == {}
    await asyncio.sleep(0)
    current = asyncio.current_task()
    assert [
        candidate
        for candidate in asyncio.all_tasks()
        if not candidate.done() and candidate is not current
    ] == []


async def test_release_failure_still_cleans_host_session() -> None:
    from linch import InMemoryRunStore

    class FailingReleaseStore(InMemoryRunStore):
        async def release(self, lease: Any) -> None:
            raise RuntimeError("release failed")

    agent = _make_agent(CountingTextProvider(), run_store=FailingReleaseStore())

    async def flow(wf: Any) -> str:
        return "done"

    with pytest.raises(RuntimeError, match="release failed"):
        await _run_workflow(agent, flow, run_id="wf-release-failure")
    assert agent._sessions == {}


async def test_lease_heartbeat_covers_slow_terminal_status_write(monkeypatch: Any) -> None:
    from linch import InMemoryRunStore
    from linch.ext.workflow import engine as workflow_engine

    class SlowTerminalStore(InMemoryRunStore):
        renew_calls = 0
        terminal_started: asyncio.Event

        def __init__(self) -> None:
            super().__init__()
            self.terminal_started = asyncio.Event()

        async def renew(self, lease: Any) -> Any:
            self.renew_calls += 1
            return await super().renew(lease)

        async def mark_completed(self, *args: Any, **kwargs: Any) -> Any:
            self.terminal_started.set()
            await asyncio.sleep(0.05)
            return await super().mark_completed(*args, **kwargs)

    monkeypatch.setattr(workflow_engine, "_WORKFLOW_LEASE_TTL_S", 0.03)
    store = SlowTerminalStore()
    agent = _make_agent(CountingTextProvider(), run_store=store)

    async def flow(wf: Any) -> str:
        return "done"

    assert await _run_workflow(agent, flow, run_id="wf-slow-terminal") == "done"
    assert store.terminal_started.is_set()
    assert store.renew_calls >= 1


async def test_supplied_workflow_lease_ttl_controls_heartbeat(monkeypatch: Any) -> None:
    from linch.ext.workflow import engine as workflow_engine
    from linch.run_store import RunLease

    delays: list[float] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)
        raise asyncio.CancelledError

    lease = RunLease("wf-ttl", "owner", 1, 100.0, 0.6)
    monkeypatch.setattr(workflow_engine.asyncio, "sleep", sleep)

    with pytest.raises(asyncio.CancelledError):
        await workflow_engine._renew_workflow_lease(object(), [lease])

    assert delays == pytest.approx([0.2])


async def test_wf_agent_inherited_subagent_tool_filter_is_part_of_replay_key() -> None:
    from linch import InMemoryRunStore
    from linch.subagents.registry import AgentRegistry
    from linch.subagents.types import AgentDefinition, AgentFrontmatter

    provider = CountingTextProvider()
    agent = _make_agent(provider, run_store=InMemoryRunStore())
    definition = AgentDefinition(
        name="reviewer",
        file_path="<test>",
        source="disk",
        frontmatter=AgentFrontmatter(
            name="reviewer",
            description="Review the result.",
            tools=["Read"],
        ),
        body="Review the result.",
    )
    agent.subagent_registry = AgentRegistry([definition])
    agent._subagents_loaded = True

    async def flow(wf: Any) -> str:
        return await wf.agent("same prompt", name="reviewer")

    first = await _run_workflow(agent, flow, run_id="wf-inherited-tool-key")
    definition.frontmatter.tools = ["Grep"]
    second = await _run_workflow(agent, flow, run_id="wf-inherited-tool-key")

    assert first == "result-1"
    assert second == "result-2"
    assert provider.calls == 2


async def test_wf_agent_passes_isolation_to_subagent() -> None:
    from linch.ext.evals import ScriptedProvider, TextTurn, ToolUseTurn
    from linch.tools import ToolRegistry, tool
    from linch.tools.isolation import TempDirIsolation

    recorded: list[str] = []

    @tool
    def record_cwd(ctx: Any) -> str:
        """Record the execution cwd."""
        recorded.append(ctx.cwd)
        return "ok"

    tools = ToolRegistry()
    tools.register(record_cwd)
    provider = ScriptedProvider(
        [ToolUseTurn(tool_name="record_cwd", tool_input={}), TextTurn("done")]
    )
    agent = _make_agent(provider, tools=tools)
    iso = TempDirIsolation()

    async def flow(wf: Any) -> str:
        return await wf.agent("go", isolation=iso)

    await _run_workflow(agent, flow)

    assert len(recorded) == 1
    assert recorded[0] != agent.cwd


async def test_wf_agent_unknown_name_raises_config_error() -> None:
    from linch.errors import ConfigError

    agent = _make_agent(CountingTextProvider())

    async def flow(wf: Any) -> str:
        return await wf.agent("do it", name="no-such-agent")

    with pytest.raises(ConfigError, match="no-such-agent"):
        await _run_workflow(agent, flow)


async def test_budget_shared_across_workflow_children() -> None:
    from linch import RunBudget

    provider = CountingTextProvider(tokens_per_turn=100)
    agent = _make_agent(provider)
    budget = RunBudget(max_tokens=10_000)

    async def flow(wf: Any) -> list[str]:
        first = await wf.agent("task one")
        second = await wf.agent("task two")
        assert wf.budget is budget
        return [first, second]

    result = await _run_workflow(agent, flow, budget=budget)

    assert result == ["result-1", "result-2"]
    assert budget.spent_tokens == 200


async def test_resume_replays_unchanged_prefix(
    pg_dsn,
) -> None:
    from linch import PostgresRunStore
    from linch.ext.workflow import WorkflowError

    async def flow(wf: Any) -> list[str]:
        one = await wf.agent("step one")
        two = await wf.agent("step two")
        three = await wf.agent("step three")
        return [one, two, three]

    # First attempt: provider dies on the third subagent call.
    provider1 = CountingTextProvider(fail_on_call=3)
    agent1 = _make_agent(provider1, run_store=PostgresRunStore(pg_dsn))
    with pytest.raises(WorkflowError):
        await _run_workflow(agent1, flow, run_id="wf-resume-1")
    assert provider1.calls == 3
    saved = await agent1.run_store.load_run("wf-resume-1")
    assert saved is not None
    assert saved.meta == {"schema_version": 3}

    # Resume: calls 1-2 replay from the journal; only call 3 hits the provider.
    await agent1.close()

    provider2 = CountingTextProvider()
    agent2 = _make_agent(provider2, run_store=PostgresRunStore(pg_dsn))
    seen: list[Any] = []
    result = await _run_workflow(agent2, flow, run_id="wf-resume-1", on_event=seen.append)

    assert provider2.calls == 1
    replays = [e for e in seen if e.type == "workflow" and e.kind == "agent_replayed"]
    assert len(replays) == 2
    assert result == ["result-1", "result-2", "result-1"]
    resumed = await agent2.run_store.load_run("wf-resume-1")
    assert resumed is not None
    assert resumed.meta == {"schema_version": 3}

    await agent2.close()


async def test_changed_prompt_invalidates_cache(
    pg_dsn,
) -> None:
    from linch import PostgresRunStore

    async def flow_v1(wf: Any) -> list[str]:
        return [await wf.agent("step one"), await wf.agent("step two")]

    provider1 = CountingTextProvider()
    agent1 = _make_agent(provider1, run_store=PostgresRunStore(pg_dsn))
    await _run_workflow(agent1, flow_v1, run_id="wf-edit-1")
    assert provider1.calls == 2

    async def flow_v2(wf: Any) -> list[str]:
        return [await wf.agent("step one"), await wf.agent("step two, reworded")]

    await agent1.close()
    provider2 = CountingTextProvider()
    agent2 = _make_agent(provider2, run_store=PostgresRunStore(pg_dsn))
    result = await _run_workflow(agent2, flow_v2, run_id="wf-edit-1")

    assert provider2.calls == 1
    assert result == ["result-1", "result-1"]
    await agent2.close()


@pytest.mark.parametrize(
    ("version", "with_events"),
    [
        (None, False),  # schema_version missing entirely
        (None, True),  # ...and existing journal events still can't bless it
        (1, False),  # a plain wrong version number
        (True, False),  # bool is an int subclass: True must not pass as version 1
        ("3", False),  # a stringified correct version must not pass
        (3.0, False),  # loose equality: 3.0 == 3 in Python; strict check must still reject it
        (3.0, True),  # ...and existing journal events still can't bless it
    ],
)
async def test_resume_rejects_incompatible_workflow_version(
    version: object, with_events: bool
) -> None:
    from linch import InMemoryRunStore
    from linch._wire import UnsupportedWireVersionError
    from linch.events import WorkflowEvent
    from linch.ext.workflow.journal import call_key

    store = InMemoryRunStore()
    await store.create_run(
        "host-session",
        id="wf-old-resume",
        meta={} if version is None else {"schema_version": version},
    )
    if with_events:
        # Current event envelopes cannot bless an incompatible workflow row.
        await store.append_event(
            "wf-old-resume",
            WorkflowEvent(
                kind="agent_end",
                title="agent",
                call_key=call_key("general-purpose", "same prompt"),
                occurrence=0,
                result_text="old result",
            ),
        )
    provider = CountingTextProvider()
    agent = _make_agent(provider, run_store=store)
    invoked = False

    async def flow(wf: Any) -> str:
        nonlocal invoked
        invoked = True
        return await wf.agent("same prompt", tools=[])

    with pytest.raises(UnsupportedWireVersionError, match="start a new run"):
        await _run_workflow(agent, flow, run_id="wf-old-resume")

    assert invoked is False
    assert provider.calls == 0


async def test_run_id_without_run_store_raises_config_error() -> None:
    from linch.errors import ConfigError

    agent = _make_agent(CountingTextProvider())

    async def flow(wf: Any) -> None:
        return None

    with pytest.raises(ConfigError, match="run_store"):
        await _run_workflow(agent, flow, run_id="wf-1")


async def test_workflow_timeout_error_is_a_retryable_workflow_error() -> None:
    from linch.ext.workflow import WorkflowError, WorkflowTimeoutError

    assert issubclass(WorkflowTimeoutError, WorkflowError)
    assert WorkflowTimeoutError("x").retryable is True


async def test_agent_timeout_cancels_the_child_and_leaks_no_tasks() -> None:
    from linch.ext.workflow import WorkflowTimeoutError

    class BlockingProvider(CountingTextProvider):
        async def stream(self, req: Any) -> AsyncIterator[dict[str, object]]:
            await asyncio.Event().wait()
            yield {}  # pragma: no cover - never reached

    agent = _make_agent(BlockingProvider())

    async def flow(wf: Any) -> str:
        return await wf.agent("hangs forever", timeout_ms=50)

    with pytest.raises(WorkflowTimeoutError, match="timed out"):
        await _run_workflow(agent, flow)

    await asyncio.sleep(0)
    await asyncio.sleep(0)
    current = asyncio.current_task()
    assert [t for t in asyncio.all_tasks() if not t.done() and t is not current] == []


async def test_run_workflow_signal_stops_agent_and_step_calls() -> None:
    from linch.abort import AbortContext
    from linch.errors import AbortError

    signal = AbortContext()
    signal.abort()
    provider = CountingTextProvider()
    agent = _make_agent(provider)
    ran: list[str] = []

    async def flow(wf: Any) -> str:
        return await wf.agent("never runs")

    with pytest.raises(AbortError):
        await _run_workflow(agent, flow, signal=signal)
    assert provider.calls == 0

    async def step_flow(wf: Any) -> str:
        return await wf.step("never runs", lambda: ran.append("x") or "x")

    with pytest.raises(AbortError):
        await _run_workflow(agent, step_flow, signal=signal)
    assert ran == []


async def test_wf_agent_retry_reruns_a_failed_subagent_and_journals_once() -> None:
    from linch.providers.retry import RetryOptions
    from linch.run_store import InMemoryRunStore

    provider = CountingTextProvider(fail_on_call=1)
    store = InMemoryRunStore()
    agent = _make_agent(provider, run_store=store)

    async def flow(wf: Any) -> str:
        return await wf.agent(
            "flaky",
            retry=RetryOptions(max_attempts=3, base_delay_ms=0, max_delay_ms=0, jitter=0.0),
        )

    result = await _run_workflow(agent, flow, run_id="wf-retry-1")
    assert result == "result-2"
    assert provider.calls == 2
    stored = await store.load_events("wf-retry-1")
    ends = [s.event for s in stored if getattr(s.event, "kind", None) == "agent_end"]
    assert len(ends) == 1
    assert ends[0].occurrence == 0


async def test_wf_agent_retry_exhaustion_journals_nothing_and_reruns_on_resume() -> None:
    from linch.ext.workflow import WorkflowError
    from linch.providers.retry import RetryOptions
    from linch.run_store import InMemoryRunStore

    class AlwaysFailingProvider(CountingTextProvider):
        async def stream(self, req: Any) -> AsyncIterator[dict[str, object]]:
            self.calls += 1
            raise RuntimeError("provider blew up")
            yield {}  # pragma: no cover - unreachable, makes this an async generator

    store = InMemoryRunStore()
    always_failing = AlwaysFailingProvider()

    async def flow(wf: Any) -> str:
        return await wf.agent(
            "flaky",
            retry=RetryOptions(max_attempts=2, base_delay_ms=0, max_delay_ms=0, jitter=0.0),
        )

    agent1 = _make_agent(always_failing, run_store=store)
    with pytest.raises(WorkflowError):
        await _run_workflow(agent1, flow, run_id="wf-retry-2")
    assert always_failing.calls == 2

    stored = await store.load_events("wf-retry-2")
    assert [s.event for s in stored if getattr(s.event, "kind", None) == "agent_end"] == []

    agent2 = _make_agent(CountingTextProvider(), run_store=store)
    assert await _run_workflow(agent2, flow, run_id="wf-retry-2") == "result-1"


async def test_workflow_suspended_escapes_a_user_except_exception() -> None:
    from linch.ext.workflow import WorkflowSuspended
    from linch.run_store import InMemoryRunStore

    agent = _make_agent(CountingTextProvider(), run_store=InMemoryRunStore())
    swallowed: list[str] = []

    async def flow(wf: Any) -> str:
        try:
            return await wf.interrupt("approve")
        except Exception:
            swallowed.append("caught")
            return "swallowed"

    with pytest.raises(WorkflowSuspended):
        await _run_workflow(agent, flow, run_id="wf-hitl-2")
    assert swallowed == []


async def test_interrupt_suspends_without_marking_the_run_failed() -> None:
    from linch.ext.workflow import WorkflowSuspended
    from linch.run_store import InMemoryRunStore

    store = InMemoryRunStore()
    agent = _make_agent(CountingTextProvider(), run_store=store)
    seen: list[Any] = []

    async def flow(wf: Any) -> str:
        return await wf.interrupt("approve", {"diff": "one line"})

    with pytest.raises(WorkflowSuspended) as excinfo:
        await _run_workflow(agent, flow, run_id="wf-hitl-1", on_event=seen.append)
    assert excinfo.value.key == "approve"
    assert excinfo.value.payload == {"diff": "one line"}
    record = await store.load_run("wf-hitl-1")
    assert record is not None
    assert record.status == "suspended"
    requests = [e for e in seen if e.type == "workflow" and e.kind == "interrupt_requested"]
    assert len(requests) == 1
    assert requests[0].structured_output == {"payload": {"diff": "one line"}}
    assert agent._sessions == {}


async def test_resume_supplies_the_interrupt_value_and_completes() -> None:
    from linch.ext.workflow import WorkflowSuspended
    from linch.run_store import InMemoryRunStore

    store = InMemoryRunStore()
    provider = CountingTextProvider()
    agent = _make_agent(provider, run_store=store)

    async def flow(wf: Any) -> list[Any]:
        first = await wf.agent("draft it")
        approved = await wf.interrupt("approve", {"draft": first})
        return [first, approved]

    with pytest.raises(WorkflowSuspended):
        await _run_workflow(agent, flow, run_id="wf-hitl-3")
    assert provider.calls == 1

    result = await _run_workflow(agent, flow, run_id="wf-hitl-3", resume={"approve": True})

    assert result == ["result-1", True]
    assert provider.calls == 1  # the agent prefix replayed

    record = await store.load_run("wf-hitl-3")
    assert record is not None
    assert record.status == "completed"

    # A later resume needs no resume mapping: the answer itself is journaled.
    seen: list[Any] = []
    assert await _run_workflow(agent, flow, run_id="wf-hitl-3", on_event=seen.append) == [
        "result-1",
        True,
    ]
    assert [e for e in seen if e.type == "workflow" and e.kind == "interrupt_replayed"]


async def test_interrupt_inside_parallel_cancels_siblings_and_suspends() -> None:
    """A fan-out cannot collect several interrupts; siblings still clean up."""
    from linch.ext.workflow import WorkflowSuspended
    from linch.run_store import InMemoryRunStore

    agent = _make_agent(CountingTextProvider(), run_store=InMemoryRunStore())
    cleaned: list[str] = []

    async def flow(wf: Any) -> list[Any]:
        async def sibling() -> str:
            try:
                await asyncio.sleep(1.0)
                return "done"
            finally:
                cleaned.append("sibling")

        return await wf.parallel([lambda: wf.interrupt("approve"), sibling])

    with pytest.raises(WorkflowSuspended):
        await _run_workflow(agent, flow, run_id="wf-hitl-4")

    assert cleaned == ["sibling"]


async def test_deadline_stops_the_workflow_and_marks_the_run_failed() -> None:
    from linch.ext.workflow import WorkflowTimeoutError
    from linch.run_store import InMemoryRunStore

    store = InMemoryRunStore()
    agent = _make_agent(CountingTextProvider(), run_store=store)
    cleaned: list[str] = []

    async def flow(wf: Any) -> str:
        async def slow() -> str:
            try:
                await asyncio.sleep(1.0)
                return "never"
            finally:
                cleaned.append("slow")

        return await wf.step("slow", slow)

    with pytest.raises(WorkflowTimeoutError, match="deadline"):
        await _run_workflow(agent, flow, run_id="wf-deadline-1", deadline_ms=20)

    assert cleaned == ["slow"]
    record = await store.load_run("wf-deadline-1")
    assert record is not None
    assert record.status == "failed"
    assert agent._sessions == {}


async def test_deadline_leaves_the_journaled_prefix_resumable() -> None:
    from linch.ext.workflow import WorkflowTimeoutError
    from linch.run_store import InMemoryRunStore

    store = InMemoryRunStore()
    provider = CountingTextProvider()
    agent = _make_agent(provider, run_store=store)
    side_effects: list[str] = []

    async def flow(wf: Any, *, stall: bool) -> str:
        await wf.step("publish", lambda: side_effects.append("published") or "ok")
        if stall:
            await asyncio.sleep(1.0)
        return await wf.agent("summarize")

    async def stalling(wf: Any) -> str:
        return await flow(wf, stall=True)

    async def finishing(wf: Any) -> str:
        return await flow(wf, stall=False)

    with pytest.raises(WorkflowTimeoutError):
        await _run_workflow(agent, stalling, run_id="wf-deadline-2", deadline_ms=30)

    assert await _run_workflow(agent, finishing, run_id="wf-deadline-2") == "result-1"
    assert side_effects == ["published"]  # replayed, not re-executed


async def test_no_deadline_never_reaches_wait_for(monkeypatch: Any) -> None:
    agent = _make_agent(CountingTextProvider())

    def explode(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("wait_for must not be called without a deadline")

    monkeypatch.setattr(asyncio, "wait_for", explode)

    async def flow(wf: Any) -> str:
        return "ok"

    assert await _run_workflow(agent, flow) == "ok"


class ConcurrencyTrackingProvider(CountingTextProvider):
    """Records how many subagent runs were in flight at once."""

    def __init__(self) -> None:
        super().__init__()
        self.active = 0
        self.high_water = 0

    async def stream(self, req: Any) -> Any:
        self.active += 1
        self.high_water = max(self.high_water, self.active)
        try:
            await asyncio.sleep(0.01)
            async for chunk in super().stream(req):
                yield chunk
        finally:
            self.active -= 1


@pytest.mark.parametrize(("limit", "expected_high_water"), [(2, 2), (None, 6)])
async def test_max_agent_concurrency_option(limit: int | None, expected_high_water: int) -> None:
    provider = ConcurrencyTrackingProvider()
    agent = _make_agent(provider)

    async def flow(wf: Any) -> list[str]:
        return await wf.parallel([lambda i=i: wf.agent(f"task {i}") for i in range(6)])

    options = {"max_concurrency": 8 if limit is not None else 6}
    if limit is not None:
        options["max_agent_concurrency"] = limit
    results = await _run_workflow(agent, flow, **options)

    assert len(results) == 6
    assert provider.high_water == expected_high_water


async def test_max_agent_concurrency_does_not_gate_the_replay_path() -> None:
    """A replayed prefix makes no provider call, so it must not queue behind the gate."""
    from linch.run_store import InMemoryRunStore

    store = InMemoryRunStore()
    provider = CountingTextProvider()
    agent = _make_agent(provider, run_store=store)

    async def flow(wf: Any) -> list[str]:
        return await wf.parallel([lambda i=i: wf.agent(f"task {i}") for i in range(4)])

    first = await _run_workflow(agent, flow, run_id="wf-gate-1", max_agent_concurrency=1)
    second = await _run_workflow(agent, flow, run_id="wf-gate-1", max_agent_concurrency=1)

    assert second == first
    assert provider.calls == 4


def _recording_store() -> Any:
    """An InMemoryRunStore that remembers every load_events watermark."""
    from linch.run_store import InMemoryRunStore

    class RecordingStore(InMemoryRunStore):
        def __init__(self) -> None:
            super().__init__()
            self.after_seqs: list[int] = []

        async def load_events(self, run_id: str, *, after_seq: int = 0) -> Any:
            self.after_seqs.append(after_seq)
            return await super().load_events(run_id, after_seq=after_seq)

    return RecordingStore()


async def test_snapshot_is_off_by_default() -> None:
    store = _recording_store()
    agent = _make_agent(CountingTextProvider(), run_store=store)

    async def flow(wf: Any) -> list[Any]:
        return [await wf.step(f"s{i}", lambda i=i: i) for i in range(4)]

    assert await _run_workflow(agent, flow, run_id="wf-snap-0") == [0, 1, 2, 3]
    record = await store.load_run("wf-snap-0")
    assert record is not None
    assert record.checkpoint is not None
    assert record.checkpoint.extension_state == {}


async def test_snapshot_lets_resume_skip_the_folded_event_prefix() -> None:
    from linch.ext.workflow import WorkflowSuspended

    store = _recording_store()
    provider = CountingTextProvider()
    agent = _make_agent(provider, run_store=store)
    side_effects: list[str] = []

    async def flow(wf: Any) -> Any:
        for i in range(4):
            await wf.step(f"s{i}", lambda i=i: side_effects.append(f"s{i}") or i)
        await wf.agent("summarize")
        return await wf.interrupt("ship")

    with pytest.raises(WorkflowSuspended):
        await _run_workflow(agent, flow, run_id="wf-snap-1", journal_snapshot_every=2)
    assert side_effects == ["s0", "s1", "s2", "s3"]

    saved = await store.load_run("wf-snap-1")
    assert saved is not None
    assert saved.checkpoint is not None
    snapshot = saved.checkpoint.extension_state["linch.workflow"]
    assert snapshot["after_seq"] > 0
    assert len(snapshot["records"]) == 4  # the 4 steps; the agent came later

    store.after_seqs.clear()
    resumed = await _run_workflow(agent, flow, run_id="wf-snap-1", resume={"ship": True})

    assert resumed is True
    assert store.after_seqs == [snapshot["after_seq"]]  # tail only
    assert side_effects == ["s0", "s1", "s2", "s3"]  # nothing re-executed
    assert provider.calls == 1  # the agent_end in the tail replayed


async def test_snapshot_survives_a_suspend() -> None:
    from linch.ext.workflow import WorkflowSuspended

    store = _recording_store()
    agent = _make_agent(CountingTextProvider(), run_store=store)
    side_effects: list[str] = []

    async def flow(wf: Any) -> Any:
        await wf.step("publish", lambda: side_effects.append("published") or "ok")
        await wf.step("notify", lambda: side_effects.append("notified") or "ok")
        return await wf.interrupt("approve")

    with pytest.raises(WorkflowSuspended):
        await _run_workflow(agent, flow, run_id="wf-snap-2", journal_snapshot_every=2)

    saved = await store.load_run("wf-snap-2")
    assert saved is not None
    assert saved.status == "suspended"
    assert saved.checkpoint is not None
    assert len(saved.checkpoint.extension_state["linch.workflow"]["records"]) == 2

    assert await _run_workflow(agent, flow, run_id="wf-snap-2", resume={"approve": "yes"}) == "yes"
    assert side_effects == ["published", "notified"]


async def test_an_oversized_snapshot_is_skipped_not_stored(monkeypatch: Any) -> None:
    """A journal too big to checkpoint falls back to folding the event log."""
    from linch.ext.workflow import WorkflowSuspended
    from linch.ext.workflow import context as wf_context

    store = _recording_store()
    agent = _make_agent(CountingTextProvider(), run_store=store)
    monkeypatch.setattr(wf_context, "_SNAPSHOT_MAX_BYTES", 512)

    async def flow(wf: Any) -> Any:
        await wf.step("big", lambda: "x" * 2048)
        return await wf.interrupt("approve")

    with pytest.raises(WorkflowSuspended):
        await _run_workflow(agent, flow, run_id="wf-snap-3", journal_snapshot_every=1)

    saved = await store.load_run("wf-snap-3")
    assert saved is not None
    assert saved.checkpoint is not None
    assert "linch.workflow" not in saved.checkpoint.extension_state

    store.after_seqs.clear()
    assert await _run_workflow(agent, flow, run_id="wf-snap-3", resume={"approve": "yes"}) == "yes"
    assert store.after_seqs == [0]  # no watermark, so the whole log is folded


def test_workflow_event_round_trips_every_kind() -> None:
    """Every declared kind must survive the codec — a kind added to the Literal
    without a matching decoder entry silently degrades to ``phase``."""
    from linch.events import (
        WORKFLOW_EVENT_KINDS,
        WorkflowEvent,
        event_from_dict,
        event_to_dict,
    )

    for kind in WORKFLOW_EVENT_KINDS:
        event = WorkflowEvent(
            kind=kind,  # type: ignore[arg-type]
            title="summarizer",
            call_key="abc123",
            occurrence=2,
            subagent_type="_default",
            result_text="done",
        )

        assert event_from_dict(event_to_dict(event)) == event


async def test_step_and_agent_prefix_replay_together_after_failure(
    pg_dsn,
) -> None:
    """The double-execution hole: a resumed workflow must re-run neither its
    journaled agent calls nor its journaled code steps."""
    from linch import PostgresRunStore
    from linch.ext.workflow import WorkflowError

    side_effects: list[str] = []

    async def flow(wf: Any) -> list[Any]:
        async def publish() -> str:
            side_effects.append("published")
            return "ok"

        first = await wf.agent("step one")
        published = await wf.step("publish", publish)
        second = await wf.agent("step two")
        return [first, published, second]

    provider1 = CountingTextProvider(fail_on_call=2)
    agent1 = _make_agent(provider1, run_store=PostgresRunStore(pg_dsn))
    with pytest.raises(WorkflowError):
        await _run_workflow(agent1, flow, run_id="wf-mixed-1")
    assert side_effects == ["published"]

    await agent1.close()

    provider2 = CountingTextProvider()
    agent2 = _make_agent(provider2, run_store=PostgresRunStore(pg_dsn))
    seen: list[Any] = []
    result = await _run_workflow(agent2, flow, run_id="wf-mixed-1", on_event=seen.append)

    assert side_effects == ["published"]  # ran exactly once across both attempts
    assert provider2.calls == 1
    assert result == ["result-1", "ok", "result-1"]
    replays = [e for e in seen if e.type == "workflow" and e.kind == "step_replayed"]
    assert len(replays) == 1

    await agent2.close()


async def test_step_in_a_loop_replays_per_occurrence() -> None:
    from linch.run_store import InMemoryRunStore

    calls: list[int] = []

    async def flow(wf: Any) -> list[int]:
        out: list[int] = []
        for i in range(3):

            async def tick(i: int = i) -> int:
                calls.append(i)
                return i * 10

            out.append(await wf.step("tick", tick))
        return out

    agent = _make_agent(CountingTextProvider(), run_store=InMemoryRunStore())

    assert await _run_workflow(agent, flow, run_id="wf-loop-1") == [0, 10, 20]
    assert calls == [0, 1, 2]

    seen: list[Any] = []
    assert await _run_workflow(agent, flow, run_id="wf-loop-1", on_event=seen.append) == [0, 10, 20]

    assert calls == [0, 1, 2]
    replays = [e for e in seen if e.type == "workflow" and e.kind == "step_replayed"]
    assert [e.occurrence for e in replays] == [0, 1, 2]


async def test_adding_a_step_does_not_invalidate_the_existing_agent_journal(
    pg_dsn,
) -> None:
    """Adding a step must not perturb already journaled agent call keys."""
    from linch import PostgresRunStore

    async def flow_v1(wf: Any) -> list[str]:
        return [await wf.agent("step one"), await wf.agent("step two")]

    provider1 = CountingTextProvider()
    agent1 = _make_agent(provider1, run_store=PostgresRunStore(pg_dsn))
    await _run_workflow(agent1, flow_v1, run_id="wf-add-step-1")
    assert provider1.calls == 2

    async def flow_v2(wf: Any) -> list[Any]:
        note = await wf.step("note", lambda: "added later")
        return [note, await wf.agent("step one"), await wf.agent("step two")]

    await agent1.close()

    provider2 = CountingTextProvider()
    agent2 = _make_agent(provider2, run_store=PostgresRunStore(pg_dsn))
    result = await _run_workflow(agent2, flow_v2, run_id="wf-add-step-1")

    assert provider2.calls == 0
    assert result == ["added later", "result-1", "result-2"]

    await agent2.close()
