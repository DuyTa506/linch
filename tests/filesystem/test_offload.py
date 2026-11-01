from __future__ import annotations

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


def _agent(*, result_offload, filesystem=None):
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
    )


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

    # Full content lives in the session filesystem.
    paths = await session.filesystem.ls("/offload")
    assert len(paths) == 1
    path = paths[0]
    assert await session.filesystem.read(path) == BIG

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


async def test_scoped_run_offloads_and_reads_back_through_same_backend() -> None:
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
        paths = await scoped.ls("/offload")
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
            ),
        )
        assert result.content == BIG
        assert await shared.ls("/offload") == []
    finally:
        await agent.close()
