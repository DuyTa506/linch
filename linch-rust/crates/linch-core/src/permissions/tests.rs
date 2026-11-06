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
    "pytest -q > test.log",
    "pytest -q >> test.log",
    "cat < input.txt",
    "cmd &> output.log",
    "git status | cat",
    "ruff check . | tee ruff.log",
    "npm test 2>&1 | tee npm.log",
    "find . -type f | sort",
    "grep -R TODO src | wc -l",
    "cargo test --all-features 2>&1",
    "python script.py < input.txt > output.txt",
    "sed -n '1,10p' README.md | cat",
    "pytest -q &>pytest.log",
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
