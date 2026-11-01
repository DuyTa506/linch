"""Layered permission sources with policy-wins (deny-override) semantics.

A :class:`PermissionRuleSet` is an ordered stack of named :class:`PermissionLayer`
sources — the canonical order is ``defaults < project < local < runtime`` (least
to most specific). Each layer is an independent flat rule list evaluated
first-match (a ``passthrough`` rule lets a layer abstain). The layers combine
with **deny-override**: a ``deny`` from *any* layer is final (fail-closed),
otherwise the highest-precedence (last) non-abstaining layer wins. ``None`` means
every layer abstained, so the caller falls back to its own default.

This is a pure mechanism: which sources exist and what they contain is embedder
policy. The engine consumes a rule set only when one is supplied
(``PermissionEngine(rule_set=...)``); with the default ``None`` the flat-rule
path is byte-identical.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .engine import PendingToolCall, PermissionDecision, _rule_applies, evaluate_rule_list
from .rules import (
    BashRule,
    PermissionRule,
    ToolRule,
    _contains_unparsed_shell_construct,
    _match_unparsed_bash_rule,
    match_tool_rule,
)


@dataclass(slots=True)
class PermissionLayer:
    """Name one ordered source of permission rules.

    Args:
        name: Stable label for the policy source.
        rules: Flat rules evaluated in first-match order within this layer.

    Attributes:
        name: The layer's policy-source label.
        rules: The layer's ordered rules.
    """

    name: str
    rules: list[PermissionRule] = field(default_factory=list)


class PermissionRuleSet:
    """Combine named permission layers with deny-override semantics.

    Args:
        layers: Ordered layers from least to most specific.
        project_root: Root used to resolve relative path patterns.

    Attributes:
        layers: A copy of the ordered policy layers.
        project_root: Root used for path matching.
    """

    def __init__(self, layers: list[PermissionLayer], *, project_root: str = "") -> None:
        """Build a layered permission policy.

        Args:
            layers: Ordered layers from least to most specific.
            project_root: Root used to resolve relative path patterns.
        """
        self.layers = list(layers)
        self.project_root = project_root

    def evaluate(self, call: PendingToolCall) -> PermissionDecision | None:
        """Evaluate one call across all layers.

        Args:
            call: Pending tool invocation to evaluate.

        Returns:
            A deny decision when any layer denies, otherwise the highest-precedence
            non-abstaining decision, or ``None`` when every layer abstains.
        """
        unparsed_floor = _unparsed_bash_deny_floor(call, self.layers)
        if unparsed_floor is not None:
            return unparsed_floor

        opinions: list[PermissionDecision] = []
        for layer in self.layers:
            decision = evaluate_rule_list(layer.rules, call, self.project_root)
            if decision is None:
                continue
            if decision.decision == "deny":
                return decision  # deny-override: a policy deny is final.
            opinions.append(decision)
        return opinions[-1] if opinions else None


def _unparsed_bash_deny_floor(
    call: PendingToolCall, layers: list[PermissionLayer]
) -> PermissionDecision | None:
    if call.tool.name != "Bash":
        return None
    command = call.input.get("command")
    if not isinstance(command, str) or not _contains_unparsed_shell_construct(command):
        return None

    saw_bash_deny = False
    for layer in layers:
        for rule in layer.rules:
            if not _rule_applies(rule, call.principal):
                continue
            if isinstance(rule, ToolRule) and match_tool_rule(rule, "Bash", call.input):
                if rule.decision == "deny":
                    return PermissionDecision(decision="deny", reason="tool rule matched Bash.")
                continue
            if isinstance(rule, BashRule) and rule.decision == "deny":
                saw_bash_deny = True
                if _match_unparsed_bash_rule(rule, command.strip()):
                    return PermissionDecision(decision="deny", reason="bash rule matched Bash.")
    return PermissionDecision(decision="ask") if saw_bash_deny else None
