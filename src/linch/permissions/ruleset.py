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

from .engine import PendingToolCall, PermissionDecision, evaluate_rule_list
from .rules import PermissionRule


@dataclass(slots=True)
class PermissionLayer:
    name: str
    rules: list[PermissionRule] = field(default_factory=list)


class PermissionRuleSet:
    def __init__(self, layers: list[PermissionLayer], *, project_root: str = "") -> None:
        self.layers = list(layers)
        self.project_root = project_root

    def evaluate(self, call: PendingToolCall) -> PermissionDecision | None:
        opinions: list[PermissionDecision] = []
        for layer in self.layers:
            decision = evaluate_rule_list(layer.rules, call, self.project_root)
            if decision is None:
                continue
            if decision.decision == "deny":
                return decision  # deny-override: a policy deny is final.
            opinions.append(decision)
        return opinions[-1] if opinions else None
