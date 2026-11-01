from __future__ import annotations

from typing import Any

import pytest

from linch.scheduler import ResolvedCall, _partition_batches, _tool_parallel
from linch.types import ToolUseBlock


class _ModalTool:
    """An exec-scoped tool that is parallel-safe only for read-mode inputs."""

    name = "Modal"
    scope = "exec"

    def parallel(self, input: dict[str, Any]) -> bool:
        return input.get("mode") == "read"

    def summarize(self, input: dict[str, Any]) -> str:
        return "Modal()"


class _RaisingTool:
    name = "Boom"
    scope = "read"

    def parallel(self, input: dict[str, Any]) -> bool:
        raise RuntimeError("predicate blew up")


def _call(tool: Any, mode: str, idx: int) -> ResolvedCall:
    inp = {"mode": mode}
    return ResolvedCall(
        id=f"t{idx}",
        block=ToolUseBlock(id=f"t{idx}", name=tool.name, input=inp),
        tool=tool,
        input=inp,
        summary="",
        is_immediate_error=False,
    )


@pytest.mark.parametrize(
    ("tool", "mode", "expected"),
    [(_ModalTool(), "read", True), (_ModalTool(), "write", False), (_RaisingTool(), "read", False)],
)
def test_callable_parallel(tool: Any, mode: str, expected: bool) -> None:
    assert _tool_parallel(_call(tool, mode, 0)) is expected


def test_partition_groups_read_inputs_and_serializes_writes_in_order() -> None:
    from linch.permissions.engine import PermissionDecision

    tool = _ModalTool()
    resolved = [
        _call(tool, "read", 0),
        _call(tool, "read", 1),
        _call(tool, "write", 2),
        _call(tool, "read", 3),
    ]
    decisions = [PermissionDecision(decision="allow") for _ in resolved]
    batches = _partition_batches(resolved, decisions, max_concurrency=8)

    shapes = [(batch["parallel"], [idx for _, idx, _ in batch["calls"]]) for batch in batches]
    assert shapes == [(True, [0, 1]), (False, [2]), (False, [3])]
