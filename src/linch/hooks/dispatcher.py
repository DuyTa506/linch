from __future__ import annotations

import inspect
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from typing import Any

from .contexts import (
    AfterProviderCallContext,
    AgentStartContext,
    AgentStopContext,
    BeforeFinalAnswerContext,
    BeforeProviderCallContext,
    EventEmitContext,
    HookContext,
    PostCompactContext,
    PostToolUseContext,
    PostToolUseFailureContext,
    PreCompactContext,
    PreToolUseContext,
    ProviderCallStartContext,
    ProviderCallStopContext,
    StopContext,
    ToolUseStartContext,
    ToolUseStopContext,
    TurnStartContext,
    TurnStopContext,
    UserPromptSubmitContext,
)
from .types import HookEvent, HookResult

_log = logging.getLogger("linch.observability")


@dataclass(slots=True)
class HookDispatchResult:
    result: HookResult = field(default_factory=HookResult.continue_)
    events: list[Any] = field(default_factory=list)
    # The context after all mutations were applied — lets callers read the final
    # (possibly mutated) fields even when a later hook short-circuits (e.g. a
    # cache `resolve` that follows an `input` mutation).
    context: Any = None


class HookDispatcher:
    def __init__(self, hooks: Iterable[Any] | None = None) -> None:
        self._hooks = list(hooks or [])

    @property
    def active(self) -> bool:
        return bool(self._hooks)

    async def dispatch(self, event: HookEvent | str, ctx: HookContext) -> HookDispatchResult:
        event_value = event.value if isinstance(event, HookEvent) else str(event)
        telemetry: list[Any] = []
        current = HookResult.continue_()
        for hook in self._hooks:
            fn = self._hook_fn(hook, event_value)
            if fn is None:
                continue
            name = str(getattr(hook, "name", hook.__class__.__name__))
            try:
                raw = fn(ctx)
                if inspect.isawaitable(raw):
                    raw = await raw
            except Exception as exc:
                # Isolate the failing hook. The error is recorded as telemetry,
                # but not every chokepoint forwards telemetry to the event
                # stream (lifecycle notifications like AgentStart/TurnStart do
                # not), so also log it — otherwise a hook raising there would be
                # swallowed silently.
                _log.warning(
                    "hook %r raised at %s; isolating and continuing: %s", name, event_value, exc
                )
                telemetry.append(_hook_event(event_value, name, "error", str(exc)))
                continue
            if raw is None:
                # A no-op hook emits no telemetry: a default-on hook (e.g.
                # read_before_write) fires on every tool call, so emitting a
                # "continue" record here would flood the event stream with noise.
                continue
            if not isinstance(raw, HookResult):
                telemetry.append(
                    _hook_event(
                        event_value,
                        name,
                        "error",
                        f"expected HookResult or None, got {type(raw).__name__}",
                    )
                )
                continue
            telemetry.append(_hook_event(event_value, name, raw.action, raw.reason))
            extra_events = raw.metadata.get("events")
            if isinstance(extra_events, list):
                telemetry.extend(extra_events)
            if raw.action in {"continue"}:
                continue
            if raw.action == "mutate":
                ctx = _apply_mutation(ctx, raw)
                current = _combine_mutation(current, raw)
                continue
            current = raw
            if raw.action in {"block", "retry", "stop", "force_continue", "resolve"}:
                break
        return HookDispatchResult(result=current, events=telemetry, context=ctx)

    def _hook_fn(self, hook: Any, event_value: str) -> Any:
        generic = getattr(hook, "on_hook", None)
        if callable(generic):
            return lambda ctx: generic(event_value, ctx)
        method = _METHODS.get(event_value)
        if method is None:
            return None
        fn = getattr(hook, method, None)
        return fn if callable(fn) else None


_METHODS = {
    HookEvent.AGENT_START.value: "on_agent_start",
    HookEvent.AGENT_STOP.value: "on_agent_stop",
    HookEvent.USER_PROMPT_SUBMIT.value: "on_user_prompt_submit",
    HookEvent.TURN_START.value: "on_turn_start",
    HookEvent.TURN_STOP.value: "on_turn_stop",
    HookEvent.BEFORE_PROVIDER_CALL.value: "on_before_provider_call",
    HookEvent.PROVIDER_CALL_START.value: "on_provider_call_start",
    HookEvent.PROVIDER_CALL_STOP.value: "on_provider_call_stop",
    HookEvent.AFTER_PROVIDER_CALL.value: "on_after_provider_call",
    HookEvent.PRE_TOOL_USE.value: "on_pre_tool_use",
    HookEvent.TOOL_USE_START.value: "on_tool_use_start",
    HookEvent.TOOL_USE_STOP.value: "on_tool_use_stop",
    HookEvent.POST_TOOL_USE.value: "on_post_tool_use",
    HookEvent.POST_TOOL_USE_FAILURE.value: "on_post_tool_use_failure",
    HookEvent.PRE_COMPACT.value: "on_pre_compact",
    HookEvent.POST_COMPACT.value: "on_post_compact",
    HookEvent.BEFORE_FINAL_ANSWER.value: "on_before_final_answer",
    HookEvent.STOP.value: "on_stop",
    HookEvent.SUBAGENT_START.value: "on_subagent_start",
    HookEvent.SUBAGENT_STOP.value: "on_subagent_stop",
    HookEvent.EVENT_EMIT.value: "on_event_emit",
}


def normalize_hooks(raw: Any) -> list[Any]:
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return list(raw)
    return [raw]


def _hook_event(event: str, hook: str, action: str, reason: str) -> Any:
    from ..events import HookEventRecord

    return HookEventRecord(event=event, hook=hook, action=action, reason=reason)


def _combine_mutation(left: HookResult, right: HookResult) -> HookResult:
    if left.action != "mutate":
        return right
    return HookResult.mutate(
        prompt=right.prompt if right.prompt is not None else left.prompt,
        images=right.images if right.images is not None else left.images,
        request=right.request if right.request is not None else left.request,
        assembly=right.assembly if right.assembly is not None else left.assembly,
        input=right.input if right.input is not None else left.input,
        tool_result=right.tool_result if right.tool_result is not None else left.tool_result,
        final_text=right.final_text if right.final_text is not None else left.final_text,
        structured_output=(
            right.structured_output
            if right.structured_output is not None
            else left.structured_output
        ),
        structured_error=(
            right.structured_error if right.structured_error is not None else left.structured_error
        ),
        result_event=right.result_event if right.result_event is not None else left.result_event,
        metadata={**left.metadata, **right.metadata},
    )


def _apply_mutation(ctx: HookContext, result: HookResult) -> HookContext:
    updates: dict[str, Any] = {}
    if isinstance(ctx, UserPromptSubmitContext):
        if result.prompt is not None:
            updates["prompt"] = result.prompt
        if result.images is not None:
            updates["images"] = result.images
    elif isinstance(ctx, BeforeProviderCallContext):
        if result.request is not None:
            updates["request"] = result.request
    elif isinstance(ctx, AfterProviderCallContext):
        if result.assembly is not None:
            updates["assembly"] = result.assembly
    elif isinstance(ctx, PreToolUseContext):
        if result.input is not None:
            updates["input"] = result.input
    elif isinstance(ctx, PostToolUseContext):
        if result.tool_result is not None:
            updates["result"] = result.tool_result
    elif isinstance(ctx, BeforeFinalAnswerContext):
        if result.final_text is not None:
            updates["final_text"] = result.final_text
        if result.structured_output is not None:
            updates["structured_output"] = result.structured_output
        if result.structured_error is not None:
            updates["structured_error"] = result.structured_error
    elif isinstance(ctx, StopContext):
        if result.result_event is not None:
            updates["result_event"] = result.result_event
    elif isinstance(
        ctx,
        (
            AgentStartContext,
            AgentStopContext,
            TurnStartContext,
            TurnStopContext,
            ProviderCallStartContext,
            ProviderCallStopContext,
            ToolUseStartContext,
            ToolUseStopContext,
            PostToolUseFailureContext,
            PreCompactContext,
            PostCompactContext,
            EventEmitContext,
        ),
    ):
        pass
    return replace(ctx, **updates) if updates else ctx
