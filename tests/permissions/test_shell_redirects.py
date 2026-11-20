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


@pytest.mark.parametrize(
    "command",
    [
        "pytest -q > test.log",
        "echo x >> f",
        "make 2> err.log",
        "cat <> f",
        "pytest -q >/dev/null 2>&1",
    ],
)
def test_an_allow_all_rule_covers_a_file_redirect(command: str) -> None:
    assert _bash([BashRule("*", "allow")], command) == "allow"
    assert _bash([BashRule(patterns=["*"], decision="allow")], command) == "allow"


def test_a_narrower_allow_rule_still_does_not_cover_a_file_redirect() -> None:
    assert _bash([BashRule("git *", "allow")], "git status > f") == "ask"
    assert _bash([BashRule("*", "allow")], "echo x > f") == "allow"
    assert _bash([BashRule("echo", "allow")], "echo x > f") == "ask"
    assert _bash([BashRule("* *", "allow")], "echo x > f") == "ask"


@pytest.mark.parametrize(
    "command",
    [
        "echo hi &>redir touch M",
        "echo \\>& touch M",
        "echo hi #'\ntouch M\n#'",
        "echo \"${x#'\"'}\" ; touch M ; echo \\'",
        "echo hi >| out",
        "echo hi |& cat",
        "cat <<EOF",
        "$a -rf x",
    ],
)
def test_an_allow_all_rule_still_never_covers_unparsed_syntax(command: str) -> None:
    assert _bash([BashRule("*", "allow")], command) == "ask"


def test_deny_and_ask_rules_are_unchanged_by_a_file_redirect() -> None:
    rules: list[PermissionRule] = [BashRule("rm *", "deny"), BashRule("git push *", "ask")]
    assert _bash(rules, "rm -rf x > /dev/null", "skip-dangerous") == "deny"
    assert _bash(rules, "rm -rf x > f", "skip-dangerous") == "deny"
    assert _bash(rules, "git push > log", "skip-dangerous") == "ask"
    assert _bash(rules, "git status > log", "skip-dangerous") == "allow"


def test_allow_all_after_a_deny_or_ask_rule_keeps_both_with_a_file_redirect() -> None:
    deny: list[PermissionRule] = [BashRule("rm *", "deny"), BashRule("*", "allow")]
    assert _bash(deny, "rm x > f") == "deny"
    assert _bash(deny, "echo x > f") == "allow"
    ask: list[PermissionRule] = [BashRule("git push *", "ask"), BashRule("*", "allow")]
    assert _bash(ask, "git push origin > log") == "ask"
    assert _bash(ask, "git status > log") == "allow"


def test_a_deny_or_ask_rule_after_allow_all_is_shadowed_by_first_match_with_a_file_redirect() -> (
    None
):
    # The first matching rule decides a segment, so `*` first allows it, with or
    # without a redirect; the rules after it are never reached.
    deny: list[PermissionRule] = [BashRule("*", "allow"), BashRule("rm *", "deny")]
    assert _bash(deny, "rm x > f") == "allow"
    assert _bash(deny, "rm x") == "allow"
    ask: list[PermissionRule] = [BashRule("*", "allow"), BashRule("git push *", "ask")]
    assert _bash(ask, "git push > log") == "allow"
    assert _bash(ask, "git push") == "allow"


def test_a_narrow_allow_rule_before_allow_all_falls_through_to_it_with_a_file_redirect() -> None:
    # Python re-evaluates the pool from each later bash rule: the narrow rule
    # does not cover the redirect, then `*` does. The Rust kernel pools once per
    # layer and would not allow here.
    rules: list[PermissionRule] = [BashRule("echo", "allow"), BashRule("*", "allow")]
    assert _bash(rules, "echo x > f") == "allow"
    assert _bash([BashRule("*", "allow"), BashRule("echo", "allow")], "echo x > f") == "allow"


def test_the_stricter_of_two_pooled_bash_rules_decides_a_file_redirect() -> None:
    # Python re-evaluates the pool from each later bash rule; the Rust kernel pools
    # once per layer and would allow here. The stricter Python result is kept.
    rules: list[PermissionRule] = [BashRule("echo", "allow"), BashRule("echo *", "ask")]
    assert _bash(rules, "echo x > f", "skip-dangerous") == "ask"


def test_an_allow_rule_does_not_cover_a_redirect_to_a_file_spelled_like_a_descriptor() -> None:
    assert _allow_echo("echo hi >&2") == "allow"
    assert _allow_echo("echo hi >&-") == "allow"
    assert _allow_echo("echo hi >&M") == "ask"
    assert _allow_echo("echo hi >&/dev/null") == "ask"  # unparsed: `>&` then a name


@pytest.mark.parametrize(
    "command",
    [
        "echo hi >&2file",
        "echo hi >&1M",
        "echo hi 1>&2file",
        "echo hi >&2-M",
        'echo hi >&2"file"',
        "echo hi >&-file",
        "echo hi >&2-",
    ],
)
def test_a_redirect_to_a_file_spelled_like_a_descriptor_is_a_file_redirect(command: str) -> None:
    assert _allow_echo(command) == "ask"
    # The allow-all rule treats them like any other file redirect.
    assert _bash([BashRule("*", "allow")], command) == "allow"


@pytest.mark.parametrize(
    "command",
    [
        "pytest -q > test.log",
        "echo x >> f",
        "build 2>err.log | tee out.log > all.log",
        "pytest -q >/dev/null 2>&1",
    ],
)
def test_an_allow_all_rule_covers_each_segment_that_redirects_to_a_file(command: str) -> None:
    # Source: `an_allow_all_rule_covers_a_file_redirect_but_a_narrow_rule_does_not`.
    assert _bash([BashRule("*", "allow")], command) == "allow"


@pytest.mark.parametrize("pattern", ["echo", "echo *", "git *", "* x", "**x"])
@pytest.mark.parametrize("command", ["echo x > f", "git log >> f"])
def test_a_narrow_pattern_does_not_cover_a_file_redirect(pattern: str, command: str) -> None:
    assert _bash([BashRule(pattern, "allow")], command) != "allow"


def test_a_regex_pattern_is_not_the_allow_all_rule() -> None:
    assert _bash([BashRule({"regex": ".*"}, "allow")], "echo x > f") == "ask"


def test_the_first_rule_that_matches_a_segment_decides_whether_it_is_allow_all() -> None:
    narrow_first: list[PermissionRule] = [BashRule("echo", "allow"), BashRule("*", "allow")]
    assert _bash(narrow_first, "ls > f") == "allow"
    # Rust asks for `echo x > f` here (one pool per layer); Python re-evaluates the
    # pool from the later `*` rule and allows it. See the pooled-rules test above.
    assert _bash(narrow_first, "echo x > f") == "allow"
