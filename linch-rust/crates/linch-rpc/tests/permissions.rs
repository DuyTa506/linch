//! What a remote tenant's agents may run without its server's consent:
//! `Bash` only when the server allows exec tools, `auto_allow` and allow
//! rules only with `skip_dangerous`, and a project root only under an
//! allowed root. A local connection keeps every choice of its config.

mod common;

use std::path::Path;

use common::*;
use linch::{Event, PermissionMode};
use linch_rpc::{Access, Allowlists};
use serde_json::{Value, json};

/// Tenant `acme` with the scripted provider, `root` as its one workspace
/// root, and `allow` applied.
fn acme(root: &Path, allow: impl FnOnce(Allowlists) -> Allowlists) -> Access {
    let roots = [root.to_path_buf()];
    let base = Allowlists::new().scripted_provider(true);
    tenant("acme", allow(base.workspace_roots(&roots).unwrap()))
}

/// A scripted config in namespace `acme` with the members of `extra`.
fn config(extra: Value) -> Value {
    let mut config = scripted(vec![text_turn("ok")], None, "");
    config["namespace"] = json!("acme");
    for (key, value) in extra.as_object().unwrap() {
        config[key] = value.clone();
    }
    config
}

/// The tools a run of `config` lists in its `init` event.
async fn tools(rpc: &mut Rpc, config: Value) -> Vec<String> {
    let agent = rpc.open(config).await;
    let run_id = rpc.start(&agent, "s1", "hi").await;
    let ended = rpc.until_end(&run_id).await;
    match &ended.envelopes[0].event {
        Event::Init(init) => init.tools.clone(),
        other => panic!("first event {other:?}"),
    }
}

fn has(tools: &[String], name: &str) -> bool {
    tools.iter().any(|tool| tool == name)
}

#[tokio::test]
async fn a_remote_tenant_gets_bash_only_when_the_server_allows_exec_tools() {
    let dir = tempfile::tempdir().unwrap();
    let workspace = |bash: Value| {
        let mut config = config(json!({"workspace": dir.path()}));
        if !bash.is_null() {
            config["bash"] = bash;
        }
        config
    };
    let mut refusing = Rpc::remote(acme(dir.path(), |allow| allow));
    for bash in [Value::Null, json!(true)] {
        let listed = tools(&mut refusing, workspace(bash)).await;
        assert!(has(&listed, "Read") && !has(&listed, "Bash"), "{listed:?}");
    }
    let mut allowing = Rpc::remote(acme(dir.path(), |allow| allow.exec_tools(true)));
    let listed = tools(&mut allowing, workspace(Value::Null)).await;
    assert!(has(&listed, "Bash"), "{listed:?}");
    let listed = tools(&mut allowing, workspace(json!(false))).await;
    assert!(!has(&listed, "Bash"), "{listed:?}");
    let mut local = Rpc::new();
    let listed = tools(&mut local, workspace(Value::Null)).await;
    assert!(has(&listed, "Bash"), "{listed:?}");
}

#[tokio::test]
async fn auto_allow_and_allow_rules_need_skip_dangerous() {
    let dir = tempfile::tempdir().unwrap();
    let policy = |extra: Value| {
        let mut permissions = json!({"mode": "default", "project_root": dir.path()});
        for (key, value) in extra.as_object().unwrap() {
            permissions[key] = value.clone();
        }
        config(json!({"workspace": dir.path(), "permissions": permissions}))
    };
    let layer = |rule: Value| json!({"layers": [{"name": "tenant", "rules": [rule]}]});
    let grants = [
        (json!({"auto_allow": ["Write"]}), "auto_allow"),
        (
            layer(json!({"type": "tool", "tool": "*", "decision": "allow"})),
            "permission_rule",
        ),
        (
            layer(json!({"type": "bash", "patterns": ["*"], "decision": "allow"})),
            "permission_rule",
        ),
        (
            layer(json!({"type": "path", "paths": ["**"], "decision": "allow"})),
            "permission_rule",
        ),
    ];
    let rpc = Rpc::remote(acme(dir.path(), |allow| allow));
    for (extra, kind) in &grants {
        let refusal = refused(&rpc, policy(extra.clone())).await;
        assert_eq!(refusal.as_deref(), Some(*kind), "{extra}");
    }
    // Rules that only ask or deny take nothing from the server.
    for rule in [
        json!({"type": "tool", "tool": "Write", "decision": "deny"}),
        json!({"type": "path", "paths": ["secrets/**"], "decision": "ask"}),
    ] {
        rpc.open(policy(layer(rule))).await;
    }
    let modes = [PermissionMode::Default, PermissionMode::SkipDangerous];
    let consenting = Rpc::remote(acme(dir.path(), |allow| allow.permission_modes(&modes)));
    for (extra, _) in grants {
        consenting.open(policy(extra)).await;
    }
}

#[tokio::test]
async fn the_task_tools_and_the_deep_agent_need_skip_dangerous() {
    // Both put the task tools in `auto_allow` after the checks, so they
    // run unasked in every mode.
    let dir = tempfile::tempdir().unwrap();
    let asks = [
        json!({"tasks": true}),
        json!({"deep_agent": {"durable": false}}),
    ];
    let rpc = Rpc::remote(acme(dir.path(), |allow| allow));
    for extra in &asks {
        let refusal = refused(&rpc, config(extra.clone())).await;
        assert_eq!(refusal.as_deref(), Some("auto_allow"), "{extra}");
    }
    let modes = [PermissionMode::Default, PermissionMode::SkipDangerous];
    let consenting = Rpc::remote(acme(dir.path(), |allow| allow.permission_modes(&modes)));
    let local = Rpc::new();
    for extra in asks {
        consenting.open(config(extra.clone())).await;
        local.open(config(extra)).await;
    }
}

#[tokio::test]
async fn a_project_root_must_lie_under_an_allowed_root() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path().join("acme");
    for sub in ["acme/project", "beta"] {
        std::fs::create_dir_all(dir.path().join(sub)).unwrap();
    }
    std::os::unix::fs::symlink(dir.path().join("beta"), root.join("link")).unwrap();
    let rpc = Rpc::remote(acme(&root, |allow| allow));
    let with = |project_root: Value, workspace: bool| {
        let mut config = config(json!({"permissions": {"mode": "accept_edits",
                                                        "project_root": project_root}}));
        if workspace {
            config["workspace"] = json!(root.join("project"));
        }
        config
    };
    for inside in [json!(root), json!(root.join("project/../project"))] {
        for workspace in [false, true] {
            rpc.open(with(inside.clone(), workspace)).await;
        }
    }
    let outside = [
        json!("/"),
        json!(dir.path().join("beta")),
        json!(root.join("../beta")),
        json!(root.join("link")),
        json!(root.join("missing")),
        json!("acme/project"),
    ];
    for project_root in outside {
        for workspace in [false, true] {
            let kind = refused(&rpc, with(project_root.clone(), workspace)).await;
            assert_eq!(kind.as_deref(), Some("project_root"), "{project_root}");
        }
    }
    // A local connection checks nothing.
    Rpc::new().open(with(json!("/"), true)).await;
}
