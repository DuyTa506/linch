from __future__ import annotations

from types import SimpleNamespace

from linch.abort import AbortContext
from linch.events import ToolCallEndEvent
from linch.hooks import HookResult
from linch.permissions import PermissionEngine
from linch.providers.retry import RetryOptions
from linch.scheduler import execute_tool_calls
from linch.tools import ToolOutput, ToolOutputError, ToolRegistry, ToolResult, tool
from linch.types import ToolUseBlock


def _agent(registry: ToolRegistry, *, hooks=(), retry: RetryOptions | None = None):
    return SimpleNamespace(
        cwd=".",
        model="m",
        tools=registry,
        permission_engine=PermissionEngine(mode="skip-dangerous"),
        max_tool_concurrency=1,
        tool_concurrency=1,
        tool_retry=retry,
        hooks=list(hooks),
        result_offload=None,
    )


def _session():
    return SimpleNamespace(
        id="s",
        store=None,
        active_run_id="r",
        tools_override=None,
        current_turn_allowed_tools=None,
        run_deps=None,
        filesystem=None,
    )


async def _run(tool_obj, *, hooks=(), retry: RetryOptions | None = None):
    registry = ToolRegistry()
    registry.register(tool_obj)
    events = [
        event
        async for event in execute_tool_calls(
            [ToolUseBlock(id="call", name=tool_obj.name, input={})],
            _agent(registry, hooks=hooks, retry=retry),
            _session(),
            AbortContext(),
        )
    ]
    return events, next(event for event in events if isinstance(event, ToolCallEndEvent))


async def test_v2_raw_json_is_normalized_validated_rendered_and_emitted() -> None:
    @tool(output_schema={"type": "object", "required": ["é"]})
    def canonical():
        return {"z": [1, None], "é": True}

    _, end = await _run(canonical)

    assert end.result == '{"z":[1,null],"é":true}'
    assert end.tool_result == ToolResult(
        content='{"z":[1,null],"é":true}',
        summary="canonical",
        duration_ms=end.duration_ms,
    )
    assert end.tool_output == ToolOutput({"z": [1, None], "é": True})


async def test_v2_expected_error_bypasses_success_schema() -> None:
    @tool(output_schema={"type": "integer"})
    def missing():
        return ToolOutputError("record missing", code="not_found", details={"id": 2})

    _, end = await _run(missing)

    assert end.result == "record missing"
    assert end.is_error is True
    assert end.tool_result is not None and end.tool_result.is_error is True
    assert end.tool_output == ToolOutputError("record missing", code="not_found", details={"id": 2})


async def test_v2_validation_error_is_not_retried_and_fires_failure_hook() -> None:
    calls = 0
    failures = []

    @tool(output_schema={"type": "integer"})
    def invalid():
        nonlocal calls
        calls += 1
        return "not an integer"

    class FailureObserver:
        def on_post_tool_use_failure(self, ctx):
            failures.append(ctx)

    _, end = await _run(
        invalid,
        hooks=[FailureObserver()],
        retry=RetryOptions(max_attempts=3, base_delay_ms=0),
    )

    assert calls == 1
    assert end.is_error is True
    assert "Tool output invalid:" in end.result
    assert end.tool_output is None
    assert len(failures) == 1
    assert failures[0].result.is_error is True
    assert failures[0].tool_output is None


async def test_v2_renderer_failure_is_not_retried() -> None:
    calls = 0

    def broken_renderer(value):
        raise RuntimeError("renderer broke")

    @tool(
        output_schema={"type": "integer"},
        render_output=broken_renderer,
        renderer_id="test.broken",
        renderer_version="1",
    )
    def broken():
        nonlocal calls
        calls += 1
        return 2

    _, end = await _run(
        broken,
        retry=RetryOptions(max_attempts=3, base_delay_ms=0),
    )

    assert calls == 1
    assert end.is_error is True
    assert "renderer broke" in end.result
    assert end.tool_output is None


async def test_post_tool_use_canonical_mutations_revalidate_and_rerender() -> None:
    seen = []

    @tool(output_schema={"type": "integer"})
    def number():
        return 1

    class AddOne:
        def on_post_tool_use(self, ctx):
            seen.append((ctx.result.content, ctx.tool_output.value))
            return HookResult.mutate(tool_output=ToolOutput(ctx.tool_output.value + 1))

    class Double:
        def on_post_tool_use(self, ctx):
            seen.append((ctx.result.content, ctx.tool_output.value))
            return HookResult.mutate(tool_output=ToolOutput(ctx.tool_output.value * 2))

    _, end = await _run(number, hooks=[AddOne(), Double()])

    assert seen == [("1", 1), ("1", 2)]
    assert end.result == "4"
    assert end.tool_output == ToolOutput(4)


async def test_post_tool_use_legacy_mutation_clears_canonical_output() -> None:
    @tool(output_schema={"type": "integer"})
    def number():
        return 1

    class LegacyMutation:
        def on_post_tool_use(self, ctx):
            assert ctx.tool_output == ToolOutput(1)
            return HookResult.mutate(tool_result=ToolResult(content="legacy replacement"))

    _, end = await _run(number, hooks=[LegacyMutation()])

    assert end.result == "legacy replacement"
    assert end.tool_output is None


async def test_post_tool_use_block_clears_canonical_and_notifies_failure() -> None:
    failures = []

    @tool(output_schema={"type": "integer"})
    def number():
        return 1

    class Block:
        def on_post_tool_use(self, ctx):
            return HookResult.block("redacted by policy")

    class Failure:
        def on_post_tool_use_failure(self, ctx):
            failures.append(ctx)

    _, end = await _run(number, hooks=[Block(), Failure()])

    assert end.result == "redacted by policy"
    assert end.is_error is True
    assert end.tool_output is None
    assert failures[0].tool_output is None


async def test_pre_tool_use_canonical_resolve_uses_same_boundary() -> None:
    calls = 0

    @tool(output_schema={"type": "integer"})
    def number():
        nonlocal calls
        calls += 1
        return 1

    class Resolve:
        def on_pre_tool_use(self, ctx):
            return HookResult.resolve(tool_output=ToolOutput(8))

    _, end = await _run(number, hooks=[Resolve()])

    assert calls == 0
    assert end.result == "8"
    assert end.tool_output == ToolOutput(8)


async def test_v2_tool_rejects_legacy_tool_result_and_does_not_retry() -> None:
    calls = 0

    @tool(output_schema={"type": "string"})
    def invalid():
        nonlocal calls
        calls += 1
        return ToolResult(content="legacy")

    _, end = await _run(
        invalid,
        retry=RetryOptions(max_attempts=3, base_delay_ms=0),
    )

    assert calls == 1
    assert end.is_error is True
    assert "must not return ToolResult" in end.result
    assert end.tool_output is None


async def test_v2_tool_cache_hit_reenters_normalization_boundary() -> None:
    from linch.hooks import ToolCacheHook

    calls = 0

    @tool(output_schema={"type": "object"})
    def cached():
        nonlocal calls
        calls += 1
        return {"calls": calls}

    cache = ToolCacheHook()
    registry = ToolRegistry()
    registry.register(cached)
    agent = _agent(registry, hooks=[cache])
    session = _session()

    ends = []
    for call_id in ("first", "second"):
        events = [
            event
            async for event in execute_tool_calls(
                [ToolUseBlock(id=call_id, name="cached", input={})],
                agent,
                session,
                AbortContext(),
            )
        ]
        ends.append(next(event for event in events if isinstance(event, ToolCallEndEvent)))

    assert calls == 1
    assert [end.tool_output for end in ends] == [
        ToolOutput({"calls": 1}),
        ToolOutput({"calls": 1}),
    ]
    assert [end.result for end in ends] == ['{"calls":1}', '{"calls":1}']


async def test_invalid_canonical_hook_mutation_becomes_failure_without_original_value() -> None:
    failures = []

    @tool(output_schema={"type": "integer"})
    def number():
        return 1

    class InvalidMutation:
        def on_post_tool_use(self, ctx):
            return HookResult.mutate(tool_output=ToolOutput("wrong type"))

    class Failure:
        def on_post_tool_use_failure(self, ctx):
            failures.append(ctx)

    _, end = await _run(number, hooks=[InvalidMutation(), Failure()])

    assert end.is_error is True
    assert "Tool output invalid:" in end.result
    assert end.tool_output is None
    assert len(failures) == 1


async def test_v2_hooks_see_full_projection_before_final_offload() -> None:
    from linch.filesystem import OffloadConfig, StateFileBackend

    full = "\n".join(f"large output line {index}" for index in range(100))
    seen = []

    @tool(output_schema={"type": "string"})
    def large():
        return full

    class Observe:
        def on_post_tool_use(self, ctx):
            seen.append((ctx.result.content, ctx.tool_output.value))

    registry = ToolRegistry()
    registry.register(large)
    agent = _agent(registry, hooks=[Observe()])
    agent.result_offload = OffloadConfig(threshold_tokens=1, preview_lines=2)
    session = _session()
    session.filesystem = StateFileBackend()

    events = [
        event
        async for event in execute_tool_calls(
            [ToolUseBlock(id="large", name="large", input={})],
            agent,
            session,
            AbortContext(),
        )
    ]
    end = next(event for event in events if isinstance(event, ToolCallEndEvent))

    assert seen == [(full, full)]
    assert "offloaded to" in end.result
    assert end.tool_result is not None and end.tool_result.content == full
    assert end.tool_output == ToolOutput(full)
