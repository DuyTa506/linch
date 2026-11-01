from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Literal, cast

from ..errors import AbortError
from .principal import Principal
from .rules import (
    BashRule,
    PathRule,
    PermissionRule,
    ToolRule,
    _contains_unparsed_shell_construct,
    _match_unparsed_bash_rule,
    evaluate_bash_rules,
    match_path_rule,
    match_tool_rule,
)

_ToolDecision = Literal["allow", "deny", "ask"]


@dataclass(slots=True)
class PermissionDecision:
    """Represent the policy outcome for one pending tool call.

    Args:
        decision: Whether the call is allowed, denied, or needs approval.
        reason: Optional human-readable explanation for the outcome.
        precomputed_result: Optional result supplied by a pre-execution hook.

    Attributes:
        decision: The policy outcome.
        reason: The explanation shown to callers or the model.
        precomputed_result: A result that can replace execution when present.
    """

    decision: _ToolDecision
    reason: str | None = None
    # Set by a PreToolUse hook that short-circuits execution (e.g. a cache hit):
    # when present, the scheduler returns this result instead of running the
    # tool. Typed Any to avoid coupling permissions to the tools layer.
    precomputed_result: Any = None


@dataclass(slots=True)
class PendingToolCall:
    """Carry the tool invocation context through permission evaluation.

    Args:
        tool_use_id: Provider-issued identifier for the invocation.
        tool: Duck-typed tool object being evaluated.
        input: Model-supplied tool arguments.
        cwd: Effective working directory for path-aware rules.
        principal: Optional identity used by role-aware rules.

    Attributes:
        tool_use_id: Provider-issued invocation identifier.
        tool: The tool object under evaluation.
        input: The validated model input mapping.
        cwd: The effective working directory, when supplied.
        principal: The caller identity, when supplied.
    """

    tool_use_id: str
    tool: Any
    input: dict[str, Any]
    cwd: str | None = None
    principal: Principal | None = None


@dataclass(slots=True)
class CanUseToolRequest:
    """Describe an approval request sent to a host callback.

    Args:
        tool_name: Name of the tool awaiting approval.
        tool_use_id: Provider-issued identifier for the invocation.
        input: Model-supplied tool arguments.
        summary: Human-readable description of the invocation.
        mode: Permission mode that produced the request.
        principal: Optional identity associated with the run.

    Attributes:
        tool_name: Name of the tool awaiting approval.
        tool_use_id: Provider-issued invocation identifier.
        input: The model-supplied arguments.
        summary: Human-readable invocation summary.
        mode: Active permission mode.
        principal: The caller identity, when supplied.
    """

    tool_name: str
    tool_use_id: str
    input: dict[str, Any]
    summary: str
    mode: str
    principal: Principal | None = None


@dataclass(slots=True)
class CanUseToolResponse:
    """Carry an approval callback's allow/deny decision and optional message.

    Args:
        behavior: Callback response behavior, typically ``"allow"`` or ``"deny"``.
        message: Optional explanation accompanying a denial.

    Attributes:
        behavior: The callback's requested behavior.
        message: The optional denial explanation.
    """

    behavior: str
    message: str | None = None


CanUseTool = Callable[[CanUseToolRequest], Any]


_TASK_TOOL_NAMES = frozenset({"TaskCreate", "TaskList", "TaskGet", "TaskUpdate"})


class PermissionEngine:
    """Evaluate and resolve ordered permission rules for tool calls.

    Args:
        mode: Baseline permission mode used when no rule matches.
        rules: Ordered flat rules evaluated after any layered rule set.
        can_use_tool: Optional callback used to resolve an ``ask`` decision.
        project_root: Root used to resolve relative path patterns.
        rule_set: Optional layered policy source evaluated before flat rules.

    Attributes:
        mode: Baseline permission mode.
        rules: Ordered flat permission rules.
        can_use_tool: Optional host approval callback.
        project_root: Root used for path matching.
        rule_set: Optional layered permission policy.
    """

    def __init__(
        self,
        *,
        mode: str = "default",
        rules: list[PermissionRule] | None = None,
        can_use_tool: CanUseTool | None = None,
        project_root: str = "",
        rule_set: Any = None,
    ) -> None:
        """Configure permission evaluation for one agent or run.

        Args:
            mode: Baseline permission mode used when no rule matches.
            rules: Ordered flat rules evaluated after any layered rule set.
            can_use_tool: Optional callback used to resolve an ``ask`` decision.
            project_root: Root used to resolve relative path patterns.
            rule_set: Optional layered policy source evaluated before flat rules.
        """
        self.mode = mode
        self.rules: list[PermissionRule] = list(rules or [])
        self.can_use_tool = can_use_tool
        self.project_root = project_root
        # Opt-in layered policy combiner (PermissionRuleSet). Default None keeps
        # the flat-rule path byte-identical.
        self.rule_set = rule_set

    def evaluate(self, call: PendingToolCall) -> PermissionDecision:
        """Evaluate a pending call without invoking an approval callback.

        Args:
            call: Pending tool invocation to validate against the configured policy.

        Returns:
            The allow, deny, or ask decision for the call.
        """
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
        """Resolve an evaluated call, including any host approval request.

        Args:
            call: Pending tool invocation to evaluate and, if needed, approve.
            signal: Abort signal used while waiting for the approval callback.

        Returns:
            The final allow or deny decision, or an unchanged non-ask decision.

        Raises:
            AbortError: If the approval wait is aborted by ``signal``.
            asyncio.CancelledError: If the surrounding task is cancelled.
        """
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

        if _signal_aborted(signal):
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
            principal=call.principal,
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

        # Linch 1 allowed an approval callback to rewrite the input *after*
        # rules had approved the original call. That is a policy bypass for
        # path/command-aware rules. Linch 2 rejects this shape: callers must use
        # PreToolUse mutation, which is revalidated and then permission-checked.
        if response.get("updatedInput") is not None:
            return PermissionDecision(
                decision="deny",
                reason=(
                    f"Permission denied for {call.tool.name}: updatedInput is unsupported; "
                    "mutate input in PreToolUse before permission evaluation."
                ),
            )
        return PermissionDecision(decision="allow")

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

    Args:
        rules: Ordered permission rules to evaluate.
        call: Pending tool invocation under evaluation.
        project_root: Root used to resolve relative path patterns.

    Returns:
        The first applicable decision, or ``None`` when every rule abstains.
    """
    tool_name = call.tool.name
    for idx, rule in enumerate(rules):
        if (
            isinstance(rule, ToolRule)
            and _rule_applies(rule, call.principal)
            and match_tool_rule(rule, tool_name, call.input)
        ):
            if rule.decision == "allow" and tool_name == "Bash":
                command = call.input.get("command")
                if isinstance(command, str) and _contains_unparsed_shell_construct(command):
                    floor = _unparsed_bash_floor(rules, idx + 1, call.principal, command)
                    if floor is not None:
                        return _rule_outcome(floor, rule.kind, tool_name)
            decision = rule.decision
            if decision == "passthrough":
                continue
            return _rule_outcome(decision, rule.kind, tool_name)
        if (
            isinstance(rule, PathRule)
            and _rule_applies(rule, call.principal)
            and match_path_rule(
                rule,
                tool_name,
                call.input,
                project_root,
                call.cwd,
            )
        ):
            decision = rule.decision
            if decision == "passthrough":
                continue
            return _rule_outcome(decision, rule.kind, tool_name)
        if (
            isinstance(rule, BashRule)
            and _rule_applies(rule, call.principal)
            and tool_name == "Bash"
        ):
            command = call.input.get("command")
            if not isinstance(command, str):
                continue
            bash_rules = _bash_rules_from(rules, idx, call.principal)
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
    if call.principal is not None and not isinstance(call.principal, Principal):
        return "Invalid permission call: principal is invalid."
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


def _bash_rules_from(
    rules: list[PermissionRule], start_index: int, principal: Principal | None
) -> list[BashRule]:
    return [
        rule
        for rule in rules[start_index:]
        if isinstance(rule, BashRule) and _rule_applies(rule, principal)
    ]


def _unparsed_bash_floor(
    rules: list[PermissionRule],
    start_index: int,
    principal: Principal | None,
    command: str,
) -> Literal["allow", "deny", "ask"] | None:
    for rule in rules[start_index:]:
        if not _rule_applies(rule, principal):
            continue
        if isinstance(rule, BashRule) and rule.decision == "deny":
            if _match_unparsed_bash_rule(rule, command.strip()):
                return "deny"
            return "ask"
        if isinstance(rule, ToolRule) and match_tool_rule(rule, "Bash", {"command": command}):
            if rule.decision == "deny":
                return "deny"
            if rule.decision == "ask":
                return "ask"
    return None


_EMPTY_ROLES: frozenset[str] = frozenset()


def _rule_applies(rule: PermissionRule, principal: Principal | None) -> bool:
    if rule.role is None and rule.unless_role is None:
        return True
    roles = principal.roles if principal is not None else _EMPTY_ROLES
    return (rule.role is None or rule.role in roles) and (
        rule.unless_role is None or rule.unless_role not in roles
    )


async def _run_with_abort(coro: Any, signal: Any) -> Any:
    if _signal_aborted(signal):
        raise AbortError("operation aborted")

    if not hasattr(coro, "__await__"):
        return coro

    task: asyncio.Task[Any] = asyncio.ensure_future(coro)
    watcher = asyncio.ensure_future(_wait_for_abort(signal))
    try:
        done, _ = await asyncio.wait([task, watcher], return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            return task.result()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        raise AbortError("operation aborted")
    finally:
        watcher.cancel()
        with suppress(asyncio.CancelledError):
            await watcher
        if not task.done():
            task.cancel()


def _signal_aborted(signal: Any) -> bool:
    if signal is None:
        return False
    aborted = getattr(signal, "aborted", None)
    if isinstance(aborted, bool):
        return aborted
    is_set = getattr(signal, "is_set", None)
    if callable(is_set):
        try:
            return bool(is_set())
        except Exception:
            return True
    return bool(is_set) if isinstance(is_set, bool) else False


async def _wait_for_abort(signal: Any) -> None:
    wait = getattr(signal, "wait", None)
    while not _signal_aborted(signal):
        if callable(wait):
            awaited = wait()
            if hasattr(awaited, "__await__"):
                await cast(Awaitable[Any], awaited)
                # Duck-typed wait() implementations may complete spuriously.
                # Yield before retrying so an immediately-returning waiter
                # cannot starve the permission callback.
                if not _signal_aborted(signal):
                    await asyncio.sleep(0)
                continue
        # Unknown duck-typed signals retain a conservative polling fallback.
        # The public AbortContext and asyncio.Event paths use wait().
        await asyncio.sleep(0.05)
