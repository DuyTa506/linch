//! Engine tests, ported from `tests/permissions/` where the case still
//! applies, plus the escape, floor and key invariants.

use proptest::prelude::*;
use serde_json::json;

use super::*;
use crate::policy::{PermissionDecision, Principal};
use crate::tool::ToolScope;

use RuleDecision::{Allow, Ask, Deny};

mod paths;
mod policy;

fn input(value: JsonValue) -> JsonObject {
    match value {
        JsonValue::Object(map) => map,
        other => panic!("not an object: {other}"),
    }
}

fn layered(mode: PermissionMode, layers: Vec<PermissionLayer>) -> PermissionEngine {
    let mut policy = PermissionPolicy::new(mode, "/project");
    policy.layers = layers;
    PermissionEngine::new(policy).unwrap()
}

fn flat(mode: PermissionMode, rules: Vec<Rule>) -> PermissionEngine {
    layered(mode, vec![PermissionLayer::new("runtime", rules)])
}

fn kind(decision: &PermissionDecision) -> &'static str {
    match decision {
        PermissionDecision::Allow {} => "allow",
        PermissionDecision::Deny { .. } => "deny",
        PermissionDecision::Ask { .. } => "ask",
    }
}

fn decide(
    engine: &PermissionEngine,
    tool: &str,
    scope: ToolScope,
    args: JsonValue,
    principal: Option<&Principal>,
) -> &'static str {
    let args = input(args);
    kind(&engine.evaluate(&PermissionCall::new(tool, scope, &args), principal))
}

fn bash(engine: &PermissionEngine, command: &str) -> &'static str {
    decide(
        engine,
        "Bash",
        ToolScope::Exec,
        json!({"command": command}),
        None,
    )
}

fn write(engine: &PermissionEngine, path: &str) -> &'static str {
    decide(
        engine,
        "Write",
        ToolScope::Write,
        json!({"file_path": path}),
        None,
    )
}

fn principal(roles: &[&str]) -> Principal {
    Principal::new("t", "u", roles.iter().copied()).unwrap()
}

fn tool(name: &str, decision: RuleDecision) -> Rule {
    ToolRule::new(name, decision).into()
}

fn bash_rule(pattern: &str, decision: RuleDecision) -> Rule {
    BashRule::new([pattern], decision).into()
}

const SUPPORTED: &[&str] = &[
    "pytest -q 2>&1",
    "cat < input.txt",
    "pytest -q > test.log",
    "pytest -q >> test.log",
    "python script.py < input.txt > output.txt",
    "pytest -q > /dev/null 2>&1",
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
    "build >out.log 2>&1 | tee run.log",
    "tail -f app.log 2>/dev/null",
    "printf '&'",
    "printf '$(literal)'",
    "printf '\\&'",
];

const UNPARSED: &[&str] = &[
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
];

#[test]
fn allow_all_bash_keeps_supported_syntax() {
    let engine = flat(PermissionMode::Default, vec![bash_rule("*", Allow)]);
    for command in SUPPORTED {
        assert_eq!(bash(&engine, command), "allow", "{command:?}");
    }
}

#[test]
fn unparsed_syntax_is_never_allowed_by_bash_rules() {
    for rule in [bash_rule("*", Allow), bash_rule("rm -rf *", Deny)] {
        let engine = flat(PermissionMode::SkipDangerous, vec![rule]);
        for command in UNPARSED {
            assert_eq!(bash(&engine, command), "ask", "{command:?}");
        }
    }
    let narrow = flat(PermissionMode::Default, vec![bash_rule("echo *", Allow)]);
    assert_eq!(bash(&narrow, "echo $'\\'' ; rm -rf / ; '\\'"), "ask");
}

fn allow_echo() -> PermissionEngine {
    flat(PermissionMode::Default, vec![bash_rule("echo", Allow)])
}

#[test]
fn an_allow_rule_does_not_cover_the_command_after_a_background_redirect() {
    let command = "echo hi &>/tmp/f touch /tmp/pwned";
    assert_eq!(bash(&allow_echo(), command), "ask", "{command:?}");
}

#[test]
fn an_allow_rule_does_not_cover_the_command_after_an_escaped_redirect_and_ampersand() {
    for command in ["echo \\>& touch /tmp/pwned", "echo \\<& touch /tmp/pwned"] {
        assert_eq!(bash(&allow_echo(), command), "ask", "{command:?}");
    }
}

#[test]
fn an_allow_rule_does_not_cover_a_line_hidden_behind_a_comment_quote() {
    let command = "echo hi #'\ntouch /tmp/pwned\n#'";
    assert_eq!(bash(&allow_echo(), command), "ask", "{command:?}");
}

#[test]
fn a_deny_rule_still_sees_a_command_after_an_ampersand_redirect() {
    let engine = flat(PermissionMode::SkipDangerous, vec![bash_rule("rm *", Deny)]);
    assert_eq!(bash(&engine, "rm -rf build &>log"), "deny");
    assert_eq!(bash(&engine, "echo hi &>log touch x"), "ask");
}

#[test]
fn an_allow_rule_covers_the_redirections_the_splitter_models() {
    for command in [
        "echo hi 2>&1",
        "echo hi >&2",
        "echo hi 1>&2 | echo ok",
        "echo hi <&3",
        "echo hi && echo ok",
        "echo hi >/dev/null 2>&1 && echo ok",
    ] {
        assert_eq!(bash(&allow_echo(), command), "allow", "{command:?}");
    }
    assert_eq!(bash(&allow_echo(), "echo hi & echo ok"), "ask");
}

#[test]
fn a_hash_inside_a_word_or_a_quote_is_not_a_comment() {
    for command in [
        "echo a#b",
        "echo '#x'",
        "echo \"#x\"",
        "echo \\#x",
        "echo ${#PATH}",
    ] {
        assert_eq!(bash(&allow_echo(), command), "allow", "{command:?}");
    }
}

#[test]
fn a_brace_group_is_not_allowed_by_a_narrow_allow_rule() {
    for command in [
        "{ echo hi; }",
        "echo hi; { touch x; }",
        "echo hi && { touch x; }",
    ] {
        assert_eq!(bash(&allow_echo(), command), "ask", "{command:?}");
    }
}

const BEHIND_A_PREFIX: &[&str] = &[
    "X=1 rm -rf build",
    "X=1 Y=\"a b\" rm -rf build",
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
];

#[test]
fn a_deny_rule_covers_a_command_that_is_not_the_first_word_of_its_segment() {
    let engine = flat(PermissionMode::SkipDangerous, vec![bash_rule("rm *", Deny)]);
    assert_eq!(bash(&engine, "rm -rf build"), "deny");
    for command in BEHIND_A_PREFIX {
        assert_eq!(bash(&engine, command), "deny", "{command:?}");
    }
}

#[test]
fn a_deny_rule_covers_a_command_name_spelled_with_quotes_or_escapes() {
    let engine = flat(PermissionMode::SkipDangerous, vec![bash_rule("rm *", Deny)]);
    for command in [
        "\\rm -rf build",
        "r\"\"m -rf build",
        "'rm' -rf build",
        "\"rm\" -rf build",
        "r\\m -rf build",
        "X=1 \\rm -rf build",
        "echo hi; \\rm -rf build",
    ] {
        assert_eq!(bash(&engine, command), "deny", "{command:?}");
    }
    assert_eq!(bash(&engine, "echo \"rm -rf build\""), "allow");
}

#[test]
fn an_ask_rule_covers_a_command_that_is_not_the_first_word_of_its_segment() {
    let engine = flat(
        PermissionMode::SkipDangerous,
        vec![bash_rule("git push *", Ask)],
    );
    for command in [
        "X=1 git push origin main",
        "if true; then git push origin main; fi",
        "{ git push origin main; }",
        "! git push origin main",
        "command git push origin main",
    ] {
        assert_eq!(bash(&engine, command), "ask", "{command:?}");
    }
}

#[test]
fn a_prefix_does_not_make_a_segment_match_an_allow_rule() {
    // `LD_PRELOAD=x echo` or `time echo` is not the command `echo`.
    let engine = allow_echo();
    for command in [
        "X=1 echo hi",
        "LD_PRELOAD=/tmp/x.so echo hi",
        "time echo hi",
        "command echo hi",
        "env X=1 echo hi",
        ">log echo hi",
    ] {
        assert_eq!(bash(&engine, command), "ask", "{command:?}");
    }
}

#[test]
fn a_deny_rule_does_not_match_words_that_only_follow_the_command() {
    let engine = flat(PermissionMode::SkipDangerous, vec![bash_rule("rm *", Deny)]);
    for command in [
        "echo rm -rf build",
        "git commit -m 'rm -rf build'",
        "X=rm echo hi",
        "cmd 2>&1 rm",
    ] {
        assert_eq!(bash(&engine, command), "allow", "{command:?}");
    }
}

#[test]
fn an_expansion_in_command_position_is_unparsed() {
    let engine = flat(PermissionMode::SkipDangerous, vec![bash_rule("rm *", Deny)]);
    let allow_all = flat(PermissionMode::Default, vec![bash_rule("*", Allow)]);
    for command in [
        "a=rm; $a -rf build",
        "$a -rf build",
        "${a} -rf build",
        "\"$a\" -rf build",
        "X=1 $a -rf build",
        "command $a -rf build",
        "! $a -rf build",
        ">log $a -rf build",
        "echo hi | $SHELL",
        "echo hi && x=$y $z",
    ] {
        assert_eq!(bash(&engine, command), "ask", "{command:?}");
        assert_eq!(bash(&allow_all, command), "ask", "{command:?}");
    }
    for command in [
        "echo $a",
        "echo \"$a\" | cat",
        "X=$a echo hi",
        "'$a' -rf build",
        "\\$a -rf build",
        "cmd $a",
    ] {
        assert_eq!(bash(&allow_all, command), "allow", "{command:?}");
    }
    // A blank after `>&` is unparsed syntax: no allow rule covers it.
    assert_eq!(bash(&allow_echo(), "echo hi >& 2"), "ask");
    assert_eq!(bash(&allow_all, "echo hi >& 2"), "ask");
}

#[test]
fn a_here_document_is_unparsed_and_never_allowed() {
    for command in [
        "echo hi <<EOF",
        "echo hi <<-EOF",
        "echo hi <<'EOF'",
        "echo hi <<EOF\ntouch pwned\nEOF",
    ] {
        assert_eq!(bash(&allow_echo(), command), "ask", "{command:?}");
    }
    let deny = flat(PermissionMode::SkipDangerous, vec![bash_rule("rm *", Deny)]);
    assert_eq!(bash(&deny, "rm -rf build <<EOF"), "deny");
}

#[test]
fn a_here_string_is_modelled_and_covered_by_the_rule_of_its_command() {
    assert_eq!(bash(&allow_echo(), "echo hi <<< word"), "allow");
    assert_eq!(bash(&allow_echo(), "echo hi <<< 'a;b'"), "allow");
    assert_eq!(bash(&allow_echo(), "echo hi <<< a; touch x"), "ask");
}

#[test]
fn a_clobber_redirect_is_unparsed_and_never_allowed() {
    assert_eq!(bash(&allow_echo(), "echo hi >| out"), "ask");
}

#[test]
fn a_pipe_and_ampersand_is_unparsed_and_never_allowed() {
    assert_eq!(bash(&allow_echo(), "echo hi |& cat"), "ask");
}

#[test]
fn a_backslash_inside_double_quotes_is_kept_before_an_ordinary_byte() {
    let engine = flat(
        PermissionMode::Default,
        vec![bash_rule("cat \"a\\nb\"", Allow)],
    );
    assert_eq!(bash(&engine, "cat \"a\\nb\""), "allow");
    assert_eq!(bash(&engine, "cat \"anb\""), "ask");
    assert_eq!(bash(&engine, "cat 'a\\nb'"), "allow");
}

/// Commands that try to run a second command under a lone `echo` allow
/// rule, in `default` mode. The ones expected `allow` run exactly one
/// command in `sh` (checked against dash and bash); the rest must ask.
const ECHO_VARIANTS: &[(&str, &str)] = &[
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
    ("echo \"$(touch pwned)\"", "ask"),
    ("echo `touch pwned`", "ask"),
    ("echo \"`touch pwned`\"", "ask"),
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
    ("echo hi; \"$t\" pwned", "ask"),
    ("echo hi | $SHELL", "ask"),
    ("X=1 echo hi", "ask"),
    ("time echo hi", "ask"),
    ("echo hi; function f { touch pwned; }; f", "ask"),
    ("echo 'a", "ask"),
    ("echo \"a", "ask"),
    ("echo a\\", "ask"),
    // One command each: quotes and escapes keep the operators literal.
    ("echo 'a;touch pwned'", "allow"),
    ("echo \"a;touch pwned\"", "allow"),
    ("echo a\\;touch pwned", "allow"),
    ("echo \"a\\\";touch pwned\"", "allow"),
    ("echo '\\'", "allow"),
    ("echo \"a\nb\"", "allow"),
    ("echo a\\&touch pwned", "allow"),
    ("echo \"\\$(touch pwned)\"", "allow"),
    ("echo \"\\`touch pwned\\`\"", "allow"),
    ("echo '$(touch pwned)' '`x`'", "allow"),
    ("echo a\\ b", "allow"),
    ("echo 1>&2", "allow"),
    ("echo hi 2>&1 >&2 <&0", "allow"),
    ("echo hi <<< 'a;touch pwned'", "allow"),
    ("echo hi\u{ff1b}touch pwned", "allow"),
    ("echo a\\>b", "allow"),
];

#[test]
fn a_lone_echo_allow_rule_never_covers_a_second_command() {
    let engine = allow_echo();
    let wrong: Vec<_> = ECHO_VARIANTS
        .iter()
        .filter(|(command, expected)| bash(&engine, command) != *expected)
        .collect();
    assert!(wrong.is_empty(), "{wrong:?}");
}

const PATTERN_OPERATORS: &[&str] = &["#", "##", "%", "%%", ":-", "/"];

#[test]
fn an_allow_rule_does_not_cover_a_command_behind_a_quote_in_a_parameter_pattern() {
    for op in PATTERN_OPERATORS {
        let command = format!("echo \"${{x{op}'\"'}}\" ; touch /tmp/pwned ; echo \\'");
        assert_eq!(bash(&allow_echo(), &command), "ask", "{command:?}");
    }
}

#[test]
fn a_deny_rule_covers_a_command_behind_a_quote_in_a_parameter_pattern() {
    let engine = flat(PermissionMode::SkipDangerous, vec![bash_rule("rm *", Deny)]);
    for op in PATTERN_OPERATORS {
        let command = format!("echo \"${{x{op}'\"'}}\" ; rm -rf build ; echo \\'");
        assert_ne!(bash(&engine, &command), "allow", "{command:?}");
    }
}

#[test]
fn a_parameter_expansion_in_double_quotes_with_a_quote_a_backslash_or_a_nested_expansion_is_unparsed()
 {
    for command in [
        "echo \"${x#'a'}\"",
        "echo \"${x:-\"a\"}\"",
        "echo \"${x#\\\"}\"",
        "echo \"${x:-${y}}\"",
        "echo \"${x/'/}\"",
        "echo \"${x#'\"'}",
    ] {
        assert_eq!(bash(&allow_echo(), command), "ask", "{command:?}");
    }
}

#[test]
fn a_plain_parameter_expansion_is_modelled_in_and_out_of_double_quotes() {
    for command in [
        "echo \"${x#a}\"",
        "echo \"${x:-b}\"",
        "echo \"${x%%.*}\" 'it''s'",
        "echo ${x#'\"'}",
        "echo ${x#\"'\"}",
        "echo ${x:-'a b'}",
    ] {
        assert_eq!(bash(&allow_echo(), command), "allow", "{command:?}");
    }
}

#[test]
fn a_quote_in_a_parameter_pattern_outside_double_quotes_does_not_hide_a_command() {
    for op in PATTERN_OPERATORS {
        let command = format!("echo ${{x{op}'\"'}} ; touch /tmp/pwned ; echo \\'");
        assert_eq!(bash(&allow_echo(), &command), "ask", "{command:?}");
        let command = format!("echo $(echo \"${{x{op}'\"'}}\" ; touch /tmp/pwned)");
        assert_eq!(bash(&allow_echo(), &command), "ask", "{command:?}");
    }
}

#[test]
fn a_deny_rule_covers_a_command_behind_a_quoted_wrapper() {
    let engine = flat(PermissionMode::SkipDangerous, vec![bash_rule("rm *", Deny)]);
    for command in [
        "env rm -rf build",
        "\\env rm -rf build",
        "e\"\"nv rm -rf build",
        "'command' rm -rf build",
        "\\command rm -rf build",
        "\\exec rm -rf build",
        "\"time\" rm -rf build",
        "env \\env rm -rf build",
    ] {
        assert_eq!(bash(&engine, command), "deny", "{command:?}");
    }
}

#[test]
fn a_deny_rule_covers_a_command_behind_a_wrapper_option_with_an_argument() {
    let engine = flat(PermissionMode::SkipDangerous, vec![bash_rule("rm *", Deny)]);
    for command in [
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
    ] {
        assert_eq!(bash(&engine, command), "deny", "{command:?}");
    }
}

#[test]
fn an_env_split_string_is_unparsed() {
    let engine = flat(PermissionMode::SkipDangerous, vec![bash_rule("rm *", Deny)]);
    for command in [
        "env -S 'rm -rf build'",
        "env --split-string='rm x'",
        "env -i -S 'rm x'",
    ] {
        assert_eq!(bash(&engine, command), "ask", "{command:?}");
    }
}

#[test]
fn a_deny_rule_does_not_match_the_argument_of_a_wrapper_option() {
    let bare = flat(PermissionMode::SkipDangerous, vec![bash_rule("rm", Deny)]);
    let glob = flat(PermissionMode::SkipDangerous, vec![bash_rule("rm *", Deny)]);
    for command in [
        "command -v rm",
        "command -pv rm",
        "env -u rm make",
        "exec -a rm sleep 1",
    ] {
        assert_eq!(bash(&bare, command), "allow", "{command:?}");
    }
    for command in [
        "env -u rm make",
        "exec -a rm sleep 1",
        "time -f rm make",
        "command -v rm x",
    ] {
        assert_eq!(bash(&glob, command), "allow", "{command:?}");
    }
}

#[test]
fn bash_only_command_prefixes_are_unparsed() {
    let engine = flat(PermissionMode::SkipDangerous, vec![bash_rule("rm *", Deny)]);
    for command in [
        "function f { rm -rf build; }; f",
        "coproc rm -rf build",
        "a[0]=1 rm -rf build",
        "{fd}>log rm -rf build",
        "{rm,-rf,build}",
        "r{m,} -rf build",
        "{1..3}",
        "X=1 function f { rm; }",
    ] {
        assert_eq!(bash(&engine, command), "ask", "{command:?}");
    }
    for command in [
        "echo {a,b}",
        "echo {1..3}",
        "echo 'function'",
        "echo a[0]=1",
        "git log {x}",
    ] {
        assert_eq!(bash(&engine, command), "allow", "{command:?}");
    }
}

#[test]
fn a_deny_glob_does_not_match_inside_a_quoted_argument() {
    let engine = flat(
        PermissionMode::SkipDangerous,
        vec![bash_rule("* --force", Deny)],
    );
    assert_eq!(bash(&engine, "git push --force"), "deny");
    for command in [
        "git commit -m \"do not use --force\"",
        "git commit -m 'never --force'",
        "echo 'x --force'",
        "echo \"x --force\"",
    ] {
        assert_eq!(bash(&engine, command), "allow", "{command:?}");
    }
}

#[test]
fn a_hash_after_an_escaped_blank_is_not_a_comment() {
    assert_eq!(bash(&allow_echo(), "echo a\\ #b"), "allow");
    assert_eq!(bash(&allow_echo(), "echo a\\;#b"), "allow");
    assert_eq!(bash(&allow_echo(), "echo a'b'#c"), "allow");
    assert_eq!(bash(&allow_echo(), "echo a #b"), "ask");
    assert_eq!(bash(&allow_echo(), "echo a ;#b"), "ask");
}

#[test]
fn an_allow_rule_does_not_cover_a_command_that_redirects_output_to_a_file() {
    for command in [
        "echo x > file",
        "echo x >file",
        "echo x >> file",
        "echo x 2> err",
        "echo x 2>>err",
        "echo x 1>f",
        "echo x >\"f\"",
        "echo x <>f",
        "echo x 2>&1 >file",
        "echo x > /dev/null/../f",
        "echo x | echo y > f",
        "echo x && echo y >> f",
        "echo x >",
    ] {
        assert_eq!(bash(&allow_echo(), command), "ask", "{command:?}");
    }
}

#[test]
fn an_allow_rule_covers_descriptor_duplication_and_the_standard_devices() {
    for command in [
        "echo x 2>&1",
        "echo x >&2",
        "echo x 1>&2",
        "echo x >&-",
        "echo x <&3",
        "echo x >/dev/null",
        "echo x 2>/dev/null",
        "echo x >> /dev/null",
        "echo x > /dev/stderr",
        "echo x 2>\"/dev/stdout\"",
        "echo x < in",
        "echo '>' \">f\" \\>f",
        "echo x 2>&1 >/dev/null",
    ] {
        assert_eq!(bash(&allow_echo(), command), "allow", "{command:?}");
    }
}

#[test]
fn an_allow_all_rule_covers_a_file_redirect_but_a_narrow_rule_does_not() {
    let allow_all = flat(PermissionMode::Default, vec![bash_rule("*", Allow)]);
    for command in [
        "pytest -q > test.log",
        "echo x >> f",
        "build 2>err.log | tee out.log > all.log",
        "pytest -q >/dev/null 2>&1",
    ] {
        assert_eq!(bash(&allow_all, command), "allow", "{command:?}");
    }
    for rule in ["echo", "echo *", "git *", "* x", "**x"] {
        let narrow = flat(PermissionMode::Default, vec![bash_rule(rule, Allow)]);
        for command in ["echo x > f", "git log >> f"] {
            assert_ne!(bash(&narrow, command), "allow", "{rule:?} {command:?}");
        }
    }
    // The first rule that matches a segment decides.
    let narrow_first = flat(
        PermissionMode::Default,
        vec![bash_rule("echo", Allow), bash_rule("*", Allow)],
    );
    assert_eq!(bash(&narrow_first, "echo x > f"), "ask");
    assert_eq!(bash(&narrow_first, "ls > f"), "allow");
}

#[test]
fn an_allow_all_rule_still_never_covers_unparsed_syntax() {
    let allow_all = flat(PermissionMode::Default, vec![bash_rule("*", Allow)]);
    for command in [
        "echo hi &>f touch M",
        "echo hi &>f",
        "echo hi >| f",
        "echo \\>& touch M",
        "echo hi #'\ntouch M\n#'",
        "echo \"${x#'\"'}\" ; touch M ; echo \\'",
    ] {
        assert_eq!(bash(&allow_all, command), "ask", "{command:?}");
    }
}

#[test]
fn ask_and_deny_rules_are_unchanged_by_a_file_redirect() {
    let skip = flat(PermissionMode::SkipDangerous, vec![bash_rule("*", Allow)]);
    assert_eq!(bash(&skip, "pytest -q > test.log"), "allow");
    let rules = flat(
        PermissionMode::SkipDangerous,
        vec![bash_rule("rm *", Deny), bash_rule("git push *", Ask)],
    );
    assert_eq!(bash(&rules, "rm -rf x > /dev/null"), "deny");
    assert_eq!(bash(&rules, "rm -rf x > f"), "deny");
    assert_eq!(bash(&rules, "git push > log"), "ask");
    assert_eq!(bash(&rules, "git status > log"), "allow");
}

const OLD_ARITHMETIC: &[&str] = &[
    r#"echo "${x:+$[}'"']}" ; touch M ; echo \'"#,
    r#"echo "${x:+$[}]'"'}" ; touch M ; echo \'"#,
    r#"echo "${x#$[}]'"'}" ; touch M ; echo \'"#,
    r#"echo "${x:-$((}'"']}" ; touch M ; echo \'"#,
    r#"echo "${x:-`}'"']}" ; touch M ; echo \'"#,
];

#[test]
fn an_allow_rule_does_not_cover_a_command_behind_a_brace_in_old_arithmetic() {
    for command in OLD_ARITHMETIC {
        assert_eq!(bash(&allow_echo(), command), "ask", "{command:?}");
    }
    assert_eq!(bash(&allow_echo(), r#"echo "${x:-a} $[1+1]""#), "allow");
}

#[test]
fn a_deny_rule_covers_a_command_behind_a_brace_in_old_arithmetic() {
    let engine = flat(
        PermissionMode::SkipDangerous,
        vec![bash_rule("touch *", Deny)],
    );
    for command in OLD_ARITHMETIC {
        assert_ne!(bash(&engine, command), "allow", "{command:?}");
    }
}

#[test]
fn an_allow_rule_does_not_cover_a_redirect_to_a_file_spelled_like_a_descriptor() {
    assert_eq!(bash(&allow_echo(), "echo hi >&2"), "allow");
    assert_eq!(bash(&allow_echo(), "echo hi >&-"), "allow");
    assert_eq!(bash(&allow_echo(), "echo hi >&M"), "ask");
    let allow_all = flat(PermissionMode::Default, vec![bash_rule("*", Allow)]);
    for command in [
        "echo hi >&2file",
        "echo hi >&1M",
        "echo hi 1>&2file",
        "echo hi >&2-M",
        "echo hi >&2\"file\"",
        "echo hi >&-file",
        "echo hi >&2-",
    ] {
        assert_eq!(bash(&allow_echo(), command), "ask", "{command:?}");
        // The allow-all rule treats them like any other file redirect.
        assert_eq!(bash(&allow_all, command), "allow", "{command:?}");
    }
}

#[test]
fn a_deny_rule_covers_a_command_behind_a_quoted_wrapper_option() {
    let engine = flat(
        PermissionMode::SkipDangerous,
        vec![bash_rule("touch *", Deny)],
    );
    for command in [
        "env -i touch M",
        "env X=1 touch M",
        "env \"-i\" touch M",
        "env \\-i touch M",
        "env '-u' X touch M",
        "env \"X=1\" touch M",
        "env 'X=1' touch M",
        "env X\\=1 touch M",
        "command \"-p\" touch M",
        "time \"-o\" f touch M",
        "exec \"-a\" n touch M",
    ] {
        assert_eq!(bash(&engine, command), "deny", "{command:?}");
    }
    for command in ["env \"-S\" 'touch M'", "env '-S' 'touch M'"] {
        assert_eq!(bash(&engine, command), "ask", "{command:?}");
    }
}

#[test]
fn a_deny_rule_covers_a_command_behind_a_bundled_or_abbreviated_wrapper_option() {
    let engine = flat(
        PermissionMode::SkipDangerous,
        vec![bash_rule("touch *", Deny)],
    );
    for command in [
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
    ] {
        assert_eq!(bash(&engine, command), "deny", "{command:?}");
    }
    for command in [
        "env -iS 'touch M'",
        "env --split-str 'touch M'",
        "env --split-string='touch M'",
    ] {
        assert_eq!(bash(&engine, command), "ask", "{command:?}");
    }
}

#[test]
fn a_deny_rule_does_not_match_the_argument_of_a_bundled_wrapper_option() {
    let engine = flat(PermissionMode::SkipDangerous, vec![bash_rule("rm", Deny)]);
    for command in [
        "env -iu rm make",
        "env --uns rm make",
        "exec -la rm sleep 1",
        "time -po rm make",
        "env \"-u\" rm make",
    ] {
        assert_eq!(bash(&engine, command), "allow", "{command:?}");
    }
}

#[test]
fn unparsed_floor_beats_a_generic_allow_in_any_layer_order() {
    let rules = vec![tool("Bash", Allow), bash_rule("rm -rf *", Deny)];
    let engine = flat(PermissionMode::SkipDangerous, rules);
    assert_eq!(bash(&engine, "true & rm -rf build"), "ask");
    assert_eq!(bash(&engine, "rm -rf build & x"), "deny");

    let split = |deny_first: bool| {
        let deny = PermissionLayer::new("project", [bash_rule("rm -rf *", Deny)]);
        let allow = PermissionLayer::new("runtime", [tool("Bash", Allow)]);
        let layers = if deny_first {
            vec![deny, allow]
        } else {
            vec![allow, deny]
        };
        layered(PermissionMode::SkipDangerous, layers)
    };
    assert_eq!(bash(&split(true), "true & rm -rf build"), "ask");
    assert_eq!(bash(&split(false), "true & rm -rf build"), "ask");
}

#[test]
fn unparsed_floor_ignores_rules_that_do_not_apply() {
    let skill_deny = ToolRule {
        skill: Some("deploy-prod".into()),
        ..ToolRule::new("*", Deny)
    };
    let engine = flat(
        PermissionMode::SkipDangerous,
        vec![tool("Bash", Allow), skill_deny.into()],
    );
    assert_eq!(bash(&engine, "true & rm -rf build"), "allow");

    for (role, unless_role, roles) in [
        (Some("blocked"), None, ["reader"]),
        (None, Some("trusted"), ["trusted"]),
    ] {
        let deny = BashRule {
            role: role.map(Into::into),
            unless_role: unless_role.map(Into::into),
            ..BashRule::new(["rm -rf *"], Deny)
        };
        let engine = flat(
            PermissionMode::SkipDangerous,
            vec![tool("Bash", Allow), deny.into()],
        );
        let args = json!({"command": "true & rm -rf build"});
        let who = principal(&roles);
        assert_eq!(
            decide(&engine, "Bash", ToolScope::Exec, args, Some(&who)),
            "allow"
        );
    }
}

#[test]
fn bash_segments_take_the_first_matching_bash_rule() {
    let rules = vec![
        bash_rule("git *", Allow),
        bash_rule("rm *", Deny),
        bash_rule("ls", Ask),
    ];
    let engine = flat(PermissionMode::Default, rules);
    assert_eq!(bash(&engine, "git status && git log"), "allow");
    assert_eq!(bash(&engine, "git status; rm -rf build"), "deny");
    assert_eq!(bash(&engine, "git status | ls"), "ask");
    // An unmatched segment abstains, so the mode default decides.
    assert_eq!(bash(&engine, "git status; make"), "ask");
    let skip = flat(
        PermissionMode::SkipDangerous,
        vec![bash_rule("git *", Allow)],
    );
    assert_eq!(bash(&skip, "git status; make"), "allow");
    assert_eq!(bash(&engine, " ; "), "ask");
}

#[test]
fn composite_role_filtering() {
    for (deny_role, expected) in [("runner", "deny"), ("admin", "allow")] {
        let allow = BashRule {
            role: Some("runner".into()),
            ..BashRule::new(["echo *"], Allow)
        };
        let deny = BashRule {
            role: Some(deny_role.into()),
            ..BashRule::new(["rm -rf *"], Deny)
        };
        let engine = flat(
            PermissionMode::SkipDangerous,
            vec![allow.into(), deny.into()],
        );
        let args = json!({"command": "echo safe; rm -rf build"});
        let who = principal(&["runner"]);
        assert_eq!(
            decide(&engine, "Bash", ToolScope::Exec, args, Some(&who)),
            expected
        );
    }
}

#[test]
fn role_guards_and_missing_principal() {
    let guarded = ToolRule {
        role: Some("operator".into()),
        unless_role: Some("suspended".into()),
        ..ToolRule::new("Write", Deny)
    };
    let engine = flat(PermissionMode::SkipDangerous, vec![guarded.into()]);
    let args = || json!({"file_path": "notes.txt"});
    let check = |p: Option<&Principal>| decide(&engine, "Write", ToolScope::Write, args(), p);
    assert_eq!(check(Some(&principal(&["operator"]))), "deny");
    assert_eq!(check(Some(&principal(&["operator", "suspended"]))), "allow");
    assert_eq!(check(Some(&principal(&["writer"]))), "allow");
    assert_eq!(check(None), "allow");

    let unless = ToolRule {
        unless_role: Some("trusted".into()),
        ..ToolRule::new("Write", Deny)
    };
    let engine = flat(PermissionMode::SkipDangerous, vec![unless.into()]);
    assert_eq!(
        decide(&engine, "Write", ToolScope::Write, args(), None),
        "deny"
    );
}

#[test]
fn layers_combine_with_deny_override() {
    let cases = [
        (
            vec![vec![], vec![tool("Bash", Deny)], vec![tool("Bash", Allow)]],
            "deny",
        ),
        (
            vec![vec![tool("Bash", Ask)], vec![tool("Bash", Allow)]],
            "allow",
        ),
        (
            vec![vec![tool("Bash", Allow)], vec![tool("Bash", Ask)]],
            "ask",
        ),
        (vec![vec![tool("Write", Deny)]], "allow"),
    ];
    for (layers, expected) in cases {
        let layers = layers
            .into_iter()
            .enumerate()
            .map(|(i, rules)| PermissionLayer::new(format!("l{i}"), rules))
            .collect();
        let engine = layered(PermissionMode::SkipDangerous, layers);
        assert_eq!(bash(&engine, "ls"), expected);
    }
}

#[test]
fn tool_rules_match_names_mcp_servers_and_skills() {
    let engine = flat(
        PermissionMode::SkipDangerous,
        vec![tool("mcp__ops_prod__*", Deny)],
    );
    let mcp = |name| decide(&engine, name, ToolScope::Exec, json!({}), None);
    assert_eq!(mcp("mcp__ops_prod__deploy"), "deny");
    assert_eq!(mcp("mcp__ops_prod__deploy__prod"), "deny");
    assert_eq!(mcp("mcp__ops_prod_evil__deploy"), "allow");

    let skill = ToolRule {
        skill: Some("deploy-prod".into()),
        ..ToolRule::new("Skill", Deny)
    };
    let engine = flat(PermissionMode::SkipDangerous, vec![skill.into()]);
    let load = |name| {
        decide(
            &engine,
            "Skill",
            ToolScope::Read,
            json!({"skill": name}),
            None,
        )
    };
    assert_eq!(load("/deploy-prod"), "deny");
    assert_eq!(load("deploy-prod"), "deny");
    assert_eq!(load("other"), "allow");
}

fn layers_strategy() -> impl Strategy<Value = Vec<PermissionLayer>> {
    let decision = prop::sample::select(vec![Allow, Deny, Ask]);
    let rule = prop_oneof![
        (
            prop::sample::select(vec!["Bash", "Write", "*"]),
            decision.clone()
        )
            .prop_map(|(name, d)| tool(name, d)),
        (
            prop::sample::select(vec!["rm *", "ls", "*", "git *"]),
            decision
        )
            .prop_map(|(pattern, d)| bash_rule(pattern, d)),
    ];
    prop::collection::vec(prop::collection::vec(rule, 0..4), 0..5).prop_map(|layers| {
        layers
            .into_iter()
            .enumerate()
            .map(|(i, rules)| PermissionLayer::new(format!("l{i}"), rules))
            .collect()
    })
}

proptest! {
    #[test]
    fn deny_wins_regardless_of_layer_order(
        (layers, shuffled) in layers_strategy().prop_flat_map(|l| (Just(l.clone()), Just(l).prop_shuffle())),
        command in prop::sample::select(vec!["ls", "rm -rf x; ls", "git status", "a & rm x", "echo $(id)"]),
        mode in prop::sample::select(vec![PermissionMode::Default, PermissionMode::SkipDangerous]),
    ) {
        let a = bash(&layered(mode, layers), command);
        let b = bash(&layered(mode, shuffled), command);
        prop_assert_eq!(a == "deny", b == "deny");
    }

    #[test]
    fn a_universal_deny_layer_always_denies(
        layers in layers_strategy(),
        at in 0usize..6,
    ) {
        let mut layers = layers;
        let at = at.min(layers.len());
        layers.insert(at, PermissionLayer::new("lock", [tool("*", Deny)]));
        prop_assert_eq!(bash(&layered(PermissionMode::SkipDangerous, layers), "ls"), "deny");
    }

    #[test]
    fn unparsed_commands_are_never_allowed_by_bash_rules(command in "[a-z &|;()$`'\"\\\\\n]{1,30}") {
        prop_assume!(shell::has_unparsed_construct(&command));
        let engine = flat(PermissionMode::SkipDangerous, vec![bash_rule("*", Allow)]);
        prop_assert_ne!(bash(&engine, &command), "allow");
    }
}
