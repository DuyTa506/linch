"""Deny and ask rules see through the words in front of a command.

The tables are cases of the Rust kernel's permission tests
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


def _deny_rm(command: str) -> str:
    return _bash([BashRule("rm *", "deny")], command, "skip-dangerous")


_BEHIND_A_PREFIX = [
    "X=1 rm -rf build",
    'X=1 Y="a b" rm -rf build',
    "if true; then rm -rf build; fi",
    "if false; then :; else rm -rf build; fi",
    "if false; then :; elif true; then rm -rf build; fi",
    "while true; do rm -rf build; done",
    "until false; do rm -rf build; done",
    "{ rm -rf build; }",
    "! rm -rf build",
    "time rm -rf build",
    "time -p rm -rf build",
    "exec rm -rf build",
    "command rm -rf build",
    "command -p rm -rf build",
    "builtin rm -rf build",
    "env rm -rf build",
    "env X=1 rm -rf build",
    "X=1 command rm -rf build",
    ">log rm -rf build",
    "> log rm -rf build",
    "2>/dev/null rm -rf build",
    "2>&1 rm -rf build",
    "<in rm -rf build",
    "echo hi && X=1 rm -rf build",
    "echo hi | ! rm -rf build",
]


@pytest.mark.parametrize("command", ["rm -rf build", *_BEHIND_A_PREFIX])
def test_a_deny_rule_covers_a_command_that_is_not_the_first_word_of_its_segment(
    command: str,
) -> None:
    assert _deny_rm(command) == "deny"


@pytest.mark.parametrize(
    "command",
    [
        "\\rm -rf build",
        'r""m -rf build',
        "'rm' -rf build",
        '"rm" -rf build',
        "r\\m -rf build",
        "X=1 \\rm -rf build",
        "echo hi; \\rm -rf build",
    ],
)
def test_a_deny_rule_covers_a_command_name_spelled_with_quotes_or_escapes(command: str) -> None:
    assert _deny_rm(command) == "deny"


def test_a_quoted_command_word_in_an_argument_is_not_a_command() -> None:
    assert _deny_rm('echo "rm -rf build"') == "allow"


@pytest.mark.parametrize(
    "command",
    [
        "X=1 git push origin main",
        "if true; then git push origin main; fi",
        "{ git push origin main; }",
        "! git push origin main",
        "command git push origin main",
    ],
)
def test_an_ask_rule_covers_a_command_that_is_not_the_first_word_of_its_segment(
    command: str,
) -> None:
    assert _bash([BashRule("git push *", "ask")], command, "skip-dangerous") == "ask"


@pytest.mark.parametrize(
    "command",
    [
        "X=1 echo hi",
        "LD_PRELOAD=/tmp/x.so echo hi",
        "time echo hi",
        "command echo hi",
        "env X=1 echo hi",
        ">log echo hi",
    ],
)
def test_a_prefix_does_not_make_a_segment_match_an_allow_rule(command: str) -> None:
    assert _allow_echo(command) == "ask"


@pytest.mark.parametrize(
    "command",
    ["echo rm -rf build", "git commit -m 'rm -rf build'", "X=rm echo hi", "cmd 2>&1 rm"],
)
def test_a_deny_rule_does_not_match_words_that_only_follow_the_command(command: str) -> None:
    assert _deny_rm(command) == "allow"


@pytest.mark.parametrize(
    "command",
    [
        "env rm -rf build",
        "\\env rm -rf build",
        'e""nv rm -rf build',
        "'command' rm -rf build",
        "\\command rm -rf build",
        "\\exec rm -rf build",
        '"time" rm -rf build',
        "env \\env rm -rf build",
    ],
)
def test_a_deny_rule_covers_a_command_behind_a_quoted_wrapper(command: str) -> None:
    assert _deny_rm(command) == "deny"


@pytest.mark.parametrize(
    "command",
    [
        "env -i rm -rf build",
        "env -u N rm -rf build",
        "env -u N -i X=1 rm -rf build",
        "env -C . rm -rf build",
        "env --unset N rm -rf build",
        "time -o f rm -rf build",
        "time -f fmt rm -rf build",
        "time -p rm -rf build",
        "exec -a n rm -rf build",
        "command -p rm -rf build",
    ],
)
def test_a_deny_rule_covers_a_command_behind_a_wrapper_option_with_an_argument(
    command: str,
) -> None:
    assert _deny_rm(command) == "deny"


@pytest.mark.parametrize(
    "command", ["command -v rm", "command -pv rm", "env -u rm make", "exec -a rm sleep 1"]
)
def test_a_deny_rule_does_not_match_the_argument_of_a_wrapper_option(command: str) -> None:
    assert _bash([BashRule("rm", "deny")], command, "skip-dangerous") == "allow"


@pytest.mark.parametrize(
    "command",
    ["env -u rm make", "exec -a rm sleep 1", "time -f rm make", "command -v rm x"],
)
def test_a_deny_glob_does_not_match_the_argument_of_a_wrapper_option(command: str) -> None:
    assert _deny_rm(command) == "allow"


@pytest.mark.parametrize(
    "command",
    [
        'git commit -m "do not use --force"',
        "git commit -m 'never --force'",
        "echo 'x --force'",
        'echo "x --force"',
    ],
)
def test_a_deny_glob_does_not_match_inside_a_quoted_argument(command: str) -> None:
    rules: list[PermissionRule] = [BashRule("* --force", "deny")]
    assert _bash(rules, "git push --force", "skip-dangerous") == "deny"
    assert _bash(rules, command, "skip-dangerous") == "allow"


@pytest.mark.parametrize("command", ["X=1 rm x", "\\rm x", "if true; then rm x; fi", "env rm x"])
def test_a_regex_deny_pattern_also_sees_the_command_behind_a_prefix(command: str) -> None:
    rules: list[PermissionRule] = [BashRule({"regex": r"^rm\b"}, "deny")]
    assert _bash(rules, command, "skip-dangerous") == "deny"
    assert _bash(rules, "echo rm x", "skip-dangerous") == "allow"


def test_a_passthrough_rule_is_skipped_for_the_command_behind_a_prefix() -> None:
    rules: list[PermissionRule] = [BashRule("rm *", "passthrough"), BashRule("rm *", "deny")]
    assert _bash(rules, "X=1 rm x", "skip-dangerous") == "deny"
    assert _bash([rules[0]], "X=1 rm x", "skip-dangerous") == "allow"


# Source: the wrapper-option tests of the Rust kernel's permission tests.
def _deny_touch(command: str) -> str:
    return _bash([BashRule("touch *", "deny")], command, "skip-dangerous")


@pytest.mark.parametrize(
    "command",
    [
        "env -i touch M",
        "env X=1 touch M",
        'env "-i" touch M',
        "env \\-i touch M",
        "env '-u' X touch M",
        'env "X=1" touch M',
        "env 'X=1' touch M",
        "env X\\=1 touch M",
        'command "-p" touch M',
        'time "-o" f touch M',
        'exec "-a" n touch M',
    ],
)
def test_a_deny_rule_covers_a_command_behind_a_quoted_wrapper_option(command: str) -> None:
    assert _deny_touch(command) == "deny"


@pytest.mark.parametrize("command", ["env \"-S\" 'touch M'", "env '-S' 'touch M'"])
def test_a_quoted_env_split_string_is_unparsed(command: str) -> None:
    assert _deny_touch(command) == "ask"


@pytest.mark.parametrize(
    "command",
    [
        "env -u X touch M",
        "env -iu X touch M",
        "env -uX touch M",
        "env --uns X touch M",
        "env --unset=X touch M",
        "env -iC . touch M",
        "env --chd . touch M",
        "env -a n touch M",
        "env --argv0 n touch M",
        "time -po f touch M",
        "time -ao f touch M",
        "time -ofile touch M",
        "time --outp f touch M",
        "time --format=x touch M",
        "exec -la n touch M",
        "exec -ca n touch M",
        "exec -cla n touch M",
    ],
)
def test_a_deny_rule_covers_a_command_behind_a_bundled_or_abbreviated_wrapper_option(
    command: str,
) -> None:
    assert _deny_touch(command) == "deny"


@pytest.mark.parametrize(
    "command",
    ["env -iS 'touch M'", "env --split-str 'touch M'", "env --split-string='touch M'"],
)
def test_a_bundled_or_abbreviated_env_split_string_is_unparsed(command: str) -> None:
    assert _deny_touch(command) == "ask"


@pytest.mark.parametrize(
    "command",
    [
        "env -iu rm make",
        "env --uns rm make",
        "exec -la rm sleep 1",
        "time -po rm make",
        'env "-u" rm make',
    ],
)
def test_a_deny_rule_does_not_match_the_argument_of_a_bundled_wrapper_option(
    command: str,
) -> None:
    assert _bash([BashRule("rm", "deny")], command, "skip-dangerous") == "allow"


# Source: `a_glob_deny_rule_covers_a_quoted_second_word` in the Rust kernel's tests.
@pytest.mark.parametrize(
    "command",
    [
        "git push origin main",
        "git 'push' origin main",
        'git "push" origin main',
        "git pu\\sh origin main",
        "git pu\"\"sh 'origin' main",
        "X=1 git 'push' origin main",
    ],
)
def test_a_glob_deny_rule_covers_a_quoted_second_word(command: str) -> None:
    assert _bash([BashRule("git push *", "deny")], command, "skip-dangerous") == "deny"


@pytest.mark.parametrize(
    "command",
    ["rm -rf build", 'rm -r""f build', "rm '-rf' build", "\\rm -r'f' build"],
)
def test_a_glob_deny_rule_covers_quoted_flags(command: str) -> None:
    assert _bash([BashRule("rm -rf *", "deny")], command, "skip-dangerous") == "deny"


@pytest.mark.parametrize(
    "command",
    [
        "git commit -m 'never --force'",
        'git commit -m "never --force"',
        "git 'commit' -m 'it'\\''s --force'",
    ],
)
def test_a_quoted_argument_stays_one_word_for_a_glob_deny_rule(command: str) -> None:
    rules: list[PermissionRule] = [BashRule("* --force", "deny")]
    assert _bash(rules, command, "skip-dangerous") == "allow"
    assert _bash(rules, "git 'push' '--force'", "skip-dangerous") == "deny"
