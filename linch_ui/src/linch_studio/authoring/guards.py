"""Fields that AI proposals may review but may not change."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from linch_studio.spec import Blueprint, Diagnostic, canonical_data, json_path, sort_diagnostics


def manual_only_diagnostics(
    current: Blueprint,
    candidate: Blueprint,
) -> tuple[Diagnostic, ...]:
    """Return fixed, value-safe diagnostics for protected authoring changes.

    Manual editing remains able to configure these fields. The AI authoring
    boundary is narrower because regular expressions, local executable command
    lines, and broad execution permissions require deliberate human review.
    """

    before_spec = canonical_data(current)["spec"]
    after_spec = canonical_data(candidate)["spec"]
    before = before_spec["capabilities"]
    after = after_spec["capabilities"]
    findings: list[Diagnostic] = []

    before_redaction = before["hooks"]["redactionRules"]
    after_redaction = after["hooks"]["redactionRules"]
    if before_redaction != after_redaction:
        findings.append(
            _manual_only(
                json_path("spec", "capabilities", "hooks", "redactionRules"),
                "AI proposals cannot change redaction regular expressions.",
                "Configure and review redaction rules manually in Advanced settings.",
            )
        )

    _check_mcp_commands(before, after, findings)
    _check_permissions(current, candidate, before, after, findings)
    _check_executable_verifiers(before_spec, after_spec, findings)
    return sort_diagnostics(findings)


def _check_executable_verifiers(
    before_spec: Mapping[str, Any],
    after_spec: Mapping[str, Any],
    findings: list[Diagnostic],
) -> None:
    """Reject AI-authored verifier entry points while allowing declarative gates."""

    before = _verifiers(before_spec)
    after = _verifiers(after_spec)
    if before == after:
        return
    forbidden_keys = {"callable", "code", "command", "entrypoint", "module", "path", "script"}
    unsafe_kinds = {"callable", "custom", "python", "shell"}
    for verifier in after:
        if not isinstance(verifier, Mapping):
            continue
        keys = {str(key).casefold() for key in verifier}
        kind = str(verifier.get("kind", "")).casefold()
        if keys & forbidden_keys or kind in unsafe_kinds:
            findings.append(
                _manual_only(
                    json_path(
                        "spec",
                        "runtime",
                        "agent",
                        "completion",
                        "verifiers",
                    ),
                    "AI proposals cannot add executable verifier implementations.",
                    "Select text_contains, json_schema, or a blocking custom_todo seam; "
                    "implement project code manually after review.",
                )
            )
            return


def _verifiers(spec: Mapping[str, Any]) -> list[Any]:
    runtime = spec.get("runtime")
    if not isinstance(runtime, Mapping):
        return []
    agent = runtime.get("agent")
    if not isinstance(agent, Mapping):
        return []
    completion = agent.get("completion")
    if not isinstance(completion, Mapping):
        return []
    verifiers = completion.get("verifiers")
    return verifiers if isinstance(verifiers, list) else []


def _check_mcp_commands(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    findings: list[Diagnostic],
) -> None:
    before_servers = before["extensions"]["mcpServers"]
    after_servers = after["extensions"]["mcpServers"]
    before_by_id = {item["id"]: (index, item) for index, item in enumerate(before_servers)}
    after_by_id = {item["id"]: (index, item) for index, item in enumerate(after_servers)}

    for server_id in sorted(set(before_by_id) | set(after_by_id)):
        old_entry = before_by_id.get(server_id)
        new_entry = after_by_id.get(server_id)
        old = old_entry[1] if old_entry is not None else None
        new = new_entry[1] if new_entry is not None else None
        old_stdio = old if old is not None and old.get("kind") == "stdio" else None
        new_stdio = new if new is not None and new.get("kind") == "stdio" else None
        if old_stdio is None and new_stdio is None:
            continue
        index = new_entry[0] if new_entry is not None else old_entry[0]  # type: ignore[index]
        old_command = old_stdio.get("command") if old_stdio is not None else None
        new_command = new_stdio.get("command") if new_stdio is not None else None
        if old_command != new_command:
            findings.append(
                _manual_only(
                    json_path(
                        "spec",
                        "capabilities",
                        "extensions",
                        "mcpServers",
                        index,
                        "command",
                    ),
                    "AI proposals cannot change local MCP executable commands.",
                    "Configure and review the stdio command manually in Advanced settings.",
                )
            )
        old_args = old_stdio.get("args") if old_stdio is not None else None
        new_args = new_stdio.get("args") if new_stdio is not None else None
        if old_args != new_args:
            findings.append(
                _manual_only(
                    json_path(
                        "spec",
                        "capabilities",
                        "extensions",
                        "mcpServers",
                        index,
                        "args",
                    ),
                    "AI proposals cannot change local MCP command arguments.",
                    "Configure and review stdio arguments manually in Advanced settings.",
                )
            )


def _check_permissions(
    current: Blueprint,
    candidate: Blueprint,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    findings: list[Diagnostic],
) -> None:
    before_permissions = before["permissions"]
    after_permissions = after["permissions"]
    before_mode = before_permissions["mode"]
    after_mode = after_permissions["mode"]
    if before_mode != after_mode and "trusted" in {before_mode, after_mode}:
        findings.append(
            _manual_only(
                json_path("spec", "capabilities", "permissions", "mode"),
                "AI proposals cannot enable, disable, or otherwise change trusted mode.",
                "Change trusted mode manually after reviewing its unrestricted fallback behavior.",
            )
        )

    before_dangerous = _dangerous_rules(current, before_permissions["rules"])
    after_dangerous = _dangerous_rules(candidate, after_permissions["rules"])
    if before_dangerous != after_dangerous:
        findings.append(
            _manual_only(
                json_path("spec", "capabilities", "permissions", "rules"),
                "AI proposals cannot change rules that allow write or execution behavior.",
                "Create or modify broad allow rules manually after reviewing their scope.",
            )
        )


def _dangerous_rules(blueprint: Blueprint, rules: list[dict[str, Any]]) -> tuple[str, ...]:
    dangerous_tools = {
        tool.id.casefold() for tool in blueprint.spec.tools if tool.scope in {"write", "exec"}
    }
    dangerous_tools.update({"*", "bash", "edit", "write"})
    selected: list[str] = []
    for rule in rules:
        if rule.get("decision") != "allow":
            continue
        kind = rule.get("kind")
        dangerous = kind == "bash"
        if kind == "path":
            named_tools = {str(item).casefold() for item in rule.get("tools", [])}
            dangerous = not named_tools or bool(named_tools & dangerous_tools)
        if kind == "tool":
            dangerous = str(rule.get("tool", "")).casefold() in dangerous_tools
        if dangerous:
            selected.append(
                json.dumps(rule, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            )
    return tuple(selected)


def _manual_only(path: str, message: str, remediation: str) -> Diagnostic:
    return Diagnostic(
        code="authoring.manual_only_field",
        severity="error",
        path=path,
        message=message,
        remediation=remediation,
    )


__all__ = ["manual_only_diagnostics"]
