//! Policy tests: mode defaults, reasons, validation, wire shape, and the
//! decision key.

use serde_json::json;

use super::*;

#[test]
fn mode_defaults() {
    let mut policy = PermissionPolicy::new(PermissionMode::Default, "/project");
    policy.auto_allow.insert("TaskCreate".into());
    let modes = [
        (PermissionMode::Default, ["allow", "ask", "ask", "allow"]),
        (
            PermissionMode::AcceptEdits,
            ["allow", "allow", "ask", "allow"],
        ),
        (
            PermissionMode::SkipDangerous,
            ["allow", "allow", "allow", "allow"],
        ),
    ];
    for (mode, expected) in modes {
        policy.mode = mode;
        let engine = PermissionEngine::new(policy.clone()).unwrap();
        let got = [
            decide(&engine, "Read", ToolScope::Read, json!({}), None),
            decide(&engine, "Write", ToolScope::Write, json!({}), None),
            decide(&engine, "Bash", ToolScope::Exec, json!({}), None),
            decide(&engine, "TaskCreate", ToolScope::Write, json!({}), None),
        ];
        assert_eq!(got, expected, "{mode:?}");
    }
}

#[test]
fn decisions_carry_reasons() {
    let rules = vec![tool("Write", Deny), tool("Edit", Ask)];
    let engine = flat(PermissionMode::Default, rules);
    let args = input(json!({}));
    let deny = engine.evaluate(&PermissionCall::new("Write", ToolScope::Write, &args), None);
    assert_eq!(
        deny,
        PermissionDecision::Deny {
            reason: "Write denied by tool rule in layer \"runtime\"".into()
        }
    );
    let ask = engine.evaluate(&PermissionCall::new("Edit", ToolScope::Write, &args), None);
    assert!(matches!(ask, PermissionDecision::Ask { reason: Some(r) } if r.contains("runtime")));
}

#[test]
fn invalid_policies_are_rejected() {
    let reject = |rule: Rule| {
        let mut policy = PermissionPolicy::new(PermissionMode::Default, "/project");
        policy.layers.push(PermissionLayer::new("p", [rule]));
        match PermissionEngine::new(policy).unwrap_err() {
            PolicyError::Rule { problem, .. } => problem,
            other => panic!("{other:?}"),
        }
    };
    for pattern in [
        "Bash*",
        "mcp____*",
        "mcp__*__*",
        "mcp__ops__prod__*",
        "mcp___ops__*",
        "mcp__ops___*",
        " ",
    ] {
        assert!(
            matches!(reject(tool(pattern, Deny)), RuleProblem::ToolPattern { .. }),
            "{pattern:?}"
        );
    }
    let blank_role = ToolRule {
        role: Some(" ".into()),
        ..ToolRule::new("Write", Deny)
    };
    assert_eq!(
        reject(blank_role.into()),
        RuleProblem::Blank { field: "role" }
    );
    let skill_on_bash = ToolRule {
        skill: Some("x".into()),
        ..ToolRule::new("Bash", Deny)
    };
    assert_eq!(reject(skill_on_bash.into()), RuleProblem::SkillTool);
    assert!(matches!(
        reject(PathRule::new(["../x"], Deny).into()),
        RuleProblem::PathPattern { .. }
    ));
    let unknown_tool = PathRule {
        tools: Some(vec!["Bash".into()]),
        ..PathRule::new(["**"], Deny)
    };
    assert_eq!(
        reject(unknown_tool.into()),
        RuleProblem::NoPathField {
            tool: "Bash".into()
        }
    );
    assert_eq!(
        reject(BashRule::new(Vec::<String>::new(), Deny).into()),
        RuleProblem::Empty { field: "patterns" }
    );

    let relative = PermissionPolicy::new(PermissionMode::Default, "project");
    assert!(matches!(
        PermissionEngine::new(relative),
        Err(PolicyError::RelativeRoot { .. })
    ));
}

#[test]
fn policy_wire_shape() {
    let wire = json!({
        "mode": "accept_edits",
        "project_root": "/project",
        "layers": [{"name": "project", "rules": [
            {"type": "tool", "tool": "Bash", "decision": "deny", "role": "guest"},
            {"type": "path", "paths": ["secrets/**"], "decision": "deny"},
            {"type": "bash", "patterns": ["git *"], "decision": "allow"},
        ]}],
        "auto_allow": ["TaskList"],
    });
    let policy: PermissionPolicy = serde_json::from_value(wire.clone()).unwrap();
    assert_eq!(serde_json::to_value(&policy).unwrap(), wire);
    assert!(PermissionEngine::new(policy).is_ok());

    let rule = |v| serde_json::from_value::<Rule>(v);
    assert!(rule(json!({"type": "tool", "tool": "x", "decision": "allow", "arg": "y"})).is_err());
    assert!(rule(json!({"type": "regex", "decision": "allow"})).is_err());
    assert!(rule(json!({"type": "bash", "patterns": ["x"], "decision": "passthrough"})).is_err());
    assert!(serde_json::from_value::<PermissionMode>(json!("acceptEdits")).is_err());
}

#[test]
fn decision_key_is_canonical() {
    let args = input(json!({"b": [1, 2.5], "a": {"y": "é", "x": null}}));
    let key = permission_decision_key("Write", &args).unwrap();
    assert_eq!(key, r#"Write:{"a":{"x":null,"y":"é"},"b":[1,2.5]}"#);

    let mut deep = json!(0);
    for _ in 0..300 {
        deep = json!([deep]);
    }
    let args = input(json!({"deep": deep}));
    assert!(permission_decision_key("Write", &args).is_err());
}

proptest! {
    #[test]
    fn decision_key_ignores_key_order(
        entries in prop::collection::btree_map("[a-z]{1,4}", any::<i64>(), 0..8),
    ) {
        let forward: JsonObject = entries.iter().map(|(k, v)| (k.clone(), json!(v))).collect();
        let backward: JsonObject = entries.iter().rev().map(|(k, v)| (k.clone(), json!(v))).collect();
        let nest = |inner: &JsonObject| input(json!({"z": inner, "a": [inner]}));
        prop_assert_eq!(
            permission_decision_key("T", &forward).unwrap(),
            permission_decision_key("T", &backward).unwrap()
        );
        prop_assert_eq!(
            permission_decision_key("T", &nest(&forward)).unwrap(),
            permission_decision_key("T", &nest(&backward)).unwrap()
        );
    }
}
