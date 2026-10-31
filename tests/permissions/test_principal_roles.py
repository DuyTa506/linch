from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import Any

import pytest

from linch.permissions import BashRule, PathRule, Principal, ToolRule
from linch.permissions.engine import PendingToolCall, PermissionEngine
from linch.permissions.ruleset import PermissionLayer, PermissionRuleSet


class _Tool:
    def __init__(self, name: str, scope: str = "write") -> None:
        self.name = name
        self.scope = scope

    def validate(self, raw: dict[str, Any]) -> dict[str, Any]:
        return dict(raw)

    def summarize(self, inp: dict[str, Any]) -> str:
        return self.name


def _call(
    name: str = "Write",
    *,
    input: dict[str, Any] | None = None,
    principal: Principal | None = None,
) -> PendingToolCall:
    return PendingToolCall(
        tool_use_id="t",
        tool=_Tool(name),
        input=input or {"file_path": "notes.txt"},
        principal=principal,
    )


def test_principal_snapshots_roles_and_is_frozen() -> None:
    roles = {"reader"}
    principal = Principal(tenant="tenant-a", user="alice", roles=roles)

    roles.add("admin")
    assert principal.roles == frozenset({"reader"})
    assert isinstance(principal.roles, frozenset)
    with pytest.raises(FrozenInstanceError):
        principal.user = "bob"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("tenant", "user", "roles"),
    [
        ("", "alice", ()),
        (" ", "alice", ()),
        ("tenant-a", "", ()),
        ("tenant-a", " ", ()),
        ("tenant-a", "alice", ("",)),
        ("tenant-a", "alice", (" ",)),
        ("tenant-a", "alice", (1,)),
        ("tenant-a", "alice", "reader"),
        ("tenant-a", "alice", None),
        ("tenant-a", "alice", {"reader": True}),
        (1, "alice", ()),
    ],
)
def test_principal_rejects_invalid_identities(tenant: object, user: object, roles: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        Principal(tenant=tenant, user=user, roles=roles)  # type: ignore[arg-type]


@pytest.mark.parametrize("rule_type", ["tool", "path", "bash"])
@pytest.mark.parametrize("guard_name", ["role", "unless_role"])
def test_rules_reject_blank_role_guards(rule_type: str, guard_name: str) -> None:
    guard = {guard_name: " "}

    with pytest.raises(ValueError):
        if rule_type == "tool":
            ToolRule("Write", "allow", **guard)  # type: ignore[arg-type]
        elif rule_type == "path":
            PathRule(["**"], "allow", ["Write"], **guard)  # type: ignore[arg-type]
        else:
            BashRule("echo *", "allow", **guard)  # type: ignore[arg-type]


def test_tool_rule_role_guard() -> None:
    engine = PermissionEngine(
        mode="skip-dangerous",
        rules=[ToolRule("Write", "deny", role="blocked")],
    )

    assert engine.evaluate(_call(principal=Principal("t", "u", {"blocked"}))).decision == "deny"
    assert engine.evaluate(_call(principal=Principal("t", "u", {"writer"}))).decision == "allow"


def test_path_rule_role_guard() -> None:
    engine = PermissionEngine(
        mode="skip-dangerous",
        rules=[PathRule(["secrets/**"], "deny", ["Write"], role="blocked")],
    )
    call_input = {"file_path": "secrets/token.txt"}

    assert (
        engine.evaluate(
            _call(principal=Principal("t", "u", {"blocked"}), input=call_input)
        ).decision
        == "deny"
    )
    assert (
        engine.evaluate(_call(principal=Principal("t", "u", {"writer"}), input=call_input)).decision
        == "allow"
    )


def test_bash_rule_role_guard() -> None:
    engine = PermissionEngine(
        mode="skip-dangerous",
        rules=[BashRule("rm -rf *", "deny", role="blocked")],
    )
    call_input = {"command": "rm -rf build"}

    assert (
        engine.evaluate(
            _call("Bash", principal=Principal("t", "u", {"blocked"}), input=call_input)
        ).decision
        == "deny"
    )
    assert (
        engine.evaluate(
            _call("Bash", principal=Principal("t", "u", {"operator"}), input=call_input)
        ).decision
        == "allow"
    )


def test_required_and_excluded_roles_are_conjunctive() -> None:
    engine = PermissionEngine(
        mode="skip-dangerous",
        rules=[ToolRule("Write", "deny", role="operator", unless_role="suspended")],
    )

    assert engine.evaluate(_call(principal=Principal("t", "u", {"operator"}))).decision == "deny"
    assert (
        engine.evaluate(_call(principal=Principal("t", "u", {"operator", "suspended"}))).decision
        == "allow"
    )
    assert engine.evaluate(_call(principal=Principal("t", "u", {"writer"}))).decision == "allow"


def test_missing_principal_has_empty_roles() -> None:
    role_rule_engine = PermissionEngine(
        mode="skip-dangerous",
        rules=[ToolRule("Write", "deny", role="admin")],
    )
    negative_rule_engine = PermissionEngine(
        mode="skip-dangerous",
        rules=[ToolRule("Write", "deny", unless_role="trusted")],
    )
    call = _call()

    assert call.principal is None
    assert role_rule_engine.evaluate(call).decision == "allow"
    assert negative_rule_engine.evaluate(call).decision == "deny"


def test_malformed_pending_principal_is_rejected() -> None:
    call = _call()
    call.principal = {"tenant": "t", "user": "u", "roles": ["writer"]}  # type: ignore[assignment]

    decision = PermissionEngine(mode="skip-dangerous").evaluate(call)

    assert decision.decision == "deny"
    assert decision.reason == "Invalid permission call: principal is invalid."


def test_bash_composite_role_specific_deny_cannot_be_bypassed() -> None:
    engine = PermissionEngine(
        mode="skip-dangerous",
        rules=[
            BashRule("echo *", "allow", role="runner"),
            BashRule("rm -rf *", "deny", role="runner"),
        ],
    )
    call = _call(
        "Bash",
        input={"command": "echo safe; rm -rf build"},
        principal=Principal("t", "u", {"runner"}),
    )

    assert engine.evaluate(call).decision == "deny"


def test_bash_composite_deny_for_other_role_is_filtered() -> None:
    engine = PermissionEngine(
        mode="skip-dangerous",
        rules=[
            BashRule("echo *", "allow", role="runner"),
            BashRule("rm -rf *", "deny", role="admin"),
        ],
    )
    call = _call(
        "Bash",
        input={"command": "echo safe; rm -rf build"},
        principal=Principal("t", "u", {"runner"}),
    )

    assert engine.evaluate(call).decision == "allow"


def test_layered_policy_role_deny_overrides_runtime_allow() -> None:
    rule_set = PermissionRuleSet(
        [
            PermissionLayer("project", [ToolRule("Bash", "deny", role="blocked")]),
            PermissionLayer("runtime", [ToolRule("Bash", "allow")]),
        ]
    )
    engine = PermissionEngine(mode="skip-dangerous", rule_set=rule_set)
    call = _call(
        "Bash",
        input={"command": "ls"},
        principal=Principal("t", "u", {"blocked"}),
    )

    assert engine.evaluate(call).decision == "deny"


async def test_async_resolve_honors_role_conditioned_rule() -> None:
    seen: list[str] = []
    principal = Principal("t", "u", {"editor"})
    seen_principals: list[Principal | None] = []

    async def approve(request: Any) -> dict[str, str]:
        seen.append(request.tool_name)
        seen_principals.append(request.principal)
        return {"behavior": "allow"}

    engine = PermissionEngine(
        mode="default",
        rules=[ToolRule("Write", "ask", role="editor")],
        can_use_tool=approve,
    )
    call = _call(principal=principal)

    decision = await engine.resolve(call, None)

    assert decision.decision == "allow"
    assert seen == ["Write"]
    assert seen_principals == [principal]
