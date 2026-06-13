"""Layered permission sources (ROADMAP Phase 4.2).

A ``PermissionRuleSet`` merges ordered named layers (defaults < project < local <
runtime) with policy-wins semantics: a ``deny`` from any layer is final
(deny-override / fail-closed), otherwise the highest-precedence non-abstaining
layer wins. A ``passthrough`` rule lets a layer abstain and defer to the next.

Verify: a project deny overrides a runtime allow.
"""

from __future__ import annotations

from typing import Any

from linch.permissions.engine import PendingToolCall, PermissionEngine
from linch.permissions.rules import ToolRule
from linch.permissions.ruleset import PermissionLayer, PermissionRuleSet


class _Tool:
    def __init__(self, name: str, scope: str = "write") -> None:
        self.name = name
        self.scope = scope

    def validate(self, raw: dict[str, Any]) -> dict[str, Any]:
        return dict(raw)

    def summarize(self, inp: dict[str, Any]) -> str:
        return self.name


def _call(name: str = "Bash", **inp: Any) -> PendingToolCall:
    return PendingToolCall(tool_use_id="t", tool=_Tool(name), input=inp or {"command": "ls"})


def test_project_deny_overrides_runtime_allow() -> None:
    rule_set = PermissionRuleSet(
        [
            PermissionLayer("defaults", []),
            PermissionLayer("project", [ToolRule("Bash", "deny")]),
            PermissionLayer("runtime", [ToolRule("Bash", "allow")]),
        ]
    )
    decision = rule_set.evaluate(_call())
    assert decision is not None
    assert decision.decision == "deny"


def test_higher_precedence_layer_wins_when_no_deny() -> None:
    # No deny anywhere → the last (highest-precedence) opinion wins.
    rule_set = PermissionRuleSet(
        [
            PermissionLayer("defaults", [ToolRule("Bash", "ask")]),
            PermissionLayer("runtime", [ToolRule("Bash", "allow")]),
        ]
    )
    decision = rule_set.evaluate(_call())
    assert decision is not None
    assert decision.decision == "allow"


def test_passthrough_layer_defers_to_next_layer() -> None:
    rule_set = PermissionRuleSet(
        [
            PermissionLayer("project", [ToolRule("Bash", "passthrough")]),
            PermissionLayer("runtime", [ToolRule("Bash", "allow")]),
        ]
    )
    decision = rule_set.evaluate(_call())
    assert decision is not None
    assert decision.decision == "allow"


def test_no_matching_layer_abstains() -> None:
    rule_set = PermissionRuleSet([PermissionLayer("project", [ToolRule("Write", "deny")])])
    assert rule_set.evaluate(_call("Bash")) is None


def test_engine_uses_rule_set_and_default_is_unchanged() -> None:
    tool = _Tool("Bash")
    rule_set = PermissionRuleSet(
        [
            PermissionLayer("project", [ToolRule("Bash", "deny")]),
            PermissionLayer("runtime", [ToolRule("Bash", "allow")]),
        ]
    )
    layered = PermissionEngine(mode="skip-dangerous", rule_set=rule_set)
    call = PendingToolCall(tool_use_id="t", tool=tool, input={"command": "ls"})
    # The layered policy deny wins even though skip-dangerous mode would allow.
    assert layered.evaluate(call).decision == "deny"

    # No rule_set → byte-identical legacy behavior (mode default allows).
    plain = PermissionEngine(mode="skip-dangerous")
    assert plain.evaluate(call).decision == "allow"
