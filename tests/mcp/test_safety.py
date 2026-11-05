from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("mcp", reason="the 'mcp' extra is not installed")


async def _unused_call(name: str, args: dict, signal: object) -> object:
    raise AssertionError("call not expected")


@pytest.mark.parametrize(
    ("trust_annotations", "annotations", "scope", "parallel", "destructive"),
    [
        (False, None, "exec", False, False),
        (False, {"readOnlyHint": True, "destructiveHint": True}, "exec", False, False),
        (True, None, "exec", False, False),
        (True, {"readOnlyHint": False}, "exec", False, False),
        (True, {"destructiveHint": True}, "exec", False, True),
        (True, {"destructiveHint": False}, "exec", False, False),
        (True, {"readOnlyHint": True}, "read", True, False),
        (True, {"readOnlyHint": True, "destructiveHint": True}, "read", True, False),
    ],
)
def test_annotations_require_host_trust(
    trust_annotations: bool,
    annotations,
    scope: str,
    parallel: bool,
    destructive: bool,
) -> None:
    from mcp.types import Tool, ToolAnnotations

    from linch.mcp.tool import make_mcp_tool

    tool: Any = make_mcp_tool(
        "srv",
        Tool(
            name="shell_exec",
            inputSchema={},
            annotations=None if annotations is None else ToolAnnotations(**annotations),
        ),
        _unused_call,
        trust_annotations=trust_annotations,
    )
    assert (tool.scope, tool.parallel, tool.destructive) == (scope, parallel, destructive)


@pytest.mark.parametrize(
    ("trust_annotations", "annotations", "decision"),
    [
        (False, None, "allow"),
        (False, {"destructiveHint": True}, "allow"),
        (True, {"destructiveHint": True}, "ask"),
    ],
)
def test_skip_dangerous_uses_host_annotation_decision(
    trust_annotations: bool, annotations, decision: str
) -> None:
    from mcp.types import Tool, ToolAnnotations

    from linch.mcp import mcp_permission_rules
    from linch.mcp.tool import make_mcp_tool
    from linch.permissions.engine import PendingToolCall, PermissionEngine

    tool: Any = make_mcp_tool(
        "srv",
        Tool(
            name="shell_exec",
            inputSchema={},
            annotations=None if annotations is None else ToolAnnotations(**annotations),
        ),
        _unused_call,
        trust_annotations=trust_annotations,
    )
    engine = PermissionEngine(mode="skip-dangerous", rules=mcp_permission_rules([tool]))
    assert (
        engine.evaluate(PendingToolCall(tool_use_id="t", tool=tool, input={})).decision == decision
    )


def test_accept_edits_asks_for_unannotated_mcp_exec_tool() -> None:
    from mcp.types import Tool

    from linch.mcp import mcp_permission_rules
    from linch.mcp.tool import make_mcp_tool
    from linch.permissions.engine import PendingToolCall, PermissionEngine

    tool: Any = make_mcp_tool("srv", Tool(name="shell_exec", inputSchema={}), _unused_call)
    engine = PermissionEngine(mode="acceptEdits", rules=mcp_permission_rules([tool]))
    assert engine.evaluate(PendingToolCall(tool_use_id="t", tool=tool, input={})).decision == "ask"


async def test_large_mcp_text_spills_under_call_context(tmp_path) -> None:
    from mcp.types import CallToolResult, TextContent, Tool

    from linch.mcp.tool import make_mcp_tool
    from linch.tools.base import ToolContext

    async def call(name: str, args: dict, signal: object) -> object:
        return CallToolResult(content=[TextContent(type="text", text="x" * 30001)])

    tool: Any = make_mcp_tool("srv", Tool(name="dump", inputSchema={}), call)
    result = await tool.execute(
        {}, ToolContext(cwd=str(tmp_path), session_id="s", run_id="r", session_store=None)
    )
    path = result.metadata["output_path"]
    assert result.truncated is True
    assert result.content.endswith(f"[output_path: {path}]")
    assert Path(path).read_bytes() == b"x" * 30001


async def test_mcp_text_over_spill_ceiling_skips_spill_work(tmp_path, monkeypatch) -> None:
    from mcp.types import CallToolResult, TextContent

    from linch.mcp import result as module
    from linch.tools._limits import OUTPUT_CAP
    from linch.tools._output import SPILL_LIMIT

    def unexpected(*args: object) -> None:
        raise AssertionError("spill must not start above its ceiling")

    monkeypatch.setattr(module, "BoundedOutput", unexpected)
    mapped = await module.map_mcp_result(
        CallToolResult(content=[TextContent(type="text", text="x" * (SPILL_LIMIT + 1))]),
        cwd=str(tmp_path),
    )
    assert len(mapped.content) < OUTPUT_CAP + 100


async def test_large_mcp_text_falls_back_to_cap_when_spill_fails(tmp_path) -> None:
    from mcp.types import CallToolResult, TextContent, Tool

    from linch.mcp.tool import make_mcp_tool
    from linch.tools.base import ToolContext

    (tmp_path / ".linch").write_text("occupied")

    async def call(name: str, args: dict, signal: object) -> object:
        return CallToolResult(content=[TextContent(type="text", text="x" * 30001)])

    tool: Any = make_mcp_tool("srv", Tool(name="dump", inputSchema={}), call)
    result = await tool.execute(
        {}, ToolContext(cwd=str(tmp_path), session_id="s", run_id="r", session_store=None)
    )
    assert result.metadata == {}
    assert result.content == "x" * 30000 + "\n… (output capped at 30000 chars)"


@pytest.mark.parametrize("size", [30000, 30001])
async def test_result_text_is_bounded_at_thirty_thousand_characters(size: int) -> None:
    from mcp.types import CallToolResult, TextContent

    from linch.mcp.result import map_mcp_result

    result = await map_mcp_result(
        CallToolResult(content=[TextContent(type="text", text="x" * size)], isError=True)
    )
    marker = "" if size == 30000 else "\n… (output capped at 30000 chars)"
    assert result.content == "x" * 30000 + marker
    assert result.is_error is True


async def test_result_cap_includes_combined_blocks_and_separators() -> None:
    from mcp.types import CallToolResult, TextContent

    from linch.mcp.result import map_mcp_result

    result = await map_mcp_result(
        CallToolResult(
            content=[
                TextContent(type="text", text="a" * 15000),
                TextContent(type="text", text="b" * 20000),
            ]
        )
    )
    assert result.content == "a" * 15000 + "\n" + "b" * 14999 + "\n… (output capped at 30000 chars)"


@pytest.mark.parametrize("binary", [False, True])
async def test_embedded_resources_expose_text_but_omit_binary(binary: bool) -> None:
    from mcp.types import (
        BlobResourceContents,
        CallToolResult,
        EmbeddedResource,
        TextResourceContents,
    )

    from linch.mcp.result import map_mcp_result

    resource = (
        BlobResourceContents(uri="file:///data", blob="Zm9v")
        if binary
        else TextResourceContents(uri="file:///data", text="resource text")
    )
    result = await map_mcp_result(
        CallToolResult(content=[EmbeddedResource(type="resource", resource=resource)])
    )
    assert result.content == ("[resource content omitted]" if binary else "resource text")


async def test_embedded_resource_text_shares_result_cap() -> None:
    from mcp.types import CallToolResult, EmbeddedResource, TextContent, TextResourceContents

    from linch.mcp.result import map_mcp_result

    result = await map_mcp_result(
        CallToolResult(
            content=[
                TextContent(type="text", text="prefix"),
                EmbeddedResource(
                    type="resource",
                    resource=TextResourceContents(uri="file:///data", text="x" * 40000),
                ),
            ]
        )
    )
    assert result.content == "prefix\n" + "x" * 29993 + "\n… (output capped at 30000 chars)"


class _TrackedSignal:
    def __init__(self) -> None:
        from linch.abort import AbortContext

        self.inner = AbortContext()
        self.wait_started = asyncio.Event()
        self.wait_finished = asyncio.Event()

    def throw_if_aborted(self) -> None:
        self.inner.throw_if_aborted()

    def abort(self) -> None:
        self.inner.abort()

    async def wait(self) -> None:
        self.wait_started.set()
        try:
            await self.inner.wait()
        finally:
            self.wait_finished.set()


class _CallSession:
    def __init__(self, *, outcome: str = "hung", signal=None) -> None:
        self.started = asyncio.Event()
        self.finished = asyncio.Event()
        self.outcome = outcome
        self.signal = signal

    async def call_tool(self, name: str, args: dict, **kwargs) -> str:
        self.started.set()
        try:
            if self.outcome == "error":
                raise RuntimeError("call failed")
            if self.outcome == "result":
                return "done"
            if self.outcome == "simultaneous":
                self.signal.abort()
                return "done"
            await asyncio.Event().wait()
            raise AssertionError("hung call resumed")
        finally:
            self.finished.set()


async def test_abort_during_hung_call_cancels_and_drains_owned_tasks() -> None:
    from linch.errors import AbortError
    from linch.mcp.client import _make_call_tool

    signal = _TrackedSignal()
    session = _CallSession()
    task = asyncio.create_task(_make_call_tool(session)("hang", {}, signal))
    await session.started.wait()
    signal.abort()
    try:
        with pytest.raises(AbortError):
            await asyncio.wait_for(task, timeout=0.5)
        assert session.finished.is_set()
        assert signal.wait_finished.is_set()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("outcome", ["result", "error", "cancel"])
async def test_call_completion_drains_abort_waiter(outcome: str) -> None:
    from linch.mcp.client import _make_call_tool

    signal = _TrackedSignal()
    session = _CallSession(outcome=outcome)
    task = asyncio.create_task(_make_call_tool(session)("call", {}, signal))
    if outcome == "cancel":
        await session.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    elif outcome == "error":
        with pytest.raises(RuntimeError, match="call failed"):
            await task
    else:
        assert await task == "done"
    assert session.finished.is_set()
    assert signal.wait_started.is_set()
    assert signal.wait_finished.is_set()


async def test_observed_abort_wins_over_simultaneous_result() -> None:
    from linch.errors import AbortError
    from linch.mcp.client import _make_call_tool

    signal = _TrackedSignal()
    session = _CallSession(outcome="simultaneous", signal=signal)
    with pytest.raises(AbortError):
        await _make_call_tool(session)("call", {}, signal)
    assert signal.wait_finished.is_set()


async def test_preaborted_signal_never_starts_call() -> None:
    from linch.errors import AbortError
    from linch.mcp.client import _make_call_tool

    signal = _TrackedSignal()
    signal.abort()
    session = _CallSession()
    with pytest.raises(AbortError):
        await _make_call_tool(session)("call", {}, signal)
    assert not session.started.is_set()
    assert not signal.wait_started.is_set()


@pytest.mark.parametrize("abandon", ["abort", "timeout"])
async def test_real_sdk_sends_cancellation_for_abandoned_request(abandon: str) -> None:
    import anyio
    from mcp import ClientSession
    from mcp.types import Tool

    from linch.abort import AbortContext
    from linch.errors import AbortError
    from linch.mcp.client import _make_call_tool
    from linch.mcp.tool import make_mcp_tool
    from linch.tools.base import ToolContext

    to_client, client_read = anyio.create_memory_object_stream(10)
    client_write, from_client = anyio.create_memory_object_stream(10)
    signal = AbortContext()
    async with to_client, client_read, client_write, from_client:
        async with ClientSession(client_read, client_write) as session:
            callback = (
                _make_call_tool(session)
                if abandon == "abort"
                else _make_call_tool(session, timeout_s=0.01)
            )
            tool: Any = make_mcp_tool("srv", Tool(name="hang", inputSchema={}), callback)
            task = asyncio.create_task(
                tool.execute(
                    {},
                    ToolContext(
                        cwd=".",
                        session_id="s",
                        run_id="r",
                        session_store=None,
                        signal=signal if abandon == "abort" else None,
                    ),
                )
            )
            try:
                request = (await asyncio.wait_for(from_client.receive(), 1)).message
                assert request.method == "tools/call"
                if abandon == "abort":
                    signal.abort()
                    with pytest.raises(AbortError):
                        await asyncio.wait_for(task, 1)
                else:
                    result = await asyncio.wait_for(task, 1)
                    assert result.is_error is True
                    assert "timed out" in result.content
                notification = (await asyncio.wait_for(from_client.receive(), 1)).message
                assert notification.method == "notifications/cancelled"
                assert notification.params["requestId"] == request.id
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


async def test_repeated_caller_cancellation_does_not_interrupt_call_cleanup() -> None:
    from linch.mcp.client import _make_call_tool

    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    cleanup_finished = asyncio.Event()

    class SlowCleanupSession(_CallSession):
        async def call_tool(self, name: str, args: dict, **kwargs) -> str:
            try:
                return await super().call_tool(name, args, **kwargs)
            finally:
                cleanup_started.set()
                await release_cleanup.wait()
                cleanup_finished.set()

    session = SlowCleanupSession()
    signal = _TrackedSignal()
    task = asyncio.create_task(_make_call_tool(session)("hang", {}, signal))
    await session.started.wait()
    task.cancel()
    await asyncio.wait_for(cleanup_started.wait(), 1)
    task.cancel()
    release_cleanup.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert cleanup_finished.is_set()
    assert signal.wait_finished.is_set()


async def test_spilled_result_is_not_offloaded_again(tmp_path) -> None:
    from mcp.types import CallToolResult, TextContent

    from linch.filesystem import OffloadConfig, StateFileBackend
    from linch.filesystem.offload import maybe_offload
    from linch.mcp.result import map_mcp_result

    result = await map_mcp_result(
        CallToolResult(content=[TextContent(type="text", text="x" * 40000)]), cwd=str(tmp_path)
    )
    content = result.content
    await maybe_offload(
        result,
        tool_name="mcp__srv__dump",
        call_id="c",
        backend=StateFileBackend(),
        config=OffloadConfig(threshold_tokens=1),
    )
    assert result.content == content


async def test_non_ascii_result_below_character_cap_does_not_spill(tmp_path):
    from mcp.types import CallToolResult, TextContent

    from linch.mcp.result import map_mcp_result

    text = "😀" * 10000
    result = await map_mcp_result(
        CallToolResult(content=[TextContent(type="text", text=text)]), cwd=str(tmp_path)
    )
    assert not result.truncated and result.content == text and not result.metadata
    assert not (tmp_path / ".linch").exists()


async def test_cancelled_spill_closes_and_removes_unreturned_file(tmp_path, monkeypatch):
    import threading

    from mcp.types import CallToolResult, TextContent

    from linch.mcp.result import map_mcp_result
    from linch.tools._output import BoundedOutput

    entered, release = threading.Event(), threading.Event()
    original = BoundedOutput._spill
    captured = []

    def blocked(self, *args):
        original(self, *args)
        captured.append((self, self._file, self.path))
        entered.set()
        release.wait()

    monkeypatch.setattr(BoundedOutput, "_spill", blocked)
    task = asyncio.create_task(
        map_mcp_result(
            CallToolResult(content=[TextContent(type="text", text="x" * 30001)]), cwd=str(tmp_path)
        )
    )
    try:
        while not entered.is_set():
            await asyncio.sleep(0)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        _, file, path = captured[0]
        assert file.closed and not Path(path).exists()
    finally:
        release.set()
