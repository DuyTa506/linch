from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tests.support.agents import make_agent
from tests.support.providers import ScriptedProvider, TextTurn, ToolUseTurn


async def _pending_background_tasks() -> list[asyncio.Task[Any]]:
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    current = asyncio.current_task()
    return [t for t in asyncio.all_tasks() if not t.done() and t is not current]


async def test_run_subagent_does_not_leak_watcher_tasks() -> None:
    from linch.subagents.default_agent import DEFAULT_AGENT
    from linch.subagents.runner import RunSubagentArgs, run_subagent

    agent = make_agent(ScriptedProvider([TextTurn("done")]), model="gpt-5")
    parent = await agent.session()

    result = await run_subagent(
        RunSubagentArgs(
            parent_session=parent,
            parent_agent=agent,
            definition=DEFAULT_AGENT,
            prompt="do the thing",
            display_name="helper",
            subagent_run_id="sa_leak_test",
        )
    )

    assert not result.errored
    assert await _pending_background_tasks() == []


async def test_cancelled_unretained_subagent_releases_child_session() -> None:
    from linch.subagents.default_agent import DEFAULT_AGENT
    from linch.subagents.runner import RunSubagentArgs, run_subagent

    started = asyncio.Event()

    class BlockingProvider(ScriptedProvider):
        async def stream(self, req):
            self.requests.append(req)
            yield {"type": "message_start", "model": req.model}
            started.set()
            await asyncio.Event().wait()

    agent = make_agent(BlockingProvider([]), model="gpt-5")
    parent = await agent.session()
    child_ids: list[str] = []
    task = asyncio.create_task(
        run_subagent(
            RunSubagentArgs(
                parent_session=parent,
                parent_agent=agent,
                definition=DEFAULT_AGENT,
                prompt="do the thing",
                display_name="helper",
                subagent_run_id="sa_cancel_cleanup",
                on_child_registered=child_ids.append,
            )
        )
    )

    await asyncio.wait_for(started.wait(), timeout=5.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(child_ids) == 1
    assert child_ids[0] not in agent._sessions


@pytest.mark.parametrize("explicit_options", [False, True])
async def test_direct_runner_call_inherits_parent_run_dependencies(explicit_options: bool) -> None:
    from linch import RunOptions
    from linch.subagents.default_agent import DEFAULT_AGENT
    from linch.subagents.runner import RunSubagentArgs, run_subagent
    from tests.support.tools import StubTool, tool_registry

    deps = object()
    seen: list[Any] = []
    tool = StubTool("inspect_deps", on_execute=lambda _input, ctx: seen.append(ctx.deps))
    agent = make_agent(
        ScriptedProvider(
            [ToolUseTurn("inspect_deps", {}), TextTurn("done")],
        ),
        model="gpt-5",
        tools=tool_registry(tool),
    )
    parent = await agent.session()
    parent.run_deps = deps

    result = await run_subagent(
        RunSubagentArgs(
            parent_session=parent,
            parent_agent=agent,
            definition=DEFAULT_AGENT,
            prompt="inspect",
            display_name="helper",
            subagent_run_id="sa_direct_deps",
            run_options=RunOptions(max_output_tokens=8) if explicit_options else None,
        )
    )

    assert not result.errored
    assert seen == [deps]
    await agent.close()



async def test_subagent_hooks_keep_the_child_invocation_dependencies() -> None:
    from linch import RunOptions
    from linch.subagents.default_agent import DEFAULT_AGENT
    from linch.subagents.runner import RunSubagentArgs, run_subagent

    deps_a = object()
    deps_b = object()
    parent_ref: dict[str, Any] = {}

    class MutatingProvider(ScriptedProvider):
        async def stream(self, req: Any):
            parent_ref["session"].run_deps = deps_b
            async for event in super().stream(req):
                yield event

    class Hooks:
        resume_policy_id = "test.hooks"

        def __init__(self) -> None:
            self.deps: list[Any] = []

        def on_subagent_start(self, ctx: Any) -> None:
            self.deps.append(ctx.deps)

        def on_subagent_stop(self, ctx: Any) -> None:
            self.deps.append(ctx.deps)

    hooks = Hooks()
    agent = make_agent(MutatingProvider([TextTurn("done")]), model="gpt-5", hooks=[hooks])
    parent = await agent.session()
    parent_ref["session"] = parent
    parent.run_deps = deps_a

    result = await run_subagent(
        RunSubagentArgs(
            parent_session=parent,
            parent_agent=agent,
            definition=DEFAULT_AGENT,
            prompt="do the thing",
            display_name="helper",
            subagent_run_id="sa_hook_deps",
            run_options=RunOptions(deps=deps_a),
        )
    )

    assert not result.errored
    assert hooks.deps == [deps_a, deps_a]
    await agent.close()


async def test_isolation_release_failure_preserves_child_error_and_cleanup(caplog) -> None:
    from linch.subagents.default_agent import DEFAULT_AGENT
    from linch.subagents.runner import RunSubagentArgs, run_subagent

    class FailingProvider(ScriptedProvider):
        async def stream(self, req: Any):
            self.requests.append(req)
            raise RuntimeError("child failed")
            yield  # pragma: no cover

    class FailingRelease:
        async def acquire(self) -> str:
            return "."

        async def release(self, cwd: str, *, keep: bool = False) -> None:
            raise RuntimeError("release failed")

    class Hooks:
        resume_policy_id = "test.hooks"

        def __init__(self) -> None:
            self.stops: list[Any] = []

        def on_subagent_stop(self, ctx: Any) -> None:
            self.stops.append(ctx)

    hooks = Hooks()
    agent = make_agent(FailingProvider([]), model="gpt-5", hooks=[hooks])
    parent = await agent.session()
    child_ids: list[str] = []

    result = await run_subagent(
        RunSubagentArgs(
            parent_session=parent,
            parent_agent=agent,
            definition=DEFAULT_AGENT,
            prompt="do the thing",
            display_name="helper",
            subagent_run_id="sa_release_failure",
            isolation=FailingRelease(),
            on_child_registered=child_ids.append,
        )
    )

    assert result.errored
    assert result.error == {"name": "RuntimeError", "message": "child failed"}
    assert len(hooks.stops) == 1
    assert child_ids and child_ids[0] not in agent._sessions
    assert "release failed" in caplog.text
    await agent.close()



async def test_registration_failure_logs_failed_child_cleanup(monkeypatch, caplog) -> None:
    from linch.subagents.default_agent import DEFAULT_AGENT
    from linch.subagents.runner import RunSubagentArgs, run_subagent

    agent = make_agent(ScriptedProvider([]))
    parent = await agent.session()

    def fail_registration(child_id):
        raise ValueError("registration failed")

    async def fail_cleanup(*args, **kwargs):
        raise RuntimeError("cleanup unavailable")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(agent, "release_session", fail_cleanup)
            with pytest.raises(ValueError, match="registration failed"):
                await run_subagent(
                    RunSubagentArgs(
                        parent_session=parent,
                        parent_agent=agent,
                        definition=DEFAULT_AGENT,
                        prompt="go",
                        display_name="worker",
                        subagent_run_id="register-failure",
                        on_child_registered=fail_registration,
                    )
                )
        assert "cleanup unavailable" in caplog.text
    finally:
        await agent.close()



async def test_failed_child_event_delivery_closes_retained_run() -> None:
    from linch.subagents.default_agent import DEFAULT_AGENT
    from linch.subagents.runner import RunSubagentArgs, run_subagent

    def fail_emit(event):
        raise RuntimeError("event sink failed")

    agent = make_agent(ScriptedProvider([TextTurn("child")]), run_store=None)
    parent = await agent.session()
    try:
        result = await run_subagent(
            RunSubagentArgs(
                parent_session=parent,
                parent_agent=agent,
                definition=DEFAULT_AGENT,
                prompt="go",
                display_name="worker",
                subagent_run_id="emit-failure",
                emit=fail_emit,
                retain=True,
            )
        )
        child = agent._sessions[result.child_session_id]
        assert result.errored
        assert result.error == {"name": "RuntimeError", "message": "event sink failed"}
        assert not child._active
        assert child.active_run_id is None
        assert child._active_gen is None
    finally:
        await agent.close()
