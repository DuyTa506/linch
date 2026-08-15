"""Phase 2: the composable tools/execute pipeline seam and its wrappers."""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

from linch.errors import ToolTimeoutError
from linch.tools.pipeline import ToolExecution, ToolPipeline
from linch.tools.wrappers import metrics_wrapper, timeout_wrapper


def _exec(name: str = "Bash") -> ToolExecution:
    return ToolExecution(tool_name=name, tool_use_id="t1", input={}, ctx=None)


@pytest.mark.asyncio
async def test_no_wrappers_reports_empty_and_runs_terminal() -> None:
    pipe = ToolPipeline()
    assert pipe.has_execute_listeners() is False

    async def terminal() -> str:
        return "body"

    assert await pipe.run_execute(_exec(), terminal) == "body"


@pytest.mark.asyncio
async def test_execute_wrapper_wraps_body_in_order() -> None:
    order: list[str] = []
    pipe = ToolPipeline()

    async def wrapper(execution: ToolExecution, next) -> object:
        order.append("before")
        result = await next()
        order.append("after")
        return result

    pipe.on_execute(wrapper)
    assert pipe.has_execute_listeners() is True

    async def terminal() -> str:
        order.append("body")
        return "ok"

    assert await pipe.run_execute(_exec(), terminal) == "ok"
    assert order == ["before", "body", "after"]


@pytest.mark.asyncio
async def test_execute_wrapper_can_short_circuit() -> None:
    reached: list[str] = []
    pipe = ToolPipeline()

    async def short(execution: ToolExecution, next) -> str:
        return "served"

    pipe.on_execute(short)

    async def terminal() -> str:
        reached.append("body")
        return "body"

    assert await pipe.run_execute(_exec(), terminal) == "served"
    assert reached == []


@pytest.mark.asyncio
async def test_execute_wrapper_can_mutate_signal_seen_by_terminal() -> None:
    execution = _exec()
    pipe = ToolPipeline()

    async def wrapper(exe: ToolExecution, next) -> object:
        exe.signal = "deadline"
        return await next()

    pipe.on_execute(wrapper)

    async def terminal() -> object:
        return execution.signal

    assert await pipe.run_execute(execution, terminal) == "deadline"


@pytest.mark.asyncio
async def test_complete_pipeline_runs_in_phase_order_and_replaces_result() -> None:
    order: list[str] = []
    pipe = ToolPipeline()

    async def pre(execution: ToolExecution, next) -> object:
        order.append("pre-before")
        result = await next()
        order.append("pre-after")
        return result

    async def execute(execution: ToolExecution, next) -> object:
        order.append("execute-before")
        result = await next()
        order.append("execute-after")
        return result

    async def post(execution: ToolExecution, result: object, next) -> object:
        order.append(f"post:{result}")
        assert await next() == "body"
        return "replaced"

    pipe.on_pre_execute(pre)
    pipe.on_execute(execute)
    pipe.on_post_execute(post)

    async def terminal() -> str:
        order.append("body")
        return "body"

    assert await pipe.run(_exec(), terminal) == "replaced"
    assert order == [
        "pre-before",
        "pre-after",
        "execute-before",
        "body",
        "execute-after",
        "post:body",
    ]


@pytest.mark.asyncio
async def test_pre_short_circuit_still_flows_through_post() -> None:
    pipe = ToolPipeline()
    reached: list[str] = []

    async def pre(execution: ToolExecution, next) -> str:
        return "cached"

    async def post(execution: ToolExecution, result: object, next) -> str:
        assert result == "cached"
        assert await next() == "cached"
        return "presented"

    pipe.on_pre_execute(pre)
    pipe.on_post_execute(post)

    async def terminal() -> str:
        reached.append("body")
        return "body"

    assert await pipe.run(_exec(), terminal) == "presented"
    assert reached == []


@pytest.mark.asyncio
async def test_disposer_removes_execute_wrapper() -> None:
    pipe = ToolPipeline()

    async def wrapper(exe: ToolExecution, next) -> object:
        return await next()

    handle = pipe.on_execute(wrapper)
    assert pipe.has_execute_listeners() is True
    await handle.dispose()
    assert pipe.has_execute_listeners() is False


@pytest.mark.asyncio
async def test_timeout_wrapper_raises_on_slow_body() -> None:
    pipe = ToolPipeline()
    pipe.on_execute(timeout_wrapper(0.01))

    async def slow() -> str:
        await asyncio.sleep(1.0)
        return "never"

    with pytest.raises(ToolTimeoutError, match="pipeline timeout"):
        await pipe.run_execute(_exec(), slow)


@pytest.mark.asyncio
async def test_metrics_wrapper_records_success_and_error() -> None:
    records: list[tuple[str, int, BaseException | None]] = []
    pipe = ToolPipeline()
    pipe.on_execute(metrics_wrapper(lambda n, ms, err: records.append((n, ms, err))))

    async def ok() -> str:
        return "ok"

    assert await pipe.run_execute(_exec("Read"), ok) == "ok"
    assert records[0][0] == "Read"
    assert records[0][2] is None

    async def boom() -> str:
        raise ValueError("nope")

    with pytest.raises(ValueError):
        await pipe.run_execute(_exec("Read"), boom)
    assert isinstance(records[1][2], ValueError)


@pytest.mark.asyncio
async def test_metrics_registered_first_times_inner_wrapper() -> None:
    records: list[int] = []
    pipe = ToolPipeline()
    pipe.on_execute(metrics_wrapper(lambda n, ms, err: records.append(ms)))

    async def slow_wrapper(exe: ToolExecution, next) -> object:
        await asyncio.sleep(0.02)
        return await next()

    pipe.on_execute(slow_wrapper)  # registered second → inner

    async def terminal() -> str:
        return "ok"

    await pipe.run_execute(_exec(), terminal)
    assert records[0] >= 15  # metrics (outer) saw the inner sleep (~20ms)


@pytest.mark.asyncio
async def test_metrics_sink_failure_cannot_mask_result_error_or_cancellation() -> None:
    def broken_sink(name: str, ms: int, error: BaseException | None) -> None:
        raise RuntimeError("metrics unavailable")

    pipe = ToolPipeline()
    pipe.on_execute(metrics_wrapper(broken_sink))

    async def ok() -> str:
        return "ok"

    assert await pipe.run_execute(_exec(), ok) == "ok"

    async def boom() -> str:
        raise ValueError("tool failure")

    with pytest.raises(ValueError, match="tool failure"):
        await pipe.run_execute(_exec(), boom)

    async def cancelled() -> str:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await pipe.run_execute(_exec(), cancelled)


@pytest.mark.asyncio
async def test_timeout_wrapper_preserves_inner_timeout_error() -> None:
    pipe = ToolPipeline()
    pipe.on_execute(timeout_wrapper(1))

    async def inner_timeout() -> str:
        raise asyncio.TimeoutError("inner deadline")

    with pytest.raises(asyncio.TimeoutError, match="inner deadline"):
        await pipe.run_execute(_exec(), inner_timeout)


@pytest.mark.asyncio
async def test_timeout_does_not_wait_forever_when_body_suppresses_cancellation() -> None:
    pipe = ToolPipeline()
    pipe.on_execute(timeout_wrapper(0.01))
    release = asyncio.Event()
    cancellations = 0

    async def stubborn() -> str:
        nonlocal cancellations
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancellations += 1
        return "late"

    with pytest.raises(ToolTimeoutError, match="pipeline timeout"):
        await asyncio.wait_for(pipe.run_execute(_exec(), stubborn), timeout=0.2)
    assert cancellations >= 1

    release.set()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_timeout_wrapper_preserves_caller_cancellation() -> None:
    pipe = ToolPipeline()
    pipe.on_execute(timeout_wrapper(10))
    started = asyncio.Event()

    async def body() -> str:
        started.set()
        await asyncio.Event().wait()
        return "never"

    running = asyncio.create_task(pipe.run_execute(_exec(), body))
    await started.wait()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running


def test_builtin_wrapper_resume_identity_includes_timeout_configuration() -> None:
    pipe = ToolPipeline()
    pipe.on_execute(metrics_wrapper(lambda name, ms, error: None))
    pipe.on_execute(timeout_wrapper(2.5))

    config = pipe.resume_policy_config
    execute = config["tools/execute"]
    assert [item["policy_id"] for item in execute] == [
        "linch.tools.wrappers.metrics",
        "linch.tools.wrappers.timeout",
    ]
    assert execute[1]["config"] == {"seconds": 2.5}
    assert execute[1]["policy_version"] == "2"


def test_anonymous_listener_has_no_durable_resume_identity() -> None:
    pipe = ToolPipeline()

    async def anonymous(execution: ToolExecution, next) -> object:
        return await next()

    pipe.on_pre_execute(anonymous)
    with pytest.raises(ValueError, match="resume_policy_id"):
        _ = pipe.resume_policy_config


def test_listener_requires_json_safe_semantic_config_or_fingerprint() -> None:
    pipe = ToolPipeline()

    async def id_only(execution: ToolExecution, next) -> object:
        return await next()

    id_only.resume_policy_id = "test.id-only"  # type: ignore[attr-defined]
    pipe.on_execute(id_only)
    with pytest.raises(ValueError, match="resume_policy_config.*resume_policy_fingerprint"):
        _ = pipe.resume_policy_config

    id_only.resume_policy_config = {"bad": float("nan")}  # type: ignore[attr-defined]
    with pytest.raises(ValueError, match="JSON-safe"):
        _ = pipe.resume_policy_config


# ── Scheduler integration: the tools/execute seam is actually invoked ──────────


class _FakeTool:
    name = "Echo"

    async def execute(self, input: dict, ctx: object) -> str:
        return f"ran:{input.get('v')}"


class _FakeCall:
    def __init__(self, tool: object) -> None:
        self.tool = tool
        self.block = None
        self.id = "call-1"


@pytest.mark.asyncio
async def test_dispatch_execute_passthrough_without_pipeline() -> None:
    from linch.scheduler import _dispatch_execute

    tool = _FakeTool()
    call = _FakeCall(tool)
    agent = type("A", (), {"tool_pipeline": None})()
    coro = _dispatch_execute(agent, call, tool, {"v": 1}, ctx=None, signal=None)  # type: ignore[arg-type]
    assert await coro == "ran:1"


@pytest.mark.asyncio
async def test_dispatch_execute_runs_pipeline_wrapper() -> None:
    from linch.scheduler import _dispatch_execute

    seen: list[str] = []
    pipe = ToolPipeline()

    async def wrapper(execution: ToolExecution, next) -> object:
        seen.append(execution.tool_name)
        return await next()

    pipe.on_execute(wrapper)
    tool = _FakeTool()
    call = _FakeCall(tool)
    agent = type("A", (), {"tool_pipeline": pipe})()
    coro = _dispatch_execute(agent, call, tool, {"v": 2}, ctx=None, signal=None)  # type: ignore[arg-type]
    assert await coro == "ran:2"
    assert seen == ["Echo"]


@pytest.mark.asyncio
async def test_dispatch_rejects_authorization_sensitive_pipeline_mutation() -> None:
    from linch.errors import ToolExecutionError
    from linch.scheduler import _dispatch_execute

    class Context:
        def __init__(self, label: str, signal: object) -> None:
            self.label = label
            self.signal = signal

    class ReplacementTool:
        name = "Replacement"

        async def execute(self, input: dict, ctx: Context) -> str:
            return f"{input['v']}:{ctx.label}:{ctx.signal}"

    pipe = ToolPipeline()

    async def pre(execution: ToolExecution, next) -> object:
        execution.tool = ReplacementTool()
        execution.input = {"v": 9}
        execution.ctx = Context("new", "stale")
        return await next()

    async def execute(execution: ToolExecution, next) -> object:
        execution.signal = "deadline"
        return await next()

    async def post(execution: ToolExecution, result: object, next) -> str:
        return f"{await next()}:post"

    pipe.on_pre_execute(pre)
    pipe.on_execute(execute)
    pipe.on_post_execute(post)
    original = _FakeTool()
    call = _FakeCall(original)
    agent = type("A", (), {"tool_pipeline": pipe})()
    coro = _dispatch_execute(
        agent,
        call,  # type: ignore[arg-type]
        original,
        {"v": 1},
        Context("old", None),  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
    )
    with pytest.raises(ToolExecutionError, match="authorization-sensitive"):
        await coro


@pytest.mark.asyncio
async def test_dispatch_rejects_in_place_mutation_of_approved_input() -> None:
    from linch.errors import ToolExecutionError
    from linch.scheduler import _dispatch_execute

    pipe = ToolPipeline()

    async def rewrite(execution: ToolExecution, next) -> object:
        execution.input["v"] = "unapproved"
        return await next()

    pipe.on_execute(rewrite)
    tool = _FakeTool()
    call = _FakeCall(tool)
    agent = type("A", (), {"tool_pipeline": pipe})()
    coro = _dispatch_execute(
        agent,
        call,  # type: ignore[arg-type]
        tool,
        {"v": "approved"},
        ctx=None,  # type: ignore[arg-type]
        signal=None,  # type: ignore[arg-type]
    )
    with pytest.raises(ToolExecutionError, match="authorization-sensitive"):
        await coro


@pytest.mark.asyncio
async def test_dispatch_allows_signal_and_result_mutation() -> None:
    from linch.scheduler import _dispatch_execute

    class Context:
        def __init__(self) -> None:
            self.signal: object = "original"

    class SignalTool:
        name = "Signal"

        async def execute(self, input: dict, ctx: Context) -> str:
            return f"{input['v']}:{ctx.signal}"

    pipe = ToolPipeline()

    async def execute(execution: ToolExecution, next) -> object:
        execution.signal = "deadline"
        return await next()

    async def post(execution: ToolExecution, result: object, next) -> str:
        return f"{await next()}:post"

    pipe.on_execute(execute)
    pipe.on_post_execute(post)
    tool = SignalTool()
    call = _FakeCall(tool)
    agent = type("A", (), {"tool_pipeline": pipe})()
    coro = _dispatch_execute(
        agent,
        call,  # type: ignore[arg-type]
        tool,
        {"v": 1},
        Context(),  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
    )
    assert await coro == "1:deadline:post"


@pytest.mark.asyncio
async def test_pipeline_cannot_mutate_away_v2_normalization_boundary() -> None:
    from types import SimpleNamespace

    from linch.abort import AbortContext
    from linch.events import ToolCallEndEvent
    from linch.permissions import PermissionEngine
    from linch.scheduler import execute_tool_calls
    from linch.tools import FunctionTool, ToolRegistry, ToolResult
    from linch.types import ToolUseBlock

    def number_fn() -> int:
        return 1

    number = FunctionTool(number_fn, output_schema={"type": "integer"})

    pipeline = ToolPipeline()

    async def bypass(execution: ToolExecution, next) -> ToolResult:
        _ = await next()
        execution.tool.output_schema = None
        return ToolResult(content="unvalidated legacy result")

    pipeline.on_execute(bypass)
    registry = ToolRegistry()
    registry.register(cast(Any, number))
    agent = SimpleNamespace(
        cwd=".",
        model="m",
        tools=registry,
        permission_engine=PermissionEngine(mode="skip-dangerous"),
        max_tool_concurrency=1,
        tool_concurrency=1,
        tool_retry=None,
        hooks=[],
        result_offload=None,
        tool_pipeline=pipeline,
    )
    session = SimpleNamespace(
        id="s",
        store=None,
        active_run_id="r",
        tools_override=None,
        current_turn_allowed_tools=None,
        run_deps=None,
        filesystem=None,
    )

    events = [
        event
        async for event in execute_tool_calls(
            [ToolUseBlock(id="call", name="number_fn", input={})],
            agent,
            session,
            AbortContext(),
        )
    ]
    end = next(event for event in events if isinstance(event, ToolCallEndEvent))
    assert end.is_error is True
    assert "must not return ToolResult" in end.result
    assert end.tool_output is None
