from __future__ import annotations

from typing import Any

import pytest

from linch.permissions.engine import PendingToolCall, PermissionEngine
from linch.permissions.rules import ToolRule
from linch.permissions.ruleset import PermissionLayer, PermissionRuleSet
from tests.support.tools import StubTool


def _call(name: str = "Bash", **inp: Any) -> PendingToolCall:
    return PendingToolCall(
        tool_use_id="t", tool=StubTool(name, scope="write"), input=inp or {"command": "ls"}
    )


@pytest.mark.parametrize(
    ("layers", "expected"),
    [
        (
            [
                PermissionLayer("defaults", []),
                PermissionLayer("project", [ToolRule("Bash", "deny")]),
                PermissionLayer("runtime", [ToolRule("Bash", "allow")]),
            ],
            "deny",
        ),
        (
            [
                PermissionLayer("defaults", [ToolRule("Bash", "ask")]),
                PermissionLayer("runtime", [ToolRule("Bash", "allow")]),
            ],
            "allow",
        ),
        (
            [
                PermissionLayer("project", [ToolRule("Bash", "passthrough")]),
                PermissionLayer("runtime", [ToolRule("Bash", "allow")]),
            ],
            "allow",
        ),
        ([PermissionLayer("project", [ToolRule("Write", "deny")])], None),
    ],
)
def test_layered_decisions(layers: list[PermissionLayer], expected: str | None) -> None:
    decision = PermissionRuleSet(layers).evaluate(_call("Bash"))
    if expected is None:
        assert decision is None
    else:
        assert decision is not None
        assert decision.decision == expected


def test_engine_uses_rule_set_and_default_is_unchanged() -> None:
    call = _call()
    rule_set = PermissionRuleSet(
        [
            PermissionLayer("project", [ToolRule("Bash", "deny")]),
            PermissionLayer("runtime", [ToolRule("Bash", "allow")]),
        ]
    )
    assert (
        PermissionEngine(mode="skip-dangerous", rule_set=rule_set).evaluate(call).decision == "deny"
    )
    assert PermissionEngine(mode="skip-dangerous").evaluate(call).decision == "allow"
