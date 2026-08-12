"""Durable extension state captured and restored through the existing run loop."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest


class _TextProvider:
    id = "checkpointable-hook-test"

    def context_window(self, model: str) -> int:
        return 100_000

    async def stream(self, req: Any) -> AsyncIterator[dict[str, object]]:
        from linch.types import Usage

        yield {"type": "message_start", "model": req.model}
        yield {"type": "text_delta", "text": "done"}
        yield {"type": "message_end", "stop_reason": "end_turn", "usage": Usage()}


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


def _agent(*, provider: Any, session_store: Any, run_store: Any, hooks: list[Any]):
    from linch import Agent
    from linch.config import FeatureFlags
    from linch.tools.registry import empty_tools

    return Agent(
        model="test-model",
        provider=provider,
        tools=empty_tools(),
        session_store=session_store,
        run_store=run_store,
        hooks=hooks,
        features=FeatureFlags(skills=False, subagents=False, mcp=False, filesystem=False),
        read_before_write=False,
        result_offload=None,
        cwd=".",
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

    agent = _agent(
        provider=_TextProvider(),
        session_store=session_store,
        run_store=run_store,
        hooks=[hook],
    )
    session = await agent.session(id="session-1")
    events = await _collect(session.run("hello"))
    run_id = next(event.run_id for event in events if event.type == "system")

    run = await run_store.load_run(run_id)
    assert run is not None and run.checkpoint is not None
    assert run.checkpoint.extension_state == {"test.durable-state": hook.state}
    assert hook.snapshots
    assert {session_id for session_id, _ in hook.snapshots} == {"session-1"}
    assert {saved_run_id for _, saved_run_id in hook.snapshots} == {run_id}


async def test_checkpointable_hook_restores_matching_state_and_preserves_other_extensions() -> None:
    from linch.run_store import InMemoryRunStore, RunCheckpoint
    from linch.session import RunOptions
    from linch.sessions import InMemorySessionStore
    from linch.types import Usage

    session_store = InMemorySessionStore()
    run_store = InMemoryRunStore()
    await session_store.create(id="session-restore")
    run = await run_store.create_run("session-restore", id="run-restore")
    await run_store.save_checkpoint(
        run.id,
        RunCheckpoint(
            phase="turn_complete",
            prompt="continue",
            turn_index=0,
            total_usage=Usage(),
            extension_state={
                "test.durable-state": {"profile": "pinned", "repair": {"used": 1}},
                "another-extension": {"must": "survive"},
            },
        ),
    )
    hook = _DurableStateHook({"profile": "pinned", "repair": {"used": 2}})
    agent = _agent(
        provider=_TextProvider(),
        session_store=session_store,
        run_store=run_store,
        hooks=[hook],
    )
    session = await agent.session(id="session-restore")

    events = await _collect(session.resume(run.id, RunOptions(allow_legacy_resume=True)))

    assert events[-1].type == "result"
    assert hook.restored == [
        ({"profile": "pinned", "repair": {"used": 1}}, "session-restore", "run-restore")
    ]
    loaded = await run_store.load_run(run.id)
    assert loaded is not None and loaded.checkpoint is not None
    assert loaded.checkpoint.extension_state["another-extension"] == {"must": "survive"}
    assert loaded.checkpoint.extension_state["test.durable-state"] == hook.state


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

    agent = _agent(
        provider=_TextProvider(),
        session_store=InMemorySessionStore(),
        run_store=InMemoryRunStore(),
        hooks=[_IncompleteHook()],
    )
    session = await agent.session()

    with pytest.raises(ConfigError, match="restore_checkpoint_state"):
        await _collect(session.run("hello"))
