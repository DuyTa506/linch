"""Durable-run identity coverage for the tools pipeline."""

from __future__ import annotations

from typing import Any

import pytest

from tests.support.agents import make_agent
from tests.support.providers import ScriptedProvider, TextTurn


def _agent(run_store: Any, session_store: Any, pipeline: Any) -> Any:
    return make_agent(
        ScriptedProvider([TextTurn("done")]),
        session_store=session_store,
        run_store=run_store,
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
