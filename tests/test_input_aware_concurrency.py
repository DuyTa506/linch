"""Input-aware concurrency seam (ROADMAP Phase 4.4).

A tool may expose ``parallel`` as a *callable* ``parallel(input) -> bool`` so it
decides concurrency-safety per call — a generic seam any tool can use, not just
read-scoped ones. Ordering and resource-conflict safety stay with
``_partition_batches``.

Verify: a tool returning ``parallel(input)=True`` for read-only inputs runs those
concurrently while mutating inputs serialize; ordering preserved.
"""

from __future__ import annotations

from typing import Any

from linch.scheduler import ResolvedCall, _partition_batches, _tool_parallel
from linch.types import ToolUseBlock


class _ModalTool:
    """An exec-scoped tool that is parallel-safe only for read-mode inputs."""

    name = "Modal"
    scope = "exec"  # deliberately not "read": proves the seam is tool-driven

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


def test_callable_parallel_decides_per_input() -> None:
    tool = _ModalTool()
    assert _tool_parallel(_call(tool, "read", 0)) is True
    # A mutating input serializes even though the same tool can be parallel.
    assert _tool_parallel(_call(tool, "write", 1)) is False


def test_callable_parallel_fails_closed_on_error() -> None:
    assert _tool_parallel(_call(_RaisingTool(), "read", 0)) is False


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

    # Shape: [parallel(read0, read1)] [serial(write2)] [serial(read3)].
    shapes = [(batch["parallel"], [idx for _, idx, _ in batch["calls"]]) for batch in batches]
    assert shapes == [(True, [0, 1]), (False, [2]), (False, [3])]
