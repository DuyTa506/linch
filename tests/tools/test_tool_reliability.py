"""Tests for Phase-11 tool execution reliability: per-tool timeout + opt-in retry.

Harness mirrors test_scheduler_v2.py: SimpleNamespace agent/session, ToolRegistry,
AbortContext, and `async for event in execute_tool_calls(...)`.  All delays are
ms-scale so the suite stays fast.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from agent_kit import RetryOptions, ToolTimeoutError
from agent_kit.abort import AbortContext
from agent_kit.errors import AbortError
from agent_kit.events import ToolCallEndEvent
from agent_kit.permissions import PermissionEngine
from agent_kit.scheduler import execute_tool_calls
from agent_kit.tools import ToolContext, ToolRegistry, ToolResult
from agent_kit.types import ToolUseBlock

# ---------------------------------------------------------------------------
# Test tools
# ---------------------------------------------------------------------------


class SleepyTool:
    """Sleeps for `delay` seconds then succeeds.  Optionally tracks cleanup via
    a mutable `finally_flag` list (appended to in finally block)."""

    description = "Sleeps and returns."
    input_schema = {"type": "object", "properties": {}}

    def __init__(
        self,
        name: str,
        delay: float,
        *,
        scope: str = "read",
        parallel: bool = True,
        finally_flag: list[bool] | None = None,
        execution_timeout_ms: float | None = None,
    ) -> None:
        self.name = name
        self.delay = delay
        self.scope = scope
        self.parallel = parallel
        self.parallel_safe = parallel
        self.finally_flag = finally_flag
        if execution_timeout_ms is not None:
            self.execution_timeout_ms = execution_timeout_ms

    def validate(self, raw: dict[str, Any]) -> dict[str, Any]:
        return raw

    def summarize(self, inp: dict[str, Any]) -> str:
        return self.name

    def resources(self, inp: dict[str, Any]) -> list:
        return []

    async def execute(self, inp: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            await asyncio.sleep(self.delay)
            return ToolResult(content=f"{self.name}-ok")
        finally:
            if self.finally_flag is not None:
                self.finally_flag.append(True)


class FlakyTool:
    """Raises `exc` for the first `fail_times` calls, then succeeds.
    Tracks call count so tests can assert retry count.
    Set `retryable = True` to opt in to retry for all scopes."""

    description = "Fails a few times then succeeds."
    input_schema = {"type": "object", "properties": {}}

    def __init__(
        self,
        name: str,
        fail_times: int,
        exc: Exception | None = None,
        *,
        scope: str = "read",
        parallel: bool = True,
        retryable: bool = False,
    ) -> None:
        self.name = name
        self.fail_times = fail_times
        self.exc = exc or RuntimeError("transient failure")
        self.scope = scope
        self.parallel = parallel
        self.parallel_safe = parallel
        self.retryable = retryable
        self.call_count = 0

    def validate(self, raw: dict[str, Any]) -> dict[str, Any]:
        return raw

    def summarize(self, inp: dict[str, Any]) -> str:
        return self.name

    def resources(self, inp: dict[str, Any]) -> list:
        return []

    async def execute(self, inp: dict[str, Any], ctx: ToolContext) -> ToolResult:
        self.call_count += 1
        if self.call_count <= self.fail_times:
            raise self.exc
        return ToolResult(content=f"{self.name}-ok")


# ---------------------------------------------------------------------------
# Harness helpers
# ---------------------------------------------------------------------------


def make_agent(
    registry: ToolRegistry,
    *,
    tool_timeout_ms: float | None = None,
    tool_retry: RetryOptions | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        cwd=".",
        tools=registry,
        permission_engine=PermissionEngine(mode="skip-dangerous"),
        max_tool_concurrency=8,
        tool_concurrency=8,
        tool_timeout_ms=tool_timeout_ms,
        tool_retry=tool_retry,
    )


def make_session() -> SimpleNamespace:
    return SimpleNamespace(
        id="s1",
        store=None,
        active_run_id="run-1",
        tools_override=None,
        current_turn_allowed_tools=None,
    )


def _block(tool_name: str, bid: str = "b1") -> ToolUseBlock:
    return ToolUseBlock(id=bid, name=tool_name, input={})


async def collect_events(
    registry: ToolRegistry,
    blocks: list[ToolUseBlock],
    *,
    tool_timeout_ms: float | None = None,
    tool_retry: RetryOptions | None = None,
    signal: AbortContext | None = None,
) -> list[Any]:
    return [
        e
        async for e in execute_tool_calls(
            blocks,
            make_agent(registry, tool_timeout_ms=tool_timeout_ms, tool_retry=tool_retry),
            make_session(),
            signal or AbortContext(),
        )
    ]


def end_events(events: list[Any]) -> list[ToolCallEndEvent]:
    return [e for e in events if isinstance(e, ToolCallEndEvent)]


# ---------------------------------------------------------------------------
# Timeout tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timeout_produces_error_result_and_run_continues() -> None:
    """A timed-out tool returns is_error result; a subsequent tool still runs."""
    registry = ToolRegistry()
    registry.add(SleepyTool("Slow", delay=0.2, scope="read"))
    # Fast write-scope tool queued after the slow one
    registry.add(SleepyTool("Fast", delay=0.001, scope="write", parallel=False))

    events = await collect_events(
        registry,
        [_block("Slow", "b1"), _block("Fast", "b2")],
        tool_timeout_ms=20,  # 20ms — Slow (200ms) will time out, Fast (1ms) won't
    )

    ends = end_events(events)
    assert len(ends) == 2

    slow_end = next(e for e in ends if e.tool_name == "Slow")
    fast_end = next(e for e in ends if e.tool_name == "Fast")

    assert slow_end.is_error is True
    assert "timed out after 20ms" in slow_end.result
    assert "retry with a larger timeout" in slow_end.result
    assert slow_end.tool_result is not None
    assert slow_end.tool_result.is_error is True
    assert slow_end.tool_result.content == slow_end.result

    assert fast_end.is_error is False  # run continued, second tool succeeded


@pytest.mark.asyncio
async def test_parallel_timeout_does_not_kill_sibling() -> None:
    """Timeout in one parallel task must not abort sibling tasks."""
    registry = ToolRegistry()
    registry.add(SleepyTool("Slow", delay=0.2, scope="read", parallel=True))
    registry.add(SleepyTool("Fast", delay=0.005, scope="read", parallel=True))

    events = await collect_events(
        registry,
        [_block("Slow", "b1"), _block("Fast", "b2")],
        tool_timeout_ms=20,
    )

    ends = end_events(events)
    slow_end = next(e for e in ends if e.tool_name == "Slow")
    fast_end = next(e for e in ends if e.tool_name == "Fast")

    assert slow_end.is_error is True
    assert "timed out" in slow_end.result
    assert fast_end.is_error is False  # sibling ran to completion


@pytest.mark.asyncio
async def test_abort_not_masked_by_timeout() -> None:
    """AbortError must propagate out even when a timeout is configured."""
    registry = ToolRegistry()
    registry.add(SleepyTool("Slow", delay=0.5, scope="read"))

    signal = AbortContext()
    signal.abort()  # abort immediately

    with pytest.raises(AbortError):
        await collect_events(
            registry,
            [_block("Slow", "b1")],
            tool_timeout_ms=1000,
            signal=signal,
        )


@pytest.mark.asyncio
async def test_per_tool_attribute_loosens_agent_default() -> None:
    """Per-tool execution_timeout_ms > agent default → tool succeeds."""
    registry = ToolRegistry()
    # Tool has its own 500ms limit; agent default is 10ms — tool's value wins
    registry.add(SleepyTool("T", delay=0.05, scope="read", execution_timeout_ms=500))

    events = await collect_events(
        registry,
        [_block("T", "b1")],
        tool_timeout_ms=10,
    )

    ends = end_events(events)
    assert ends[0].is_error is False


@pytest.mark.asyncio
async def test_per_tool_attribute_tightens_when_agent_default_is_none() -> None:
    """Per-tool execution_timeout_ms with no agent default → tool can time out."""
    registry = ToolRegistry()
    registry.add(SleepyTool("T", delay=0.2, scope="read", execution_timeout_ms=10))

    events = await collect_events(
        registry,
        [_block("T", "b1")],
        tool_timeout_ms=None,
    )

    ends = end_events(events)
    assert ends[0].is_error is True
    assert "timed out" in ends[0].result


@pytest.mark.asyncio
async def test_none_default_no_timeout_backcompat() -> None:
    """No tool_timeout_ms → slow tool completes normally (zero-overhead backcompat)."""
    registry = ToolRegistry()
    registry.add(SleepyTool("T", delay=0.05, scope="read"))

    events = await collect_events(
        registry,
        [_block("T", "b1")],
        tool_timeout_ms=None,
    )

    ends = end_events(events)
    assert ends[0].is_error is False
    assert "T-ok" in ends[0].result


@pytest.mark.asyncio
async def test_per_tool_opt_out_zero_bypasses_agent_default() -> None:
    """execution_timeout_ms=0 on a tool opts out even when agent default is set."""
    registry = ToolRegistry()
    registry.add(SleepyTool("T", delay=0.05, scope="read", execution_timeout_ms=0))

    events = await collect_events(
        registry,
        [_block("T", "b1")],
        tool_timeout_ms=10,  # agent default is tight, but tool opted out
    )

    ends = end_events(events)
    assert ends[0].is_error is False  # completes because tool's 0 = no timeout


@pytest.mark.asyncio
async def test_cleanup_runs_on_timeout_cancellation() -> None:
    """asyncio.wait_for cancels the coroutine → tool's finally block executes."""
    flag: list[bool] = []
    registry = ToolRegistry()
    registry.add(SleepyTool("T", delay=0.2, scope="read", finally_flag=flag))

    await collect_events(
        registry,
        [_block("T", "b1")],
        tool_timeout_ms=20,
    )

    assert flag == [True], "finally block did not run — cleanup not guaranteed"


# ---------------------------------------------------------------------------
# Retry tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transient_failure_retried_then_succeeds() -> None:
    """read-scope tool, fail_times=2, max_attempts=3 → succeeds on 3rd attempt."""
    tool = FlakyTool("F", fail_times=2, retryable=True)
    registry = ToolRegistry()
    registry.add(tool)

    events = await collect_events(
        registry,
        [_block("F", "b1")],
        tool_retry=RetryOptions(max_attempts=3, base_delay_ms=1),
    )

    ends = end_events(events)
    assert ends[0].is_error is False
    assert tool.call_count == 3


@pytest.mark.asyncio
async def test_retry_exhausted_then_errors() -> None:
    """fail_times=5, max_attempts=3 → is_error after 3 attempts."""
    tool = FlakyTool("F", fail_times=5, retryable=True)
    registry = ToolRegistry()
    registry.add(tool)

    events = await collect_events(
        registry,
        [_block("F", "b1")],
        tool_retry=RetryOptions(max_attempts=3, base_delay_ms=1),
    )

    ends = end_events(events)
    assert ends[0].is_error is True
    assert ends[0].tool_result is not None
    assert ends[0].tool_result.is_error is True
    assert ends[0].tool_result.content == ends[0].result
    assert tool.call_count == 3


@pytest.mark.asyncio
async def test_timeout_is_retried_for_read_tool() -> None:
    """A read tool that times out on attempt 1 but succeeds on attempt 2."""
    call_n: list[int] = []

    class SlowThenFastTool:
        name = "SloFast"
        description = "Slow on first call, fast on second."
        input_schema = {"type": "object", "properties": {}}
        scope = "read"
        parallel = True
        parallel_safe = True

        def validate(self, raw):
            return raw

        def summarize(self, inp):
            return self.name

        def resources(self, inp):
            return []

        async def execute(self, inp, ctx):
            call_n.append(1)
            if len(call_n) == 1:
                await asyncio.sleep(0.2)  # will time out
            return ToolResult(content="ok")

    registry = ToolRegistry()
    registry.add(SlowThenFastTool())

    events = await collect_events(
        registry,
        [_block("SloFast", "b1")],
        tool_timeout_ms=20,
        tool_retry=RetryOptions(max_attempts=2, base_delay_ms=1),
    )

    ends = end_events(events)
    assert ends[0].is_error is False, "second attempt should have succeeded"
    assert len(call_n) == 2


@pytest.mark.asyncio
async def test_abort_error_not_retried() -> None:
    """AbortError raised from a tool must not be retried — should propagate."""
    call_count: list[int] = []

    class AbortingTool:
        name = "Aborter"
        description = "Always aborts."
        input_schema = {"type": "object", "properties": {}}
        scope = "read"
        parallel = True
        parallel_safe = True

        def validate(self, raw):
            return raw

        def summarize(self, inp):
            return self.name

        def resources(self, inp):
            return []

        async def execute(self, inp, ctx):
            call_count.append(1)
            raise AbortError("tool says abort")

    registry = ToolRegistry()
    registry.add(AbortingTool())

    with pytest.raises(AbortError):
        await collect_events(
            registry,
            [_block("Aborter", "b1")],
            tool_retry=RetryOptions(max_attempts=5, base_delay_ms=1),
        )

    assert len(call_count) == 1, "AbortError must not be retried"


@pytest.mark.asyncio
async def test_write_tool_not_retried_by_default() -> None:
    """Write-scope tool with no retryable opt-in: call_count stays 1."""
    tool = FlakyTool("W", fail_times=2, scope="write", parallel=False)
    registry = ToolRegistry()
    registry.add(tool)

    events = await collect_events(
        registry,
        [_block("W", "b1")],
        tool_retry=RetryOptions(max_attempts=3, base_delay_ms=1),
    )

    ends = end_events(events)
    assert ends[0].is_error is True
    assert tool.call_count == 1, "write-scope tools must not be retried by default"


@pytest.mark.asyncio
async def test_write_tool_retried_when_opted_in() -> None:
    """Write-scope tool with retryable=True IS retried."""
    tool = FlakyTool("W", fail_times=1, scope="write", parallel=False, retryable=True)
    registry = ToolRegistry()
    registry.add(tool)

    events = await collect_events(
        registry,
        [_block("W", "b1")],
        tool_retry=RetryOptions(max_attempts=3, base_delay_ms=1),
    )

    ends = end_events(events)
    assert ends[0].is_error is False
    assert tool.call_count == 2


@pytest.mark.asyncio
async def test_no_retry_by_default() -> None:
    """tool_retry=None (default) means a single attempt — no retry."""
    tool = FlakyTool("F", fail_times=1, retryable=True)
    registry = ToolRegistry()
    registry.add(tool)

    events = await collect_events(
        registry,
        [_block("F", "b1")],
        tool_retry=None,
    )

    ends = end_events(events)
    assert ends[0].is_error is True
    assert tool.call_count == 1


# ---------------------------------------------------------------------------
# ToolTimeoutError export test
# ---------------------------------------------------------------------------


def test_tool_timeout_error_is_retryable() -> None:
    """ToolTimeoutError.retryable is True so the retry predicate sees it as transient."""
    exc = ToolTimeoutError("timed out")
    assert exc.retryable is True
    assert exc.kind == "tool_timeout"
