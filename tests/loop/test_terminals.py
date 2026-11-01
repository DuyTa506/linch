"""Terminal tail persistence regressions."""

from __future__ import annotations

from typing import Any

from tests.support.agents import make_agent
from tests.support.providers import ScriptedProvider, TextTurn


class _FailOnceMarkFailedStore:
    """Fail the first failed-status write, then persist subsequent writes."""

    def __init__(self) -> None:
        from linch.run_store import InMemoryRunStore

        self._inner = InMemoryRunStore()
        self.mark_failed_calls = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def mark_failed(
        self,
        run_id: str,
        checkpoint: Any = None,
        error: dict[str, object] | None = None,
        *,
        events: list[Any] = (),
    ) -> Any:
        self.mark_failed_calls += 1
        if self.mark_failed_calls == 1:
            raise OSError("failed status write")
        return await self._inner.mark_failed(run_id, checkpoint, error=error, events=events)


async def test_failed_terminal_tail_recovery_persists_one_result_event() -> None:
    from linch import HookResult
    from linch.sessions import InMemorySessionStore

    class StopHook:
        resume_policy_id = "test.terminal-tail-stop"
        resume_policy_version = "1"

        def on_before_final_answer(self, ctx: Any) -> Any:
            return HookResult.stop("blocked")

    store = _FailOnceMarkFailedStore()
    agent = make_agent(
        ScriptedProvider([TextTurn("done")]),
        model="terminal-tail-model",
        permissions={"mode": "skip-dangerous"},
        session_store=InMemorySessionStore(),
        run_store=store,
        hooks=[StopHook()],
        cwd=".",
    )
    session = await agent.session(id="terminal-tail")

    events = [event async for event in session.run("hello")]
    run_id = next(event.run_id for event in events if event.run_id is not None)
    rows = await store.load_events(run_id)

    assert store.mark_failed_calls == 2
    assert sum(row.event.type == "result" for row in rows) == 1
    assert sum(event.type == "result" for event in events) == 1
