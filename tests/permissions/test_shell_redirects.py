"""An allow rule does not cover a command that redirects output to a file.

The cases are those of the Rust kernel's permission tests
(`linch-rust/crates/linch-core/src/permissions/tests.rs`).
"""

from __future__ import annotations

import pytest

from linch.permissions.engine import PendingToolCall, PermissionEngine
from linch.permissions.rules import BashRule, PermissionRule
from tests.support.tools import StubTool


def _bash(rules: list[PermissionRule], command: str, mode: str = "default") -> str:
    call = PendingToolCall(
        tool_use_id="bash-test",
        tool=StubTool("Bash", scope="exec"),
        input={"command": command},
    )
    return PermissionEngine(mode=mode, rules=rules).evaluate(call).decision


def _allow_echo(command: str) -> str:
    return _bash([BashRule("echo", "allow")], command)


@pytest.mark.parametrize(
    "command",
    [
        "echo x > file",
        "echo x >file",
        "echo x >> file",
        "echo x 2> err",
        "echo x 2>>err",
        "echo x 1>f",
        'echo x >"f"',
        "echo x <>f",
        "echo x 2>&1 >file",
        "echo x > /dev/null/../f",
        "echo x | echo y > f",
        "echo x && echo y >> f",
        "echo x >",
    ],
)
def test_an_allow_rule_does_not_cover_a_command_that_redirects_output_to_a_file(
    command: str,
) -> None:
    assert _allow_echo(command) == "ask"


@pytest.mark.parametrize(
    "command",
    [
        "echo x 2>&1",
        "echo x >&2",
        "echo x 1>&2",
        "echo x >&-",
        "echo x <&3",
        "echo x >/dev/null",
        "echo x 2>/dev/null",
        "echo x >> /dev/null",
        "echo x > /dev/stderr",
        'echo x 2>"/dev/stdout"',
        "echo x < in",
        "echo '>' \">f\" \\>f",
        "echo x 2>&1 >/dev/null",
    ],
)
def test_an_allow_rule_covers_descriptor_duplication_and_the_standard_devices(
    command: str,
) -> None:
    assert _allow_echo(command) == "allow"


def test_an_allow_all_rule_does_not_cover_a_file_redirect_and_ask_and_deny_are_unchanged() -> None:
    allow_all: list[PermissionRule] = [BashRule("*", "allow")]
    assert _bash(allow_all, "pytest -q > test.log") == "ask"
    assert _bash(allow_all, "pytest -q >/dev/null 2>&1") == "allow"
    assert _bash(allow_all, "pytest -q > test.log", "skip-dangerous") == "allow"
    rules: list[PermissionRule] = [BashRule("rm *", "deny"), BashRule("git push *", "ask")]
    assert _bash(rules, "rm -rf x > /dev/null", "skip-dangerous") == "deny"
    assert _bash(rules, "rm -rf x > f", "skip-dangerous") == "deny"
    assert _bash(rules, "git push > log", "skip-dangerous") == "ask"
    assert _bash(rules, "git status > log", "skip-dangerous") == "allow"


def test_the_stricter_of_two_pooled_bash_rules_decides_a_file_redirect() -> None:
    # Python re-evaluates the pool from each later bash rule; the Rust kernel pools
    # once per layer and would allow here. The stricter Python result is kept.
    rules: list[PermissionRule] = [BashRule("echo", "allow"), BashRule("echo *", "ask")]
    assert _bash(rules, "echo x > f", "skip-dangerous") == "ask"
