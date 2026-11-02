from __future__ import annotations

import math
import re
from types import SimpleNamespace
from typing import Any

import pytest


def test_tool_context_keeps_file_tracker_positional_and_timeout_keyword_only() -> None:
    from linch.tools.base import ToolContext

    tracker = object()
    context = ToolContext("/tmp", "session", "run", None, None, tracker)

    assert context.file_read_tracker is tracker
    assert context.tool_timeout_s is None


@pytest.mark.parametrize("value", [True, False, 0, -1, 1.5, math.nan, math.inf, "10"])
def test_bash_timeout_ms_rejects_invalid_values(value: object) -> None:
    from linch.tools.builtin import BashTool

    with pytest.raises(ValueError, match="timeout_ms"):
        BashTool().validate({"command": "echo hi", "timeout_ms": value})


@pytest.mark.parametrize("field", ["max_processes", "max_runtime_ms"])
@pytest.mark.parametrize("value", [True, False, 0, -1, 1.5, math.nan, math.inf])
def test_bash_process_limits_reject_invalid_values(field: str, value: object) -> None:
    from linch.tools.builtin import BashTool

    with pytest.raises(ValueError, match="process limits"):
        BashTool(**{field: value})


@pytest.mark.parametrize("value", [True, 0, -1, math.nan, math.inf])
@pytest.mark.asyncio
async def test_bash_rejects_invalid_context_timeout(tmp_path, value: object) -> None:
    from linch.tools.base import ToolContext
    from linch.tools.builtin import BashTool

    context = ToolContext(str(tmp_path), "session", "run", None, tool_timeout_s=value)
    tool = BashTool()

    with pytest.raises(ValueError, match="tool timeout"):
        await tool.execute(tool.validate({"command": "echo hi"}), context)


@pytest.mark.asyncio
async def test_bash_background_requires_managed_process_registry(tmp_path) -> None:
    from linch.tools.base import ToolContext
    from linch.tools.builtin import BashTool

    context = ToolContext(
        str(tmp_path), "session", "run", None, _session=SimpleNamespace(_processes=None)
    )
    tool = BashTool()

    result = await tool.execute(
        tool.validate({"command": "echo hi", "run_in_background": True}), context
    )
    assert result.is_error
    assert "managed process" in result.content.lower()


def test_process_control_allowlist_requires_builtin_identity() -> None:
    from linch.permissions import PendingToolCall, PermissionEngine
    from linch.tools.processes import BashProcessTool

    class Impostor:
        name = "BashProcess"
        scope = "exec"

        def validate(self, raw: dict[str, object]) -> dict[str, object]:
            return raw

        def summarize(self, input: dict[str, object]) -> str:
            return "impostor"

    engine = PermissionEngine(mode="default")
    impostor = engine.evaluate(PendingToolCall("call", Impostor(), {}))
    builtin = engine.evaluate(
        PendingToolCall("call", BashProcessTool(), {"action": "status", "id": "x"})
    )

    assert impostor.decision == "ask"
    assert builtin.decision == "allow"


@pytest.mark.asyncio
async def test_process_ids_are_unique_eight_hex_digits() -> None:
    from linch.tools.builtin import BashTool
    from linch.tools.execution import ExecResult
    from linch.tools.processes import ProcessRegistry

    class Handle:
        def __init__(self) -> None:
            self.running = True

        async def wait(self, timeout: float | None) -> ExecResult | None:
            if self.running:
                return None
            return ExecResult("", "", 0, False)

        async def kill(self) -> None:
            self.running = False

        def read(self) -> Any:
            return SimpleNamespace(
                stdout="", stderr="", stdout_path=None, stderr_path=None, lossy=False
            )

    class Backend:
        async def start(self, command: str, *, cwd: str) -> Handle:
            return Handle()

    tool = BashTool(execution=SimpleNamespace(shell=Backend()))
    registry = ProcessRegistry("run")
    session = SimpleNamespace(_processes=registry)
    from linch.tools.base import ToolContext

    ctx = ToolContext(".", "session", "run", None, _session=session)
    try:
        first = await registry.run(tool, {"command": "noop", "run_in_background": True}, ctx, 1)
        second = await registry.run(tool, {"command": "noop", "run_in_background": True}, ctx, 1)
        first_id = str(first.metadata["process_id"])
        second_id = str(second.metadata["process_id"])
        assert re.fullmatch(r"p[0-9a-f]{8}", first_id)
        assert re.fullmatch(r"p[0-9a-f]{8}", second_id)
        assert first_id != second_id
    finally:
        await registry.close()
