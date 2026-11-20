"""Decisions of Bash rules over shell syntax, one table per behaviour.

The tables are the engine cases of the Rust kernel's permission tests
(`linch-rust/crates/linch-core/src/permissions/tests.rs`: `SUPPORTED`,
`UNPARSED`, `ECHO_VARIANTS` and the deny/ask/allow tests),
copied with their expected decisions so both kernels decide the same inputs the
same way.
"""

from __future__ import annotations

import random

import pytest

from linch.permissions.engine import PendingToolCall, PermissionEngine
from linch.permissions.rules import BashRule, PermissionRule, ToolRule
from linch.permissions.ruleset import PermissionLayer, PermissionRuleSet
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


_SUPPORTED = [
    "pytest -q 2>&1",
    "cat < input.txt",
    "pytest -q > /dev/null 2>&1",
    "python script.py < input.txt 2> /dev/null",
    "git status | cat",
    "ruff check . | tee ruff.log",
    "npm test 2>&1 | tee npm.log",
    "find . -type f | sort",
    "grep -R TODO src | wc -l",
    "cargo test --all-features 2>&1",
    "sed -n '1,10p' README.md | cat",
    "echo hi >&2",
    "cat <&3",
    "build 2>&1 && deploy",
    "build >/dev/null 2>&1 | tee run.log",
    "tail -f app.log 2>/dev/null",
    "printf '&'",
    "printf '$(literal)'",
    "printf '\\&'",
]

_UNPARSED = [
    "true & rm -rf build",
    "echo ok\ntouch pwned",
    "echo ok \\\nrm -rf build",
    "echo $(rm -rf build)",
    "echo `rm -rf build`",
    "cat <(secret-command)",
    "cat >(write-command)",
    "echo ok & wait",
    "echo $(cat secrets.txt)",
    "printf '%s' `cat token.txt`",
    "echo $'\\'' ; rm -rf / ; '\\'",
    "cmd &> output.log",
    "echo hi &>/tmp/f touch /tmp/pwned",
    "echo \\>& touch /tmp/pwned",
    "echo \\<& touch /tmp/pwned",
    "echo hi #'\ntouch /tmp/pwned\n#'",
    "echo hi # comment",
    "echo hi;# comment",
    "echo hi >| /tmp/f touch /tmp/pwned",
    "cat <<EOF",
    "cat <<-EOF",
]


@pytest.mark.parametrize("command", _SUPPORTED)
def test_allow_all_bash_keeps_supported_syntax(command: str) -> None:
    assert _bash([BashRule("*", "allow")], command) == "allow"


@pytest.mark.parametrize("command", _UNPARSED)
@pytest.mark.parametrize("rule", [BashRule("*", "allow"), BashRule("rm -rf *", "deny")])
def test_unparsed_syntax_is_never_allowed_by_bash_rules(rule: BashRule, command: str) -> None:
    assert _bash([rule], command, "skip-dangerous") == "ask"


def test_narrow_allow_rule_asks_for_ansi_c_quoted_quote() -> None:
    assert _bash([BashRule("echo *", "allow")], "echo $'\\'' ; rm -rf / ; '\\'") == "ask"


def test_an_allow_rule_does_not_cover_the_command_after_a_background_redirect() -> None:
    assert _allow_echo("echo hi &>redir touch M") == "ask"
    assert _allow_echo("echo hi &>/tmp/f touch /tmp/pwned") == "ask"


@pytest.mark.parametrize("command", ["echo \\>& touch M", "echo \\<& touch M"])
def test_an_allow_rule_does_not_cover_the_command_after_an_escaped_redirect_and_ampersand(
    command: str,
) -> None:
    assert _allow_echo(command) == "ask"


def test_an_allow_rule_does_not_cover_a_line_hidden_behind_a_comment_quote() -> None:
    assert _allow_echo("echo hi #'\ntouch M\n#'") == "ask"


def test_an_allow_rule_does_not_cover_a_command_behind_a_quote_in_a_parameter_pattern() -> None:
    assert _allow_echo("echo \"${x#'\"'}\" ; touch M ; echo \\'") == "ask"


def test_a_deny_rule_still_sees_a_command_after_an_ampersand_redirect() -> None:
    assert _deny_rm("rm -rf build &>log") == "deny"
    assert _deny_rm("echo hi &>log touch x") == "ask"


@pytest.mark.parametrize(
    "command",
    [
        "echo hi 2>&1",
        "echo hi >&2",
        "echo hi 1>&2 | echo ok",
        "echo hi <&3",
        "echo hi && echo ok",
        "echo hi >/dev/null 2>&1 && echo ok",
    ],
)
def test_an_allow_rule_covers_the_redirections_the_splitter_models(command: str) -> None:
    assert _allow_echo(command) == "allow"
    assert _allow_echo("echo hi & echo ok") == "ask"


@pytest.mark.parametrize(
    "command",
    ["echo a#b", "echo '#x'", 'echo "#x"', "echo \\#x", "echo ${#PATH}"],
)
def test_a_hash_inside_a_word_or_a_quote_is_not_a_comment(command: str) -> None:
    assert _allow_echo(command) == "allow"


@pytest.mark.parametrize(
    "command", ["{ echo hi; }", "echo hi; { touch x; }", "echo hi && { touch x; }"]
)
def test_a_brace_group_is_not_allowed_by_a_narrow_allow_rule(command: str) -> None:
    assert _allow_echo(command) == "ask"


@pytest.mark.parametrize(
    "command",
    [
        "a=rm; $a -rf build",
        "$a -rf build",
        "${a} -rf build",
        '"$a" -rf build',
        "X=1 $a -rf build",
        "command $a -rf build",
        "! $a -rf build",
        ">log $a -rf build",
        "echo hi | $SHELL",
        "echo hi && x=$y $z",
    ],
)
def test_an_expansion_in_command_position_is_unparsed(command: str) -> None:
    assert _deny_rm(command) == "ask"
    assert _bash([BashRule("*", "allow")], command) == "ask"


@pytest.mark.parametrize(
    "command",
    ["echo $a", 'echo "$a" | cat', "X=$a echo hi", "'$a' -rf build", "\\$a -rf build", "cmd $a"],
)
def test_an_expansion_in_an_argument_is_modelled(command: str) -> None:
    assert _bash([BashRule("*", "allow")], command) == "allow"


@pytest.mark.parametrize(
    "command",
    ["echo hi <<EOF", "echo hi <<-EOF", "echo hi <<'EOF'", "echo hi <<EOF\ntouch pwned\nEOF"],
)
def test_a_here_document_is_unparsed_and_never_allowed(command: str) -> None:
    assert _allow_echo(command) == "ask"
    assert _deny_rm("rm -rf build <<EOF") == "deny"


def test_a_here_string_is_modelled_and_covered_by_the_rule_of_its_command() -> None:
    assert _allow_echo("echo hi <<< word") == "allow"
    assert _allow_echo("echo hi <<< 'a;b'") == "allow"
    assert _allow_echo("echo hi <<< a; touch x") == "ask"


def test_a_clobber_redirect_is_unparsed_and_never_allowed() -> None:
    assert _allow_echo("echo hi >| out") == "ask"


def test_a_pipe_and_ampersand_is_unparsed_and_never_allowed() -> None:
    assert _allow_echo("echo hi |& cat") == "ask"


def test_a_backslash_inside_double_quotes_is_kept_before_an_ordinary_byte() -> None:
    rules: list[PermissionRule] = [BashRule('cat "a\\nb"', "allow")]
    assert _bash(rules, 'cat "a\\nb"') == "allow"
    assert _bash(rules, 'cat "anb"') == "ask"
    assert _bash(rules, "cat 'a\\nb'") == "allow"


# Commands that try to run a second command under a lone `echo` allow rule, in
# `default` mode. The ones expected `allow` run exactly one command in `sh`
# (checked against dash and bash); the rest must ask.
_ECHO_VARIANTS = [
    ("echo hi; touch pwned", "ask"),
    ("echo hi;touch pwned", "ask"),
    ("echo hi && touch pwned", "ask"),
    ("echo hi || touch pwned", "ask"),
    ("echo hi | touch pwned", "ask"),
    ("echo hi |& touch pwned", "ask"),
    ("echo hi & touch pwned", "ask"),
    ("echo hi\ntouch pwned", "ask"),
    ("echo hi \\\ntouch pwned", "ask"),
    ("echo hi #\ntouch pwned", "ask"),
    ("echo hi #'\ntouch pwned\n#'", "ask"),
    ("echo $(touch pwned)", "ask"),
    ('echo "$(touch pwned)"', "ask"),
    ("echo `touch pwned`", "ask"),
    ('echo "`touch pwned`"', "ask"),
    ("echo ${x:-$(touch pwned)}", "ask"),
    ("echo $((1))", "ask"),
    ("echo $'a'; touch pwned", "ask"),
    ("echo $'\\''; touch pwned; echo '\\''", "ask"),
    ("echo hi <(touch pwned)", "ask"),
    ("echo hi >(touch pwned)", "ask"),
    ("(echo hi; touch pwned)", "ask"),
    ("echo hi <<EOF\ntouch pwned\nEOF", "ask"),
    ("echo hi <<-EOF\n\ttouch pwned\n\tEOF", "ask"),
    ("echo hi &>/dev/null touch pwned", "ask"),
    ("echo hi >|f touch pwned", "ask"),
    ("echo \\>& touch pwned", "ask"),
    ("echo '>'& touch pwned", "ask"),
    ("echo hi >& touch pwned", "ask"),
    ("echo hi >&$fd; touch pwned", "ask"),
    ("echo \\$(touch pwned)", "ask"),
    ("echo hi; X=1 touch pwned", "ask"),
    ("echo hi && { touch pwned; }", "ask"),
    ("echo hi && if true; then touch pwned; fi", "ask"),
    ("echo hi && ! touch pwned", "ask"),
    ("echo hi && $t pwned", "ask"),
    ('echo hi; "$t" pwned', "ask"),
    ("echo hi | $SHELL", "ask"),
    ("X=1 echo hi", "ask"),
    ("time echo hi", "ask"),
    ("echo hi; function f { touch pwned; }; f", "ask"),
    ("echo 'a", "ask"),
    ('echo "a', "ask"),
    ("echo a\\", "ask"),
    # One command each: quotes and escapes keep the operators literal.
    ("echo 'a;touch pwned'", "allow"),
    ('echo "a;touch pwned"', "allow"),
    ("echo a\\;touch pwned", "allow"),
    ('echo "a\\";touch pwned"', "allow"),
    ("echo '\\'", "allow"),
    ('echo "a\nb"', "allow"),
    ("echo a\\&touch pwned", "allow"),
    ('echo "\\$(touch pwned)"', "allow"),
    ('echo "\\`touch pwned\\`"', "allow"),
    ("echo '$(touch pwned)' '`x`'", "allow"),
    ("echo a\\ b", "allow"),
    ("echo 1>&2", "allow"),
    ("echo hi 2>&1 >&2 <&0", "allow"),
    ("echo hi <<< 'a;touch pwned'", "allow"),
    ("echo hi；touch pwned", "allow"),
    ("echo a\\>b", "allow"),
]


@pytest.mark.parametrize(("command", "expected"), _ECHO_VARIANTS)
def test_a_lone_echo_allow_rule_never_covers_a_second_command(command: str, expected: str) -> None:
    assert _allow_echo(command) == expected


_PATTERN_OPERATORS = ["#", "##", "%", "%%", ":-", "/"]


@pytest.mark.parametrize("op", _PATTERN_OPERATORS)
def test_parameter_pattern_quotes_do_not_hide_a_command_from_an_allow_rule(op: str) -> None:
    assert _allow_echo(f"echo \"${{x{op}'\"'}}\" ; touch /tmp/pwned ; echo \\'") == "ask"
    assert _allow_echo(f"echo ${{x{op}'\"'}} ; touch /tmp/pwned ; echo \\'") == "ask"
    assert _allow_echo(f'echo $(echo "${{x{op}\'"\'}}" ; touch /tmp/pwned)') == "ask"


@pytest.mark.parametrize("op", _PATTERN_OPERATORS)
def test_a_deny_rule_covers_a_command_behind_a_quote_in_a_parameter_pattern(op: str) -> None:
    assert _deny_rm(f"echo \"${{x{op}'\"'}}\" ; rm -rf build ; echo \\'") != "allow"


@pytest.mark.parametrize(
    "command",
    [
        "echo \"${x#'a'}\"",
        'echo "${x:-"a"}"',
        'echo "${x#\\"}"',
        'echo "${x:-${y}}"',
        'echo "${x/\'/}"',
        "echo \"${x#'\"'}",
    ],
)
def test_a_double_quoted_expansion_holding_a_quote_a_backslash_or_a_nested_expansion_is_unparsed(
    command: str,
) -> None:
    assert _allow_echo(command) == "ask"


@pytest.mark.parametrize(
    "command",
    [
        'echo "${x#a}"',
        'echo "${x:-b}"',
        "echo \"${x%%.*}\" 'it''s'",
        "echo ${x#'\"'}",
        'echo ${x#"\'"}',
        "echo ${x:-'a b'}",
    ],
)
def test_a_plain_parameter_expansion_is_modelled_in_and_out_of_double_quotes(
    command: str,
) -> None:
    assert _allow_echo(command) == "allow"


@pytest.mark.parametrize(
    "command", ["env -S 'rm -rf build'", "env --split-string='rm x'", "env -i -S 'rm x'"]
)
def test_an_env_split_string_is_unparsed(command: str) -> None:
    assert _deny_rm(command) == "ask"


@pytest.mark.parametrize(
    "command",
    [
        "function f { rm -rf build; }; f",
        "coproc rm -rf build",
        "a[0]=1 rm -rf build",
        "{fd}>log rm -rf build",
        "{rm,-rf,build}",
        "r{m,} -rf build",
        "{1..3}",
        "X=1 function f { rm; }",
    ],
)
def test_bash_only_command_prefixes_are_unparsed(command: str) -> None:
    assert _deny_rm(command) == "ask"


@pytest.mark.parametrize(
    "command",
    ["echo {a,b}", "echo {1..3}", "echo 'function'", "echo a[0]=1", "git log {x}"],
)
def test_bash_only_words_in_an_argument_are_modelled(command: str) -> None:
    assert _deny_rm(command) == "allow"


def test_a_hash_after_an_escaped_blank_is_not_a_comment() -> None:
    assert _allow_echo("echo a\\ #b") == "allow"
    assert _allow_echo("echo a\\;#b") == "allow"
    assert _allow_echo("echo a'b'#c") == "allow"
    assert _allow_echo("echo a #b") == "ask"
    assert _allow_echo("echo a ;#b") == "ask"


def test_unparsed_floor_beats_a_generic_allow_in_any_layer_order() -> None:
    flat: list[PermissionRule] = [ToolRule("Bash", "allow"), BashRule("rm -rf *", "deny")]
    assert _bash(flat, "true & rm -rf build", "skip-dangerous") == "ask"
    assert _bash(flat, "rm -rf build & x", "skip-dangerous") == "deny"

    for deny_first in (True, False):
        deny = PermissionLayer("project", [BashRule("rm -rf *", "deny")])
        allow = PermissionLayer("runtime", [ToolRule("Bash", "allow")])
        layers = [deny, allow] if deny_first else [allow, deny]
        engine = PermissionEngine(mode="skip-dangerous", rule_set=PermissionRuleSet(layers))
        call = PendingToolCall(
            tool_use_id="bash-test",
            tool=StubTool("Bash", scope="exec"),
            input={"command": "true & rm -rf build"},
        )
        assert engine.evaluate(call).decision == "ask"


def test_bash_segments_take_the_first_matching_bash_rule() -> None:
    rules: list[PermissionRule] = [
        BashRule("git *", "allow"),
        BashRule("rm *", "deny"),
        BashRule("ls", "ask"),
    ]
    assert _bash(rules, "git status && git log") == "allow"
    assert _bash(rules, "git status; rm -rf build") == "deny"
    assert _bash(rules, "git status | ls") == "ask"
    assert _bash(rules, "git status; make") == "ask"
    assert _bash([BashRule("git *", "allow")], "git status; make", "skip-dangerous") == "allow"
    assert _bash(rules, " ; ") == "ask"


def test_a_later_deny_rule_still_denies_an_unparsed_command_after_one_that_does_not_match() -> None:
    rules: list[PermissionRule] = [
        ToolRule("Bash", "allow"),
        BashRule("ls", "deny"),
        BashRule("rm *", "deny"),
    ]
    assert _bash(rules, "rm -rf build & x", "skip-dangerous") == "deny"
    layered = PermissionEngine(
        mode="skip-dangerous",
        rule_set=PermissionRuleSet([PermissionLayer("runtime", rules)]),
    )
    call = PendingToolCall(
        tool_use_id="bash-test",
        tool=StubTool("Bash", scope="exec"),
        input={"command": "rm -rf build & x"},
    )
    assert layered.evaluate(call).decision == "deny"


@pytest.mark.parametrize(
    "command", ["echo hi &>log touch x", "X=1 echo hi", "echo hi\ntouch x", "echo $(x)"]
)
def test_layered_rules_decide_these_commands_like_flat_rules(command: str) -> None:
    rules: list[PermissionRule] = [BashRule("echo", "allow"), BashRule("rm *", "deny")]
    layered = PermissionEngine(
        mode="default", rule_set=PermissionRuleSet([PermissionLayer("runtime", rules)])
    )
    call = PendingToolCall(
        tool_use_id="bash-test",
        tool=StubTool("Bash", scope="exec"),
        input={"command": command},
    )
    assert layered.evaluate(call).decision == "ask"
    assert _bash(rules, command) == "ask"


def test_evaluating_shell_text_never_raises() -> None:
    # Shell alphabet fuzz: the tokenizer, splitter, detector and rule evaluation
    # index into the text by position and must not fail on any prefix or suffix.
    rng = random.Random(7)
    alphabet = list("a b\t&|;()$`'\"\\\n<>#{}[]=,.!-*?\u00e9\u00a0")
    rules: list[PermissionRule] = [
        BashRule("a *", "allow"),
        BashRule("b", "deny"),
        BashRule("c *", "ask"),
    ]
    for _ in range(3000):
        command = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 40)))
        assert _bash(rules, command) in {"allow", "deny", "ask"}


# Source: `OLD_ARITHMETIC` in the Rust kernel's permission tests.
_OLD_ARITHMETIC = [
    "echo \"${x:+$[}'\"']}\" ; touch M ; echo \\'",
    "echo \"${x:+$[}]'\"'}\" ; touch M ; echo \\'",
    "echo \"${x#$[}]'\"'}\" ; touch M ; echo \\'",
    "echo \"${x:-$((}'\"']}\" ; touch M ; echo \\'",
    "echo \"${x:-`}'\"']}\" ; touch M ; echo \\'",
]


@pytest.mark.parametrize("command", _OLD_ARITHMETIC)
def test_an_allow_rule_does_not_cover_a_command_behind_a_brace_in_old_arithmetic(
    command: str,
) -> None:
    assert _allow_echo(command) == "ask"


@pytest.mark.parametrize("command", _OLD_ARITHMETIC)
def test_a_deny_rule_covers_a_command_behind_a_brace_in_old_arithmetic(command: str) -> None:
    rules: list[PermissionRule] = [BashRule("touch *", "deny")]
    assert _bash(rules, command, "skip-dangerous") != "allow"


def test_old_arithmetic_outside_a_parameter_expansion_is_modelled() -> None:
    assert _allow_echo('echo "${x:-a} $[1+1]"') == "allow"


def test_a_blank_after_an_ampersand_redirect_is_unparsed_and_never_allowed() -> None:
    assert _allow_echo("echo hi >& 2") == "ask"
    assert _bash([BashRule("*", "allow")], "echo hi >& 2") == "ask"
