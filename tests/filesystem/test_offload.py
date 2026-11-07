from __future__ import annotations

import re
from typing import Any

import pytest

from linch.config import FeatureFlags
from linch.filesystem import OffloadConfig, StateFileBackend
from linch.filesystem.offload import maybe_offload
from linch.tools.base import ToolContext, ToolResult
from linch.tools.registry import workspace_tools
from tests.support.agents import make_agent
from tests.support.providers import ScriptedProvider, TextTurn, ToolUseTurn
from tests.support.tools import StubTool

BIG = "\n".join(f"result line {i} with some filler text" for i in range(200))


def _agent(*, result_offload, filesystem=None, turns=None, **overrides: Any):
    if turns is None:
        turns = [ToolUseTurn("big_search", {}), TextTurn("done")]
    return make_agent(
        ScriptedProvider(turns),
        model="gpt-5",
        tools=workspace_tools(  # type: ignore[arg-type]
            extra=[StubTool("big_search", output=BIG, parallel=True)]
        ),
        features=FeatureFlags(filesystem=result_offload is not None or filesystem is not None),
        filesystem=filesystem,
        result_offload=result_offload,
        **overrides,
    )


def _offload_path(session: Any) -> str:
    """Return the offload path the model was given in the provider view."""
    for message in session.provider_view:
        for block in message.content:
            if getattr(block, "type", None) == "tool_result":
                match = re.search(r"offloaded to '([^']+)'", str(block.content))
                if match:
                    return match.group(1)
    raise AssertionError("no offloaded result in the provider view")


async def test_maybe_offload_replaces_large_content() -> None:
    backend = StateFileBackend()
    result = ToolResult(content=BIG)
    out = await maybe_offload(
        result,
        tool_name="big_search",
        call_id="abc",
        backend=backend,
        config=OffloadConfig(threshold_tokens=10, preview_lines=3),
    )
    assert out.truncated
    assert "offloaded to" in out.content
    assert len(out.content) < len(BIG)
    path = out.metadata["offloaded_to"]
    assert await backend.read(path) == BIG  # full content preserved in backend


@pytest.mark.parametrize("case", ["small", "error", "filesystem"])
async def test_maybe_offload_skip_cases(case: str) -> None:
    backend = StateFileBackend()
    result = ToolResult(content="small" if case == "small" else BIG, is_error=case == "error")
    out = await maybe_offload(
        result,
        tool_name="t" if case == "small" else "big_search" if case == "error" else "read_file",
        call_id="x",
        backend=backend,
        config=OffloadConfig(threshold_tokens=10_000 if case == "small" else 1),
    )
    assert out.content == ("small" if case == "small" else BIG)
    if case == "small":
        assert not out.truncated
        assert await backend.ls() == []


async def test_offload_end_to_end() -> None:
    agent = _agent(result_offload=OffloadConfig(threshold_tokens=10, preview_lines=5))
    session = await agent.session()
    events = [e async for e in session.run("search")]

    end = next(e for e in events if e.type == "tool_call_end" and e.tool_name == "big_search")
    # The string the LLM sees is shrunk and references a file...
    assert "offloaded to" in end.result
    assert len(end.result) < len(BIG)
    # ...but the full structured result still rides along for observers.
    assert end.tool_result is not None
    assert end.tool_result.content == BIG
    assert not end.tool_result.truncated

    # Full content lives in the session store, not in the session filesystem.
    assert await session.filesystem.ls("/offload") == []
    paths = await session.store.list_offloads(session.id)
    assert len(paths) == 1
    path = paths[0]
    assert await session.store.load_offload(session.id, path) == BIG

    # And the persisted provider_view never contains the full payload.
    persisted = "".join(
        block.content
        for msg in session.provider_view
        for block in msg.content
        if getattr(block, "type", None) == "tool_result"
    )
    assert BIG not in persisted


async def test_no_offload_when_disabled() -> None:
    agent = _agent(result_offload=None)
    session = await agent.session()
    assert session.filesystem is None
    events = [e async for e in session.run("search")]
    end = next(e for e in events if e.type == "tool_call_end" and e.tool_name == "big_search")
    assert end.result == BIG  # untouched


async def test_scoped_run_offloads_to_the_session_and_reads_back_through_deps() -> None:
    from linch import RunOptions, ScopedFileBackend
    from linch.filesystem.tools import ReadFileTool

    shared = StateFileBackend()
    scoped = ScopedFileBackend(shared, "/tenants/alice")
    agent = _agent(
        result_offload=OffloadConfig(threshold_tokens=10),
        filesystem=shared,
    )
    try:
        session = await agent.session()
        events = [
            event async for event in session.run("search", RunOptions(deps={"filesystem": scoped}))
        ]
        assert events[-1].subtype == "success"
        # Neither the explicit nor the run-scoped backend holds the offload.
        assert await scoped.ls("/offload") == []
        assert await shared.ls("/offload") == []
        paths = await session.store.list_offloads(session.id)
        assert len(paths) == 1
        result = await ReadFileTool().execute(
            {"path": paths[0]},
            ToolContext(
                cwd=".",
                session_id=session.id,
                run_id="read-back",
                session_store=session.store,
                filesystem=shared,
                deps={"filesystem": scoped},
                _session=session,
            ),
        )
        assert result.content == BIG
    finally:
        await agent.close()


async def test_offload_with_an_execution_backend_stays_out_of_the_workspace(tmp_path) -> None:
    from linch import LocalExecutionBackend

    turns = [
        ToolUseTurn("big_search", {}, tool_id="search_1"),
        ToolUseTurn("read_file", {"path": "/offload/big_search_search_1.txt"}, tool_id="read_1"),
        TextTurn("done"),
    ]
    agent = _agent(
        result_offload=OffloadConfig(threshold_tokens=10),
        turns=turns,
        cwd=str(tmp_path),
        execution_backend=LocalExecutionBackend(cwd=tmp_path),
    )
    try:
        session = await agent.session()
        events = [event async for event in session.run("search")]

        assert _offload_path(session) == "/offload/big_search_search_1.txt"
        assert not (tmp_path / "offload").exists()
        assert list(tmp_path.iterdir()) == []
        read = next(e for e in events if e.type == "tool_call_end" and e.tool_name == "read_file")
        assert read.result == BIG
    finally:
        await agent.close()


@pytest.mark.parametrize("store_case", ["memory", "postgres"])
async def test_offload_survives_resume_in_a_fresh_agent(store_case: str, pg_dsn: str) -> None:
    from linch.sessions import InMemorySessionStore

    def store() -> Any:
        if store_case == "memory":
            return shared_memory
        from linch.sessions.postgres import PostgresSessionStore

        return PostgresSessionStore(pg_dsn)

    shared_memory = InMemorySessionStore()
    config = OffloadConfig(threshold_tokens=10)
    first_store = store()
    first = _agent(result_offload=config, session_store=first_store)
    try:
        session = await first.session()
        events = [event async for event in session.run("search")]
        assert events[-1].subtype == "success"
        session_id = session.id
        path = _offload_path(session)
    finally:
        await first.close()
        await first_store.close()

    # A worker in another process: new agent, new store objects, same durable data.
    second_store = store()
    turns = [ToolUseTurn("read_file", {"path": path}, tool_id="read_1"), TextTurn("ok")]
    second = _agent(result_offload=config, session_store=second_store, turns=turns)
    try:
        resumed = await second.session(id=session_id)
        assert _offload_path(resumed) == path
        events = [event async for event in resumed.run("read it back")]
        read = next(e for e in events if e.type == "tool_call_end" and e.tool_name == "read_file")
        assert read.result == BIG
    finally:
        await second.close()
        await second_store.close()


async def test_subagent_offloads_belong_to_the_root_session() -> None:
    from linch.subagents.default_agent import DEFAULT_AGENT
    from linch.subagents.runner import RunSubagentArgs, run_subagent

    turns = [ToolUseTurn("big_search", {}, tool_id="child_search"), TextTurn("done")]
    agent = _agent(result_offload=OffloadConfig(threshold_tokens=10), turns=turns)
    try:
        parent = await agent.session()
        result = await run_subagent(
            RunSubagentArgs(
                parent_session=parent,
                parent_agent=agent,
                definition=DEFAULT_AGENT,
                prompt="search",
                display_name="w",
                subagent_run_id="sa_offload",
            )
        )

        assert not result.errored
        path = "/offload/big_search_child_search.txt"
        assert await parent.store.list_offloads(parent.id) == [path]
        assert await parent.store.list_offloads(result.child_session_id) == []
    finally:
        await agent.close()


async def test_a_store_without_offload_support_keeps_results_inline() -> None:
    from linch.sessions import InMemorySessionStore

    class NoOffloadStore(InMemorySessionStore):
        save_offload = None
        load_offload = None
        list_offloads = None

    agent = _agent(
        result_offload=OffloadConfig(threshold_tokens=10), session_store=NoOffloadStore()
    )
    try:
        session = await agent.session()
        events = [event async for event in session.run("search")]
        end = next(e for e in events if e.type == "tool_call_end" and e.tool_name == "big_search")
        assert end.result == BIG
        assert await session.filesystem.ls() == []
    finally:
        await agent.close()
