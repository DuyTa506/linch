from __future__ import annotations

import asyncio
import builtins
import importlib
import sys
from types import SimpleNamespace

import pytest

from linch.abort import AbortContext
from linch.events import (
    ResultEvent,
    ToolCallEndEvent,
    event_from_dict,
    event_to_dict,
)
from linch.permissions import PendingToolCall, PermissionEngine
from linch.scheduler import execute_tool_calls
from linch.tools import Citation, ToolContext, ToolRegistry, ToolResult
from linch.tools.builtin import GlobTool, GrepTool, WriteTool
from linch.types import ToolUseBlock, Usage


class SleepTool:
    name = "SleepTool"
    description = "Sleep for scheduler duration testing."
    input_schema = {"type": "object", "properties": {}}
    scope = "write"
    parallel_safe = False

    def validate(self, raw: dict[str, object]) -> dict[str, object]:
        return {}

    def summarize(self, input: dict[str, object]) -> str:
        return "sleep"

    async def execute(self, input: dict[str, object], ctx: ToolContext) -> ToolResult:
        await asyncio.sleep(0.01)
        return ToolResult(content="ok", summary="ok")


@pytest.mark.asyncio
async def test_permission_callback_allows_sync_and_async() -> None:
    class DummyTool:
        name = "WriteDummy"
        scope = "write"
        parallel_safe = False

        def validate(self, raw: dict[str, object]) -> dict[str, object]:
            return raw

        def summarize(self, input: dict[str, object]) -> str:
            return "dummy"

    call = PendingToolCall(tool_use_id="t1", tool=DummyTool(), input={})
    signal = AbortContext()

    sync_engine = PermissionEngine(
        mode="default",
        can_use_tool=lambda _req: {"behavior": "allow"},
    )
    sync_decision = await sync_engine.resolve(call, signal)
    assert sync_decision.decision == "allow"

    async def allow_async(_req: object) -> dict[str, str]:
        await asyncio.sleep(0)
        return {"behavior": "allow"}

    async_engine = PermissionEngine(mode="default", can_use_tool=allow_async)
    async_decision = await async_engine.resolve(call, signal)
    assert async_decision.decision == "allow"


@pytest.mark.asyncio
async def test_scheduler_emits_non_zero_duration() -> None:
    registry = ToolRegistry()
    registry.register(SleepTool())
    agent = SimpleNamespace(
        cwd=".",
        tools=registry,
        permission_engine=PermissionEngine(mode="skip-dangerous"),
        tool_concurrency=2,
    )
    session = SimpleNamespace(
        id="s1",
        store=None,
        active_run_id="run-1",
        tools_override=None,
        current_turn_allowed_tools=None,
    )
    events = [
        event
        async for event in execute_tool_calls(
            [ToolUseBlock(id="call-1", name="SleepTool", input={})],
            agent,
            session,
            AbortContext(),
        )
    ]
    end_events = [e for e in events if isinstance(e, ToolCallEndEvent)]
    assert len(end_events) == 1
    assert end_events[0].duration_ms > 0


@pytest.mark.asyncio
async def test_write_tool_allows_empty_content(tmp_path) -> None:
    tool = WriteTool()
    validated = tool.validate({"file_path": "empty.txt", "content": ""})
    ctx = ToolContext(
        cwd=str(tmp_path),
        session_id="s",
        run_id="r",
        session_store=None,
    )
    result = await tool.execute(validated, ctx)
    assert result.is_error is False
    assert (tmp_path / "empty.txt").read_text(encoding="utf-8") == ""


@pytest.mark.asyncio
async def test_grep_and_glob_are_confined_to_cwd(tmp_path) -> None:
    ctx = ToolContext(
        cwd=str(tmp_path),
        session_id="s",
        run_id="r",
        session_store=None,
    )
    grep = await GrepTool().execute({"pattern": "x", "path": "/tmp"}, ctx)
    glob = await GlobTool().execute({"glob_pattern": "*.py", "target_directory": "/tmp"}, ctx)
    assert grep.is_error is True
    assert glob.is_error is True
    assert "escapes cwd" in grep.content
    assert "escapes cwd" in glob.content


def test_event_round_trip() -> None:
    event = ResultEvent(
        subtype="success",
        stop_reason="end_turn",
        total_usage=Usage(input_tokens=10, output_tokens=4),
        duration_ms=123,
        final_text="done",
    )
    raw = event_to_dict(event)
    rebuilt = event_from_dict(raw)
    assert isinstance(rebuilt, ResultEvent)
    assert rebuilt.subtype == "success"
    assert rebuilt.duration_ms == 123
    assert rebuilt.final_text == "done"
    assert rebuilt.total_usage.input_tokens == 10


def test_tool_call_end_event_round_trips_structured_tool_result() -> None:
    event = ToolCallEndEvent(
        tool_use_id="call-1",
        tool_name="Search",
        result="legacy text",
        is_error=False,
        duration_ms=12,
        tool_result=ToolResult(
            content="legacy text",
            summary="Search result",
            metadata={"rank": 1, "nested": {"ok": True}},
            citations=[
                Citation(
                    id="c1",
                    source="doc://1",
                    label="Doc",
                    chunk="chunk",
                    score=0.5,
                    metadata={"page": 2},
                )
            ],
            attachments=[object()],
            duration_ms=12,
            truncated=True,
        ),
    )

    raw = event_to_dict(event)
    assert "tool_result" in raw
    assert "attachments" not in raw["tool_result"]
    rebuilt = event_from_dict(raw)

    assert isinstance(rebuilt, ToolCallEndEvent)
    assert rebuilt.result == "legacy text"
    assert rebuilt.tool_result is not None
    assert rebuilt.tool_result.summary == "Search result"
    assert rebuilt.tool_result.metadata["nested"] == {"ok": True}
    assert rebuilt.tool_result.citations[0].source == "doc://1"
    assert rebuilt.tool_result.citations[0].metadata == {"page": 2}
    assert rebuilt.tool_result.truncated is True


def test_old_tool_call_end_event_dict_remains_supported() -> None:
    rebuilt = event_from_dict(
        {
            "type": "tool_call_end",
            "tool_use_id": "call-1",
            "tool_name": "OldTool",
            "result": "old result",
            "is_error": False,
            "duration_ms": 3,
        }
    )

    assert isinstance(rebuilt, ToolCallEndEvent)
    assert rebuilt.result == "old result"
    assert rebuilt.tool_result is None


def test_import_linch_without_mcp_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    original_import = builtins.__import__

    def blocked_import(
        name: str,
        globals: dict[str, object] | None = None,
        locals: dict[str, object] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> object:
        if level == 0 and (name == "mcp" or name.startswith("mcp.")):
            raise ModuleNotFoundError("No module named 'mcp'")
        return original_import(name, globals, locals, fromlist, level)

    for key in list(sys.modules):
        if key == "mcp" or key.startswith("mcp.") or key.startswith("linch"):
            sys.modules.pop(key, None)

    monkeypatch.setattr(builtins, "__import__", blocked_import)
    mod = importlib.import_module("linch")
    assert hasattr(mod, "connect_mcp_servers")
