"""Tool timeout and retry demo — Phase 11 reliability features.

Run:
    python3 examples/tools/tool_reliability_agent.py

Runs the scheduler directly; no live provider call needed.

Demonstrates:
  1. Agent(tool_timeout_ms=...) — agent-wide execution deadline.
  2. execution_timeout_ms=0 on a tool class — opt out of agent deadline.
  3. RetryOptions(max_attempts=...) — exponential-backoff retry.
  4. retryable=True on write/exec tools — explicit side-effect opt-in.
  5. ToolTimeoutError — the typed error emitted on timeout.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from linch import RetryOptions
from linch.abort import AbortContext
from linch.events import ToolCallEndEvent, ToolCallStartEvent
from linch.permissions import PermissionEngine
from linch.scheduler import execute_tool_calls
from linch.tools import ToolContext, ToolRegistry, ToolResult
from linch.types import ToolUseBlock

ROOT = Path(__file__).resolve().parents[2]


class SlowReadTool:
    """Read tool that sleeps to simulate a slow network call."""

    description = "Simulate a slow remote read."
    input_schema = {
        "type": "object",
        "properties": {"key": {"type": "string"}},
        "required": ["key"],
    }
    scope = "read"
    parallel = True

    def __init__(self, name: str, delay_ms: float = 200) -> None:
        self.name = name
        self._delay = delay_ms / 1000.0

    def validate(self, raw: dict[str, Any]) -> dict[str, Any]:
        return raw

    async def execute(self, input: dict[str, Any], ctx: ToolContext) -> ToolResult:
        await asyncio.sleep(self._delay)
        return ToolResult(content=f"value:{input['key']}", summary=f"{self.name}({input['key']})")

    def summarize(self, input: dict[str, Any]) -> str:
        return f"{self.name}({input.get('key', '?')})"


class ManagedTimeoutTool:
    """Read tool that opts out of the agent-wide timeout via execution_timeout_ms=0.

    Use this pattern for tools that manage their own internal deadline (e.g.
    Bash with a subprocess timeout, or a gRPC stub with a per-call deadline).
    """

    name = "ManagedTimeout"
    description = "Tool with its own internal timeout."
    input_schema = {
        "type": "object",
        "properties": {"key": {"type": "string"}},
        "required": ["key"],
    }
    scope = "read"
    parallel = True
    execution_timeout_ms = 0  # 0 → opt out of any agent-wide timeout

    def validate(self, raw: dict[str, Any]) -> dict[str, Any]:
        return raw

    async def execute(self, input: dict[str, Any], ctx: ToolContext) -> ToolResult:
        await asyncio.sleep(0.05)  # well within any real deadline
        return ToolResult(
            content=f"managed:{input['key']}",
            summary=f"ManagedTimeout({input['key']})",
        )

    def summarize(self, input: dict[str, Any]) -> str:
        return f"ManagedTimeout({input.get('key', '?')})"


class FlakySearchTool:
    """Read tool that fails `fail_count` times before succeeding.

    Read-scope tools are retried on any exception when tool_retry is set —
    no extra opt-in needed because reads are idempotent.
    """

    name = "FlakySearch"
    description = "Simulate transient network errors."
    input_schema = {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    }
    scope = "read"
    parallel = True

    def __init__(self, fail_count: int = 2) -> None:
        self._fail_count = fail_count
        self.call_count = 0

    def validate(self, raw: dict[str, Any]) -> dict[str, Any]:
        return raw

    async def execute(self, input: dict[str, Any], ctx: ToolContext) -> ToolResult:
        self.call_count += 1
        if self.call_count <= self._fail_count:
            raise ConnectionError(f"Transient error (attempt {self.call_count})")
        return ToolResult(
            content=f"results for {input['query']!r}",
            summary=f"FlakySearch({input['query']})",
        )

    def summarize(self, input: dict[str, Any]) -> str:
        return f"FlakySearch({input.get('query', '?')})"


class FlakyWriteTool:
    """Write tool that fails unless it explicitly sets retryable=True.

    Write/exec tools are NOT retried by default to avoid double side-effects.
    Set class-level `retryable = True` to explicitly opt in.
    """

    description = "Simulate a flaky write operation."
    input_schema = {
        "type": "object",
        "properties": {"data": {"type": "string"}},
        "required": ["data"],
    }
    scope = "write"
    retryable = True  # explicit opt-in for write tools

    def __init__(self, name: str, fail_count: int = 1) -> None:
        self.name = name
        self._fail_count = fail_count
        self.call_count = 0

    def validate(self, raw: dict[str, Any]) -> dict[str, Any]:
        return raw

    async def execute(self, input: dict[str, Any], ctx: ToolContext) -> ToolResult:
        self.call_count += 1
        if self.call_count <= self._fail_count:
            raise OSError(f"Write failed (attempt {self.call_count})")
        return ToolResult(
            content=f"written:{input['data']}", summary=f"{self.name}({input['data']})"
        )

    def summarize(self, input: dict[str, Any]) -> str:
        return f"{self.name}({input.get('data', '?')})"


def _make_agent(registry: ToolRegistry, **kwargs: Any) -> SimpleNamespace:
    return SimpleNamespace(
        cwd=str(ROOT),
        tools=registry,
        permission_engine=PermissionEngine(mode="skip-dangerous"),
        max_tool_concurrency=4,
        tool_timeout_ms=kwargs.get("tool_timeout_ms"),
        tool_retry=kwargs.get("tool_retry"),
    )


def _make_session() -> SimpleNamespace:
    return SimpleNamespace(
        id="demo",
        store=None,
        active_run_id="reliability-demo",
        tools_override=None,
        current_turn_allowed_tools=None,
    )


async def _run(calls: list[ToolUseBlock], agent: SimpleNamespace) -> None:
    async for event in execute_tool_calls(calls, agent, _make_session(), AbortContext()):
        if isinstance(event, ToolCallStartEvent):
            print(f"  start  {event.tool_name}")
        elif isinstance(event, ToolCallEndEvent):
            suffix = " [ERROR]" if event.is_error else ""
            print(f"  end    {event.tool_name}: {event.result[:70]}{suffix}")


async def demo_timeout() -> None:
    print("── 1. Timeout: agent_timeout=50ms, tool sleeps 200ms ──────────────")
    registry = ToolRegistry()
    registry.add(SlowReadTool("SlowRead", delay_ms=200))
    agent = _make_agent(registry, tool_timeout_ms=50)
    await _run([ToolUseBlock(id="a", name="SlowRead", input={"key": "alpha"})], agent)


async def demo_opt_out() -> None:
    print("\n── 2. Opt-out: execution_timeout_ms=0 ignores agent deadline ──────")
    registry = ToolRegistry()
    registry.add(ManagedTimeoutTool())
    # agent_timeout=50ms but ManagedTimeoutTool declares execution_timeout_ms=0
    agent = _make_agent(registry, tool_timeout_ms=50)
    await _run([ToolUseBlock(id="b", name="ManagedTimeout", input={"key": "beta"})], agent)


async def demo_parallel_timeout() -> None:
    print("\n── 3. Parallel safety: sibling tool succeeds when other times out ──")
    registry = ToolRegistry()
    registry.add(SlowReadTool("Slow", delay_ms=200))
    registry.add(SlowReadTool("Fast", delay_ms=10))
    agent = _make_agent(registry, tool_timeout_ms=50)
    calls = [
        ToolUseBlock(id="c1", name="Slow", input={"key": "slow"}),
        ToolUseBlock(id="c2", name="Fast", input={"key": "fast"}),
    ]
    await _run(calls, agent)


async def demo_retry_read() -> None:
    print("\n── 4. Retry: read tool fails 2×, succeeds on attempt 3 ────────────")
    flaky = FlakySearchTool(fail_count=2)
    registry = ToolRegistry()
    registry.add(flaky)
    agent = _make_agent(registry, tool_retry=RetryOptions(max_attempts=3, base_delay_ms=5))
    await _run([ToolUseBlock(id="d", name="FlakySearch", input={"query": "resilience"})], agent)
    print(f"  → total attempts: {flaky.call_count}")


async def demo_retry_exhausted() -> None:
    print("\n── 5. Retry exhausted: fail_count > max_attempts ───────────────────")
    flaky = FlakySearchTool(fail_count=10)
    registry = ToolRegistry()
    registry.add(flaky)
    agent = _make_agent(registry, tool_retry=RetryOptions(max_attempts=3, base_delay_ms=5))
    await _run([ToolUseBlock(id="e", name="FlakySearch", input={"query": "hopeless"})], agent)
    print(f"  → total attempts: {flaky.call_count}")


async def demo_write_retry_opt_in() -> None:
    print("\n── 6. Write retry opt-in: retryable=True on write tool ─────────────")
    write_tool = FlakyWriteTool("FlakyWrite", fail_count=1)
    registry = ToolRegistry()
    registry.add(write_tool)
    agent = _make_agent(registry, tool_retry=RetryOptions(max_attempts=3, base_delay_ms=5))
    await _run([ToolUseBlock(id="f", name="FlakyWrite", input={"data": "payload"})], agent)
    print(f"  → total attempts: {write_tool.call_count}")


async def main() -> None:
    await demo_timeout()
    await demo_opt_out()
    await demo_parallel_timeout()
    await demo_retry_read()
    await demo_retry_exhausted()
    await demo_write_retry_opt_in()


if __name__ == "__main__":
    asyncio.run(main())
