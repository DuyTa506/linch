//! Path rule tests: globs, the root boundary, and the field tables.

use serde_json::json;

use super::*;

#[test]
fn path_globs_resolve_parent_segments() {
    let allow = |p: &str| {
        let rule = PathRule {
            tools: Some(vec!["Write".into()]),
            ..PathRule::new([p], Allow)
        };
        flat(PermissionMode::Default, vec![rule.into()])
    };
    assert_eq!(write(&allow("config/*.json"), "config/app.json"), "allow");
    assert_eq!(
        write(&allow("config/*.json"), "config/prod/secrets.json"),
        "ask"
    );
    assert_eq!(
        write(&allow("config/**/*.json"), "config/prod/secrets.json"),
        "allow"
    );
    assert_eq!(
        write(&allow("config/**/*.json"), "config/app.json"),
        "allow"
    );

    let deny = flat(
        PermissionMode::SkipDangerous,
        vec![PathRule::new(["secrets/**"], Deny).into()],
    );
    assert_eq!(write(&deny, "src/../secrets/token.txt"), "deny");
    assert_eq!(write(&deny, "/project/secrets/token.txt"), "deny");
    assert_eq!(write(&deny, "src/token.txt"), "allow");
    for tool_name in ["write_file", "edit_file"] {
        let args = json!({"path": "/secrets/deploy.key"});
        assert_eq!(
            decide(&deny, tool_name, ToolScope::Write, args, None),
            "deny"
        );
    }
    // Default tools are write-scope tools with a path field: not Read.
    let read = json!({"file_path": "secrets/k"});
    assert_eq!(decide(&deny, "Read", ToolScope::Read, read, None), "allow");
}

#[test]
fn a_path_outside_the_root_never_matches_an_allow_rule() {
    for pattern in ["**", "!secrets/**", "/etc/**"] {
        let engine = flat(
            PermissionMode::Default,
            vec![PathRule::new([pattern], Allow).into()],
        );
        assert_eq!(write(&engine, "../../etc/passwd"), "ask", "{pattern}");
        assert_eq!(write(&engine, "/etc/passwd"), "ask", "{pattern}");
        let args = json!({"path": "/../../etc/passwd"});
        assert_eq!(
            decide(&engine, "write_file", ToolScope::Write, args, None),
            "ask"
        );
    }
    let outside = flat(
        PermissionMode::SkipDangerous,
        vec![PathRule::new(["!src/**"], Deny).into()],
    );
    assert_eq!(write(&outside, "/etc/passwd"), "deny");
    assert_eq!(write(&outside, "src/main.rs"), "allow");
    let absolute = flat(
        PermissionMode::SkipDangerous,
        vec![PathRule::new(["/etc/**"], Deny).into()],
    );
    assert_eq!(write(&absolute, "../etc/passwd"), "deny");

    let cwd_rule = flat(
        PermissionMode::Default,
        vec![PathRule::new(["**"], Allow).into()],
    );
    let args = input(json!({"file_path": "x"}));
    let mut call = PermissionCall::new("Write", ToolScope::Write, &args);
    call.cwd = Some("/tmp");
    assert_eq!(kind(&cwd_rule.evaluate(&call, None)), "ask");
    call.cwd = Some("sub");
    assert_eq!(kind(&cwd_rule.evaluate(&call, None)), "allow");
}

#[test]
fn field_tables_extend_the_builtins() {
    let mut policy = PermissionPolicy::new(PermissionMode::SkipDangerous, "/project");
    let field = PathField {
        field: "target".into(),
        default_to_cwd: false,
        virtual_root: false,
    };
    policy.path_fields.insert("save_doc".into(), field);
    policy.command_fields.insert("Shell".into(), "cmd".into());
    let rules = vec![
        PathRule::new(["secrets/**"], Deny).into(),
        bash_rule("rm *", Deny),
    ];
    policy.layers.push(PermissionLayer::new("project", rules));
    let engine = PermissionEngine::new(policy).unwrap();
    let doc = json!({"target": "secrets/a"});
    assert_eq!(
        decide(&engine, "save_doc", ToolScope::Write, doc, None),
        "deny"
    );
    let shell = json!({"cmd": "ls; rm -rf x"});
    assert_eq!(
        decide(&engine, "Shell", ToolScope::Exec, shell, None),
        "deny"
    );
    assert_eq!(bash(&engine, "rm x"), "deny");
}

#[test]
fn accept_edits_asks_for_a_write_outside_the_root() {
    let engine = flat(PermissionMode::AcceptEdits, vec![]);
    assert_eq!(write(&engine, "src/main.rs"), "allow");
    assert_eq!(write(&engine, "../etc/passwd"), "ask");
    assert_eq!(write(&engine, "/etc/passwd"), "ask");
    let args = json!({"path": "/../../etc/passwd"});
    assert_eq!(
        decide(&engine, "write_file", ToolScope::Write, args, None),
        "ask"
    );
    // Reads outside the root, and writes with no known path field, keep the
    // mode default.
    let read = json!({"file_path": "/etc/passwd"});
    assert_eq!(
        decide(&engine, "Read", ToolScope::Read, read, None),
        "allow"
    );
    let opaque = json!({"to": "/etc/passwd"});
    assert_eq!(
        decide(&engine, "save", ToolScope::Write, opaque, None),
        "allow"
    );

    let args = input(json!({"file_path": "x"}));
    let mut call = PermissionCall::new("Write", ToolScope::Write, &args);
    call.cwd = Some("/tmp");
    assert_eq!(kind(&engine.evaluate(&call, None)), "ask");

    // Only the accept_edits default changes; rules still decide first.
    let skip = flat(PermissionMode::SkipDangerous, vec![]);
    assert_eq!(write(&skip, "/etc/passwd"), "allow");
    let trusted = flat(PermissionMode::AcceptEdits, vec![tool("Write", Allow)]);
    assert_eq!(write(&trusted, "/etc/passwd"), "allow");
}
