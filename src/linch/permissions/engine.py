from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from ..errors import AbortError
from .rules import (
    BashRule,
    PathRule,
    PermissionRule,
    ToolRule,
    evaluate_bash_rules,
    match_path_rule,
    match_tool_rule,
)

_ToolDecision = Literal["allow", "deny", "ask"]


@dataclass(slots=True)
class PermissionDecision:
    decision: _ToolDecision
    reason: str | None = None
    updated_input: dict[str, Any] | None = None
    # Set by a PreToolUse hook that short-circuits execution (e.g. a cache hit):
    # when present, the scheduler returns this result instead of running the
    # tool. Typed Any to avoid coupling permissions to the tools layer.
    precomputed_result: Any = None


@dataclass(slots=True)
class PendingToolCall:
    tool_use_id: str
    tool: Any
    input: dict[str, Any]
    cwd: str | None = None


@dataclass(slots=True)
class CanUseToolRequest:
    tool_name: str
    tool_use_id: str
    input: dict[str, Any]
    summary: str
    mode: str


@dataclass(slots=True)
class CanUseToolResponse:
    behavior: str
    updated_input: dict[str, Any] | None = None
    message: str | None = None


CanUseTool = Callable[[CanUseToolRequest], Any]


_TASK_TOOL_NAMES = frozenset({"TaskCreate", "TaskList", "TaskGet", "TaskUpdate"})


class PermissionEngine:
    def __init__(
        self,
        *,
        mode: str = "default",
        rules: list[PermissionRule] | None = None,
        can_use_tool: CanUseTool | None = None,
        project_root: str = "",
        rule_set: Any = None,
    ) -> None:
        self.mode = mode
        self.rules: list[PermissionRule] = list(rules or [])
        self.can_use_tool = can_use_tool
        self.project_root = project_root
        # Opt-in layered policy combiner (PermissionRuleSet). Default None keeps
        # the flat-rule path byte-identical.
        self.rule_set = rule_set

    def evaluate(self, call: PendingToolCall) -> PermissionDecision:
        invalid_reason = _validate_pending_call(call)
        if invalid_reason is not None:
            return PermissionDecision(decision="deny", reason=invalid_reason)

        # Layered sources (deny-override / policy-wins) take precedence; they
        # fall through to the flat rules + mode default only when every layer
        # abstains.
        if self.rule_set is not None:
            layered = self.rule_set.evaluate(call)
            if layered is not None:
                return layered

        rule_decision = self._evaluate_rules(call)
        if rule_decision is not None:
            return rule_decision

        return _mode_default(call.tool, self.mode)

    async def resolve(self, call: PendingToolCall, signal: Any) -> PermissionDecision:
        initial = self.evaluate(call)
        if initial.decision != "ask":
            return initial

        cb = self.can_use_tool
        if cb is None:
            return PermissionDecision(
                decision="deny",
                reason=f"Permission denied for {call.tool.name}: "
                "canUseTool callback is not configured.",
            )

        if getattr(signal, "is_set", None):
            return PermissionDecision(
                decision="deny",
                reason=f"Permission denied for {call.tool.name}: permission request aborted.",
            )

        summary: str = ""
        try:
            summary = call.tool.summarize(call.input)
        except Exception:
            summary = f"{call.tool.name}(...)"

        req = CanUseToolRequest(
            tool_name=call.tool.name,
            tool_use_id=call.tool_use_id,
            input=call.input,
            summary=summary,
            mode=self.mode,
        )

        try:
            response = await _run_with_abort(cb(req), signal)
        except (AbortError, asyncio.CancelledError):
            raise
        except Exception:
            return PermissionDecision(
                decision="deny",
                reason=f"Permission denied for {call.tool.name}: "
                "permission callback failed or aborted.",
            )

        if not isinstance(response, dict):
            return _invalid_callback_response(call.tool.name)

        if response.get("behavior") == "deny":
            if not isinstance(response.get("message"), str):
                return _invalid_callback_response(call.tool.name)
            return PermissionDecision(decision="deny", reason=response["message"])

        if response.get("behavior") != "allow":
            return _invalid_callback_response(call.tool.name)

        updated_input = response.get("updatedInput")
        if updated_input is None:
            return PermissionDecision(decision="allow")

        if not isinstance(updated_input, dict):
            return _invalid_callback_response(call.tool.name)

        try:
            validated = call.tool.validate(updated_input)
        except Exception:
            return PermissionDecision(
                decision="deny",
                reason=f"Permission denied for {call.tool.name}: updated input is invalid.",
            )

        if not isinstance(validated, dict):
            return PermissionDecision(
                decision="deny",
                reason=f"Permission denied for {call.tool.name}: updated input is invalid.",
            )

        return PermissionDecision(decision="allow", updated_input=validated)

    def _evaluate_rules(self, call: PendingToolCall) -> PermissionDecision | None:
        return evaluate_rule_list(self.rules, call, self.project_root)


def _rule_outcome(decision: _ToolDecision, kind: str, tool_name: str) -> PermissionDecision:
    return PermissionDecision(
        decision=decision,
        reason=f"{kind} rule matched {tool_name}." if decision == "deny" else None,
    )


def evaluate_rule_list(
    rules: list[PermissionRule],
    call: PendingToolCall,
    project_root: str,
) -> PermissionDecision | None:
    """First-match decision for one flat rule list, or None when it abstains.

    A matched ``passthrough`` rule abstains: scanning continues past it (so a
    layer can carve out exceptions that defer to the next source) and the list
    yields ``None`` if nothing else matches.
    """
    tool_name = call.tool.name
    for idx, rule in enumerate(rules):
        if isinstance(rule, ToolRule) and match_tool_rule(rule, tool_name, call.input):
            decision = rule.decision
            if decision == "passthrough":
                continue
            return _rule_outcome(decision, rule.kind, tool_name)
        if isinstance(rule, PathRule) and match_path_rule(
            rule,
            tool_name,
            call.input,
            project_root,
            call.cwd,
        ):
            decision = rule.decision
            if decision == "passthrough":
                continue
            return _rule_outcome(decision, rule.kind, tool_name)
        if isinstance(rule, BashRule) and tool_name == "Bash":
            command = call.input.get("command")
            if not isinstance(command, str):
                continue
            bash_rules = _bash_rules_from(rules, idx)
            bash_decision = evaluate_bash_rules(bash_rules, command)
            if bash_decision is not None and bash_decision != "passthrough":
                return _rule_outcome(bash_decision, rule.kind, tool_name)
    return None


def _mode_default(tool: Any, mode: str) -> PermissionDecision:
    if tool.scope == "read" or tool.name in _TASK_TOOL_NAMES:
        return PermissionDecision(decision="allow")
    if mode == "skip-dangerous":
        return PermissionDecision(decision="allow")
    if tool.scope == "write" and mode == "acceptEdits":
        return PermissionDecision(decision="allow")
    return PermissionDecision(decision="ask")


def _validate_pending_call(call: PendingToolCall) -> str | None:
    if not isinstance(call, PendingToolCall):
        return "Invalid permission call: call must be a PendingToolCall."
    if not isinstance(call.tool_use_id, str) or call.tool_use_id.strip() == "":
        return "Invalid permission call: tool_use_id must be a non-empty string."
    if not isinstance(call.input, dict):
        return "Invalid permission call: input must be a dict."
    if not _is_tool_like(call.tool):
        return "Invalid permission call: tool is invalid."
    return None


def _is_tool_like(obj: Any) -> bool:
    return (
        hasattr(obj, "name")
        and hasattr(obj, "scope")
        and hasattr(obj, "validate")
        and hasattr(obj, "summarize")
    )


def _invalid_callback_response(tool_name: str) -> PermissionDecision:
    return PermissionDecision(
        decision="deny",
        reason=f"Permission denied for {tool_name}: "
        "permission callback returned an invalid response.",
    )


def _bash_rules_from(rules: list[PermissionRule], start_index: int) -> list[BashRule]:
    return [r for r in rules[start_index:] if isinstance(r, BashRule)]


async def _run_with_abort(coro: Any, signal: Any) -> Any:
    is_set = getattr(signal, "is_set", False)
    if is_set:
        raise asyncio.CancelledError("aborted")

    if not inspect.isawaitable(coro):
        return coro

    task: asyncio.Task[Any] = asyncio.ensure_future(coro)

    async def _abort_watch() -> None:
        while not task.done():
            if getattr(signal, "is_set", False):
                task.cancel()
                return
            await asyncio.sleep(0.05)

    watcher = asyncio.ensure_future(_abort_watch())
    try:
        done, _ = await asyncio.wait([task, watcher], return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            return task.result()
        raise asyncio.CancelledError("aborted")
    finally:
        watcher.cancel()
        if not task.done():
            task.cancel()
