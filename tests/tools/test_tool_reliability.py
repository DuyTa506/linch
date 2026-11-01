from __future__ import annotations

import asyncio
from typing import Any

import pytest

from linch import RetryOptions
from linch.abort import AbortContext
from linch.errors import AbortError
from linch.events import ToolCallEndEvent
from linch.scheduler import execute_tool_calls
from linch.tools import ToolContext, ToolRegistry, ToolResult
from linch.types import ToolUseBlock
from tests.support.scheduler import scheduler_agent, scheduler_session


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
            scheduler_agent(registry, tool_timeout_ms=tool_timeout_ms, tool_retry=tool_retry),
            scheduler_session(),
            signal or AbortContext(),
        )
    ]


def end_events(events: list[Any]) -> list[ToolCallEndEvent]:
    return [e for e in events if isinstance(e, ToolCallEndEvent)]


@pytest.mark.asyncio
async def test_timeout_produces_error_result_and_run_continues() -> None:
    """A timed-out tool returns is_error result; a subsequent tool still runs."""
    registry = ToolRegistry()
    registry.register(SleepyTool("Slow", delay=0.2, scope="read"))
    # Fast write-scope tool queued after the slow one
    registry.register(SleepyTool("Fast", delay=0.001, scope="write", parallel=False))

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
    registry.register(SleepyTool("Slow", delay=0.2, scope="read", parallel=True))
    registry.register(SleepyTool("Fast", delay=0.005, scope="read", parallel=True))

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
    registry.register(SleepyTool("Slow", delay=0.5, scope="read"))

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
@pytest.mark.parametrize(
    ("agent_timeout", "tool_timeout", "delay", "timed_out"),
    [
        (10, 500, 0.05, False),  # the tool-specific limit wins
        (None, 10, 0.2, True),  # a tool-specific limit applies without a default
        (None, None, 0.05, False),  # no configured limit preserves old behavior
        (10, 0, 0.05, False),  # zero explicitly opts out of the agent default
    ],
    ids=["tool-loosens-default", "tool-tightens-none", "none-backcompat", "zero-opt-out"],
)
async def test_tool_timeout_configuration(
    agent_timeout: float | None,
    tool_timeout: float | None,
    delay: float,
    timed_out: bool,
) -> None:
    """Resolve the effective timeout from agent and per-tool settings."""
    registry = ToolRegistry()
    registry.register(SleepyTool("T", delay=delay, scope="read", execution_timeout_ms=tool_timeout))
    events = await collect_events(
        registry,
        [_block("T", "b1")],
        tool_timeout_ms=agent_timeout,
    )
    end = end_events(events)[0]
    assert end.is_error is timed_out
    if timed_out:
        assert "timed out" in end.result
    else:
        assert "T-ok" in end.result


@pytest.mark.asyncio
async def test_cleanup_runs_on_timeout_cancellation() -> None:
    """asyncio.wait_for cancels the coroutine → tool's finally block executes."""
    flag: list[bool] = []
    registry = ToolRegistry()
    registry.register(SleepyTool("T", delay=0.2, scope="read", finally_flag=flag))

    await collect_events(
        registry,
        [_block("T", "b1")],
        tool_timeout_ms=20,
    )

    assert flag == [True], "finally block did not run — cleanup not guaranteed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fail_times", "is_error"),
    [(2, False), (5, True)],
    ids=["eventual-success", "exhausted"],
)
async def test_retry_outcomes(fail_times: int, is_error: bool) -> None:
    """Retry a transient read failure until success or the attempt limit."""
    tool = FlakyTool("F", fail_times=fail_times, retryable=True)
    registry = ToolRegistry()
    registry.register(tool)

    events = await collect_events(
        registry,
        [_block("F", "b1")],
        tool_retry=RetryOptions(max_attempts=3, base_delay_ms=1),
    )

    ends = end_events(events)
    assert ends[0].is_error is is_error
    if is_error:
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
    registry.register(SlowThenFastTool())

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
    registry.register(AbortingTool())

    with pytest.raises(AbortError):
        await collect_events(
            registry,
            [_block("Aborter", "b1")],
            tool_retry=RetryOptions(max_attempts=5, base_delay_ms=1),
        )

    assert len(call_count) == 1, "AbortError must not be retried"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "scope", "fail_times", "retryable", "retry", "expected_calls", "is_error"),
    [
        ("W", "write", 2, False, True, 1, True),
        ("W", "write", 1, True, True, 2, False),
        ("F", "read", 1, True, False, 1, True),
    ],
    ids=["write-default", "write-opt-in", "retry-disabled"],
)
async def test_retry_policy(
    name: str,
    scope: str,
    fail_times: int,
    retryable: bool,
    retry: bool,
    expected_calls: int,
    is_error: bool,
) -> None:
    """Apply retry opt-ins by scope and preserve the no-retry default."""
    tool = FlakyTool(
        name,
        fail_times=fail_times,
        scope=scope,
        parallel=scope == "read",
        retryable=retryable,
    )
    registry = ToolRegistry()
    registry.register(tool)
    events = await collect_events(
        registry,
        [_block(name, "b1")],
        tool_retry=RetryOptions(max_attempts=3, base_delay_ms=1) if retry else None,
    )
    end = end_events(events)[0]
    assert end.is_error is is_error
    assert tool.call_count == expected_calls


def test_tool_timeout_error_is_retryable() -> None:
    from linch import ToolTimeoutError

    exc = ToolTimeoutError("timed out")
    assert exc.retryable is True
    assert exc.kind == "tool_timeout"
