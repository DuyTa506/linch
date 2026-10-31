from __future__ import annotations

import inspect
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from typing import Any

from .. import lifecycle
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

    async def dispatch(
        self, event: lifecycle.LifecyclePoint | HookEvent | str, ctx: HookContext
    ) -> HookDispatchResult:
        """Run hooks for one lifecycle point while preserving hook ordering.

        Args:
            event: Catalog point or hook name.
            ctx: Mutable context passed to matching hooks.

        Returns:
            The combined hook action, telemetry events, and final context.
        """
        if isinstance(event, lifecycle.LifecyclePoint):
            if event.mode != "serial":
                raise ValueError(
                    f"hook lifecycle point {event.name!r} uses {event.mode} dispatch; "
                    "HookDispatcher requires serial"
                )
            event_value = event.name
        else:
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
                    "hook %r raised at %s; isolating and continuing",
                    name,
                    event_value,
                    exc_info=exc,
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
    point.name: "on_" + re.sub(r"(?<!^)(?=[A-Z])", "_", point.name).lower()
    for point in lifecycle.LIFECYCLE_POINTS
    if isinstance(point._payload_ref, str)
    and point._payload_ref.startswith("linch.hooks.contexts.")
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
        tool_result=(
            right.tool_result
            if right.tool_result is not None
            else (None if right.tool_output is not None else left.tool_result)
        ),
        tool_output=(
            right.tool_output
            if right.tool_output is not None
            else (None if right.tool_result is not None else left.tool_output)
        ),
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
            updates["tool_output"] = None
        elif result.tool_output is not None:
            updates["tool_output"] = result.tool_output
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
