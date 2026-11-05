"""Durable extension state captured and restored through the existing run loop."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from tests.support.agents import make_agent
from tests.support.providers import ScriptedProvider, TextTurn


class _DurableStateHook:
    checkpoint_key = "test.durable-state"
    resume_policy_id = "test.durable-state-policy"
    resume_policy_version = "1"
    resume_policy_config = {"fixture": "durable-state"}

    def __init__(self, state: dict[str, Any]) -> None:
        self.state = state
        self.snapshots: list[tuple[str, str]] = []
        self.restored: list[tuple[dict[str, Any], str, str]] = []

    def checkpoint_state(self, session: Any, run_id: str) -> dict[str, Any]:
        self.snapshots.append((session.id, run_id))
        return self.state

    def restore_checkpoint_state(self, state: dict[str, Any], session: Any, run_id: str) -> None:
        self.restored.append((state, session.id, run_id))


def _hook_agent(session_store: Any, run_store: Any, hook: Any) -> Any:
    return make_agent(
        ScriptedProvider([TextTurn("done")], context_window=100_000),
        session_store=session_store,
        run_store=run_store,
        hooks=[hook],
        result_offload=None,
        read_before_write=False,
    )


async def _collect(events: AsyncIterator[Any]) -> list[Any]:
    return [event async for event in events]


async def test_checkpointable_hook_snapshots_session_scoped_json_state() -> None:
    from linch.hooks import CheckpointableHook
    from linch.run_store import InMemoryRunStore
    from linch.sessions import InMemorySessionStore

    session_store = InMemorySessionStore()
    run_store = InMemoryRunStore()
    hook = _DurableStateHook({"profile": "hash-1", "repair": {"used": 1}})
    assert isinstance(hook, CheckpointableHook)

    agent = _hook_agent(session_store, run_store, hook)
    session = await agent.session(id="session-1")
    events = await _collect(session.run("hello"))
    run_id = next(event.run_id for event in events if event.type == "system")

    run = await run_store.load_run(run_id)
    assert run is not None and run.checkpoint is not None
    assert run.checkpoint.extension_state["test.durable-state"] == hook.state
    accounting = run.checkpoint.extension_state["linch.model_calls.v1"]
    assert accounting["version"] == 3
    assert accounting["format"] == "journal-v1"
    assert accounting["active_primary"] is None
    assert accounting["ledger"] is None
    assert isinstance(accounting["journal_count"], int)
    assert accounting["journal_count"] > 0
    assert hook.snapshots
    assert {session_id for session_id, _ in hook.snapshots} == {"session-1"}
    assert {saved_run_id for _, saved_run_id in hook.snapshots} == {run_id}


async def test_incomplete_checkpointable_hook_fails_closed_with_configuration_error() -> None:
    from linch.errors import ConfigError
    from linch.run_store import InMemoryRunStore
    from linch.sessions import InMemorySessionStore

    class _IncompleteHook:
        checkpoint_key = "test.incomplete"
        resume_policy_id = "test.incomplete-policy"
        resume_policy_config: dict[str, Any] = {}

        def checkpoint_state(self, session: Any, run_id: str) -> dict[str, Any]:
            return {}

    agent = _hook_agent(InMemorySessionStore(), InMemoryRunStore(), _IncompleteHook())
    session = await agent.session()

    with pytest.raises(ConfigError, match="restore_checkpoint_state"):
        await _collect(session.run("hello"))
