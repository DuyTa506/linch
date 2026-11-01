from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from linch import Agent
from linch.abort import AbortContext
from linch.errors import ConfigError
from linch.events import ToolCallEndEvent, ToolCallStartEvent
from linch.permissions import PermissionEngine
from linch.permissions.engine import PermissionDecision
from linch.scheduler import ResolvedCall, _partition_batches, execute_tool_calls
from linch.tools import ResourceAccess, ToolContext, ToolRegistry, ToolResult
from linch.types import ToolUseBlock
from tests.support.providers import ScriptedProvider


class _Conflicting:
    """Parallel-safe tool that writes a named resource, so same-``res`` calls
    conflict while distinct-``res`` calls may run together."""

    scope = "read"
    input_schema = {"type": "object", "properties": {}}

    def __init__(self, name: str = "Conf") -> None:
        self.name = name
        self.description = "d"

    def parallel(self, input: dict[str, Any]) -> bool:
        return True

    def validate(self, raw: dict[str, Any]) -> dict[str, Any]:
        return raw

    def summarize(self, input: dict[str, Any]) -> str:
        return self.name

    def resources(self, input: dict[str, Any]) -> list[ResourceAccess]:
        res = input.get("res")
        return [ResourceAccess(resource=res, mode="write")] if res else []

    async def execute(self, input: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return ToolResult(content=str(input.get("res")))


class _SerialTool:
    scope = "read"
    input_schema = {"type": "object", "properties": {}}

    def __init__(self, name: str = "Ser") -> None:
        self.name = name
        self.description = "d"

    def parallel(self, input: dict[str, Any]) -> bool:
        return False

    def validate(self, raw: dict[str, Any]) -> dict[str, Any]:
        return raw

    def summarize(self, input: dict[str, Any]) -> str:
        return self.name

    def resources(self, input: dict[str, Any]) -> list[ResourceAccess]:
        return []

    async def execute(self, input: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return ToolResult(content=self.name)


def _call(tool: Any, idx: int, res: str | None) -> ResolvedCall:
    inp = {"res": res} if res is not None else {}
    return ResolvedCall(
        id=f"t{idx}",
        block=ToolUseBlock(id=f"t{idx}", name=tool.name, input=inp),
        tool=tool,
        input=inp,
        summary="",
        is_immediate_error=False,
    )


def _shapes(batches: list[dict[str, Any]]) -> list[tuple[bool, list[int]]]:
    return [(b["parallel"], [idx for _, idx, _ in b["calls"]]) for b in batches]


def _allow(n: int) -> list[PermissionDecision]:
    return [PermissionDecision(decision="allow") for _ in range(n)]


def test_greedy_default_stops_at_first_conflict() -> None:
    tool = _Conflicting()
    resolved = [_call(tool, 0, "A"), _call(tool, 1, "A"), _call(tool, 2, "B")]
    batches = _partition_batches(resolved, _allow(3), max_concurrency=8)
    # Greedy takes the contiguous prefix {0}, breaks at the A/A conflict.
    assert _shapes(batches) == [(False, [0]), (True, [1, 2])]


def test_maximal_packs_across_a_conflict() -> None:
    tool = _Conflicting()
    resolved = [_call(tool, 0, "A"), _call(tool, 1, "A"), _call(tool, 2, "B")]
    batches = _partition_batches(resolved, _allow(3), max_concurrency=8, strategy="maximal")
    # Maximal admits 0 and 2 together (skipping the conflicting 1), defers 1.
    assert _shapes(batches) == [(True, [0, 2]), (False, [1])]


def test_maximal_respects_concurrency_cap() -> None:
    tool = _Conflicting()
    resolved = [_call(tool, 0, "A"), _call(tool, 1, "B"), _call(tool, 2, "C")]
    batches = _partition_batches(resolved, _allow(3), max_concurrency=2, strategy="maximal")
    assert _shapes(batches) == [(True, [0, 1]), (False, [2])]


def test_maximal_keeps_non_parallel_calls_as_barriers() -> None:
    conf = _Conflicting()
    ser = _SerialTool()
    resolved = [
        _call(conf, 0, "A"),
        _call(conf, 1, "B"),
        _call(ser, 2, None),
        _call(conf, 3, "C"),
        _call(conf, 4, "D"),
    ]
    batches = _partition_batches(resolved, _allow(5), max_concurrency=8, strategy="maximal")
    # Packs each parallel run, but the serial call splits the two runs.
    assert _shapes(batches) == [(True, [0, 1]), (False, [2]), (True, [3, 4])]


def test_maximal_matches_greedy_when_no_conflicts() -> None:
    tool = _Conflicting()
    resolved = [_call(tool, i, chr(ord("A") + i)) for i in range(4)]
    greedy = _partition_batches(resolved, _allow(4), max_concurrency=8)
    maximal = _partition_batches(resolved, _allow(4), max_concurrency=8, strategy="maximal")
    assert _shapes(greedy) == _shapes(maximal) == [(True, [0, 1, 2, 3])]


def _session() -> SimpleNamespace:
    return SimpleNamespace(
        id="s1",
        store=None,
        active_run_id="run-1",
        tools_override=None,
        current_turn_allowed_tools=None,
    )


def _agent(registry: ToolRegistry, strategy: str) -> SimpleNamespace:
    from linch import Context

    return SimpleNamespace(
        cwd=".",
        tools=registry,
        permission_engine=PermissionEngine(mode="skip-dangerous"),
        max_tool_concurrency=8,
        tool_concurrency=8,
        tool_batching_strategy=strategy,
        context=Context(),
    )


async def _start_order(strategy: str) -> tuple[list[str], list[str]]:
    registry = ToolRegistry()
    registry.register(_Conflicting("Conf"))
    blocks = [
        ToolUseBlock(id="t0", name="Conf", input={"res": "A"}),
        ToolUseBlock(id="t1", name="Conf", input={"res": "A"}),
        ToolUseBlock(id="t2", name="Conf", input={"res": "B"}),
    ]
    starts: list[str] = []
    ends: list[str] = []
    stream = execute_tool_calls(blocks, _agent(registry, strategy), _session(), AbortContext())
    async for event in stream:
        if isinstance(event, ToolCallStartEvent):
            starts.append(event.tool_use_id)
        elif isinstance(event, ToolCallEndEvent):
            ends.append(event.tool_use_id)
    return starts, ends


@pytest.mark.asyncio
async def test_greedy_start_order_is_provider_order() -> None:
    starts, ends = await _start_order("greedy")
    assert starts == ["t0", "t1", "t2"]
    assert ends == ["t0", "t1", "t2"]


@pytest.mark.asyncio
async def test_maximal_start_order_reflects_admission() -> None:
    starts, ends = await _start_order("maximal")
    # t2 is admitted into the first batch ahead of the conflicting t1.
    assert starts == ["t0", "t2", "t1"]
    assert ends == ["t0", "t2", "t1"]


@pytest.mark.parametrize(
    ("strategy", "expected"),
    [("maximal", "maximal"), (None, "greedy")],
)
def test_agent_tool_batching_strategy_defaults_and_accepts(
    strategy: str | None, expected: str
) -> None:
    kwargs = {} if strategy is None else {"tool_batching_strategy": strategy}
    agent = Agent(model="dummy", provider=ScriptedProvider([]), **kwargs)
    assert agent.tool_batching_strategy == expected


def test_agent_rejects_invalid_strategy() -> None:
    with pytest.raises(ConfigError):
        Agent(model="dummy", provider=ScriptedProvider([]), tool_batching_strategy="bogus")
