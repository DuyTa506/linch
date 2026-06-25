"""Hook package extension template.

Hooks are plain objects with ``on_<event>`` methods. Return ``HookResult`` only
when you need to block, mutate, retry, stop, or force continuation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from linch.hooks import BeforeFinalAnswerContext, EventEmitContext, HookResult, PreToolUseContext


@dataclass(slots=True)
class TemplateAuditHook:
    """Small hook that records events and blocks selected tools."""

    blocked_tools: set[str] = field(default_factory=set)
    seen_events: list[str] = field(default_factory=list)

    name = "template_audit"

    def on_event_emit(self, ctx: EventEmitContext) -> None:
        event_type = getattr(ctx.event, "type", None)
        if isinstance(event_type, str):
            self.seen_events.append(event_type)

    def on_pre_tool_use(self, ctx: PreToolUseContext) -> HookResult | None:
        if ctx.tool_name in self.blocked_tools:
            return HookResult.block(f"{ctx.tool_name} is blocked by TemplateAuditHook")
        return None

    def on_before_final_answer(self, ctx: BeforeFinalAnswerContext) -> HookResult | None:
        if ctx.final_text is None:
            return None
        text = ctx.final_text.strip()
        if text != ctx.final_text:
            return HookResult.mutate(final_text=text)
        return None


def build_hooks(*, blocked_tools: set[str] | None = None) -> list[Any]:
    return [TemplateAuditHook(blocked_tools=blocked_tools or set())]
