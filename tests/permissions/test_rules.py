from __future__ import annotations

from pathlib import Path

import pytest

from linch.permissions.engine import PendingToolCall, PermissionEngine
from linch.permissions.principal import Principal
from linch.permissions.rules import (
    BashRule,
    PathRule,
    ToolRule,
    match_bash_rule,
    match_path_rule,
    match_tool_rule,
)
from linch.permissions.ruleset import PermissionLayer, PermissionRuleSet
from tests.support.tools import StubTool


@pytest.mark.parametrize(
    ("pattern", "action", "command", "matches"),
    [
        ("rm -rf /tmp", "allow", "rm -rf /tmp", True),
        ("rm -rf /tmp", "allow", "rm -rf /tmp/../etc", False),
        ("ls", "allow", "ls", True),
        ("ls", "allow", "lsof -i :8080", False),
        ("npm install", "allow", "npm install lodash", True),
        ("git *", "allow", "git status", True),
        ("rm -rf /", "deny", "rm -rf /", True),
    ],
)
def test_bash_rule_matching(pattern: str, action: str, command: str, matches: bool) -> None:
    assert match_bash_rule(BashRule(pattern, action), command) is matches


@pytest.mark.parametrize(
    ("pattern", "matches", "nonmatches"),
    [
        ("config/*.json", ["config/app.json"], ["config/prod/secrets.json"]),
        ("config/**/*.json", ["config/prod/secrets.json", "config/app.json"], []),
    ],
)
def test_path_rule_matching(pattern: str, matches: list[str], nonmatches: list[str]) -> None:
    rule = PathRule([pattern], "allow", ["Write"])
    for path in matches:
        assert match_path_rule(rule, "Write", {"file_path": path}, "/project", "/project")
    for path in nonmatches:
        assert not match_path_rule(rule, "Write", {"file_path": path}, "/project", "/project")


def test_path_rule_resolves_parent_segments_before_matching(tmp_path: Path) -> None:
    (tmp_path / "secrets").mkdir()
    rule = PathRule(["secrets/**"], "deny", ["Write"])

    assert match_path_rule(
        rule,
        "Write",
        {"file_path": "src/../secrets/token.txt"},
        str(tmp_path),
        str(tmp_path),
    )


def test_path_rule_resolves_symlinks_before_matching(tmp_path: Path) -> None:
    (tmp_path / "secrets").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "secrets", target_is_directory=True)
    rule = PathRule(["secrets/**"], "deny", ["Write"])

    assert match_path_rule(
        rule,
        "Write",
        {"file_path": "link/token.txt"},
        str(tmp_path),
        str(tmp_path),
    )


def test_path_rule_resolves_symlinked_rule_pattern(tmp_path: Path) -> None:
    (tmp_path / "secrets").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "secrets", target_is_directory=True)
    rule = PathRule(["link/**"], "deny", ["Write"])

    assert match_path_rule(
        rule,
        "Write",
        {"file_path": "secrets/token.txt"},
        str(tmp_path),
        str(tmp_path),
    )


@pytest.mark.parametrize(
    ("tool_name", "input_key"),
    [("write_file", "path"), ("edit_file", "path")],
)
def test_path_rule_covers_virtual_filesystem_writes(
    tmp_path: Path, tool_name: str, input_key: str
) -> None:
    (tmp_path / "secrets").mkdir()
    rule = PathRule(["secrets/**"], "deny")

    assert match_path_rule(
        rule,
        tool_name,
        {input_key: "/secrets/deploy.key"},
        str(tmp_path),
        str(tmp_path),
    )


def test_skill_tool_rule_matches_the_skill_tool_normalization() -> None:
    rule = ToolRule("Skill", "deny", arg="deploy-prod")

    assert match_tool_rule(rule, "Skill", {"skill": "/deploy-prod"})


def test_mcp_server_tool_rule_matches_only_its_literal_prefix() -> None:
    rule = ToolRule("mcp__ops_prod__*", "deny")

    assert match_tool_rule(rule, "mcp__ops_prod__deploy", {})
    assert not match_tool_rule(rule, "mcp__ops_prod_evil__deploy", {})
    assert not match_tool_rule(ToolRule("Bash*", "deny"), "Bash", {})
    assert not match_tool_rule(ToolRule("mcp____*", "deny"), "mcp____deploy", {})
    assert not match_tool_rule(ToolRule("mcp__*__*", "deny"), "mcp__ops__deploy", {})


def _bash_call(command: str) -> PendingToolCall:
    return PendingToolCall(
        tool_use_id="bash-test",
        tool=StubTool("Bash", scope="exec"),
        input={"command": command},
    )


_SUPPORTED_SHELL_COMMANDS = [
    "pytest -q 2>&1",
    "pytest -q > test.log",
    "pytest -q >> test.log",
    "cat < input.txt",
    "cmd &> output.log",
    "git status | cat",
    "git diff | less",
    "ruff check . | tee ruff.log",
    "python -m pytest | tail -n 20",
    "npm test 2>&1 | tee npm.log",
    "uv run pytest -q",
    "git log --oneline | head",
    "find . -type f | sort",
    "grep -R TODO src | wc -l",
    "docker compose ps",
    "cargo test --all-features 2>&1",
    "make test > make.log",
    "python script.py < input.txt > output.txt",
    "sed -n '1,10p' README.md | cat",
    "go test ./... | tee go.log",
    "pytest -q &>pytest.log",
    "cat input.txt 2>&1 | grep error",
    "echo ready > status.txt",
    "tail -f app.log 2>/dev/null",
    "sort < names.txt > sorted.txt",
    "printf '&'",
    "printf '$(literal)'",
    "printf '\\&'",
]


@pytest.mark.parametrize("command", _SUPPORTED_SHELL_COMMANDS)
def test_bash_allow_rule_keeps_supported_shell_syntax(command: str) -> None:
    assert (
        PermissionEngine(mode="default", rules=[BashRule("*", "allow")])
        .evaluate(_bash_call(command))
        .decision
        == "allow"
    )


_UNPARSED_SHELL_COMMANDS = [
    "true & rm -rf build",
    "echo ok\ntouch pwned",
    "echo ok \\\nrm -rf build",
    "echo $(rm -rf build)",
    "echo `rm -rf build`",
    "cat <(secret-command)",
    "cat >(write-command)",
    "echo ok & wait",
    "pytest -q\nrm -rf build",
    "echo $(cat secrets.txt)",
    "printf '%s' `cat token.txt`",
]


@pytest.mark.parametrize("command", _UNPARSED_SHELL_COMMANDS)
def test_bash_allow_rule_does_not_auto_allow_unparsed_syntax(command: str) -> None:
    assert (
        PermissionEngine(mode="skip-dangerous", rules=[BashRule("*", "allow")])
        .evaluate(_bash_call(command))
        .decision
        == "ask"
    )


@pytest.mark.parametrize("command", _UNPARSED_SHELL_COMMANDS)
def test_bash_deny_rule_asks_for_unparsed_syntax_in_skip_dangerous(command: str) -> None:
    assert (
        PermissionEngine(mode="skip-dangerous", rules=[BashRule("rm -rf *", "deny")])
        .evaluate(_bash_call(command))
        .decision
        == "ask"
    )


def test_generic_bash_allow_cannot_bypass_later_unparsed_deny_rule() -> None:
    engine = PermissionEngine(
        mode="skip-dangerous",
        rules=[ToolRule("Bash", "allow"), BashRule("rm -rf *", "deny")],
    )

    assert engine.evaluate(_bash_call("true & rm -rf build")).decision == "ask"


def test_layered_generic_bash_allow_cannot_override_unparsed_deny_floor() -> None:
    engine = PermissionEngine(
        mode="skip-dangerous",
        rule_set=PermissionRuleSet(
            [
                PermissionLayer("project", [BashRule("rm -rf *", "deny")]),
                PermissionLayer("runtime", [ToolRule("Bash", "allow")]),
            ]
        ),
    )

    assert engine.evaluate(_bash_call("true & rm -rf build")).decision == "ask"


def test_unparsed_bash_floor_ignores_tool_rule_skill_arguments() -> None:
    rules = [
        ToolRule("Bash", "allow"),
        ToolRule("Bash", "deny", arg="deploy-prod"),
    ]
    flat = PermissionEngine(mode="skip-dangerous", rules=rules)
    layered = PermissionEngine(
        mode="skip-dangerous",
        rule_set=PermissionRuleSet([PermissionLayer("runtime", rules)]),
    )

    command = _bash_call("true & rm -rf build")
    assert flat.evaluate(command).decision == "allow"
    assert layered.evaluate(command).decision == "allow"


@pytest.mark.parametrize(
    ("guard", "roles"),
    [({"role": "blocked"}, {"reader"}), ({"unless_role": "trusted"}, {"trusted"})],
)
def test_unparsed_bash_floor_honors_inapplicable_role_guards(
    guard: dict[str, str], roles: set[str]
) -> None:
    rules = [
        ToolRule("Bash", "allow"),
        BashRule("rm -rf *", "deny", **guard),
    ]
    call = _bash_call("true & rm -rf build")
    call.principal = Principal("tenant", "user", roles)

    assert PermissionEngine(mode="skip-dangerous", rules=rules).evaluate(call).decision == "allow"
    assert (
        PermissionEngine(
            mode="skip-dangerous",
            rule_set=PermissionRuleSet([PermissionLayer("runtime", rules)]),
        )
        .evaluate(call)
        .decision
        == "allow"
    )
