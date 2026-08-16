"""Durable-run identity coverage for the tools pipeline."""

from __future__ import annotations

from typing import Any, cast

import pytest


class _Provider:
    id = "tool-pipeline-contract-provider"

    def context_window(self, model: str) -> int:
        return 128_000

    async def stream(self, req: Any):
        from linch.types import Usage

        yield {"type": "message_start", "model": req.model}
        yield {"type": "text_delta", "text": "done"}
        yield {"type": "message_end", "stop_reason": "end_turn", "usage": Usage()}


def _agent(run_store: Any, session_store: Any, pipeline: Any) -> Any:
    from linch import Agent
    from linch.config import FeatureFlags
    from linch.tools.registry import empty_tools

    return Agent(
        model="test-model",
        provider=cast(Any, _Provider()),
        tools=empty_tools(),
        permissions={"mode": "skip-dangerous"},
        session_store=session_store,
        run_store=run_store,
        features=FeatureFlags(),
        result_offload=None,
        tool_pipeline=pipeline,
    )


async def _interrupt_after_first_event(session: Any) -> str:
    iterator = session.run("go").__aiter__()
    event = await iterator.__anext__()
    assert event.type == "system"
    await iterator.aclose()
    return event.run_id


async def test_anonymous_pipeline_listener_is_rejected_for_durable_run() -> None:
    from linch.errors import ConfigError
    from linch.run_store import InMemoryRunStore
    from linch.sessions import InMemorySessionStore
    from linch.tools.pipeline import ToolExecution, ToolPipeline

    pipeline = ToolPipeline()

    async def anonymous(execution: ToolExecution, next) -> object:
        return await next()

    pipeline.on_execute(anonymous)
    agent = _agent(InMemoryRunStore(), InMemorySessionStore(), pipeline)

    with pytest.raises(ConfigError, match=r"tools/execute.*resume_policy_id"):
        _ = [event async for event in (await agent.session()).run("go")]


async def test_pipeline_timeout_configuration_change_denies_resume() -> None:
    from linch.errors import ConfigError
    from linch.run_store import InMemoryRunStore
    from linch.sessions import InMemorySessionStore
    from linch.tools.pipeline import ToolPipeline
    from linch.tools.wrappers import timeout_wrapper

    run_store = InMemoryRunStore()
    session_store = InMemorySessionStore()
    first_pipeline = ToolPipeline()
    first_pipeline.on_execute(timeout_wrapper(1))
    first = _agent(run_store, session_store, first_pipeline)
    run_id = await _interrupt_after_first_event(await first.session(id="s1"))

    changed_pipeline = ToolPipeline()
    changed_pipeline.on_execute(timeout_wrapper(2))
    changed = _agent(run_store, session_store, changed_pipeline)

    with pytest.raises(ConfigError, match=r"tool_pipeline.*seconds"):
        _ = [event async for event in (await changed.session(id="s1")).resume(run_id)]
