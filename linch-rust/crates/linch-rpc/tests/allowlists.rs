//! The tenant allowlists on a remote connection: provider endpoints that
//! may receive the server's keys, workspace roots, and MCP stdio commands.
//! A local connection checks none of them.

mod common;

use std::path::PathBuf;

use common::*;
use linch_rpc::protocol::method;
use linch_rpc::{Access, Allowlists, McpCommand, code};
use serde_json::{Value, json};

/// `cargo test` builds the examples next to the test binaries' `deps`
/// directory.
fn example(name: &str) -> PathBuf {
    let mut dir = std::env::current_exe().expect("test binary path");
    dir.pop();
    if dir.ends_with("deps") {
        dir.pop();
    }
    dir.join("examples").join(name)
}

fn strings(items: &[&str]) -> Vec<String> {
    items.iter().map(|item| item.to_string()).collect()
}

/// Tenant `acme` with the scripted provider and `allow` applied.
fn acme(allow: impl FnOnce(Allowlists) -> Allowlists) -> Access {
    tenant("acme", allow(Allowlists::new().scripted_provider(true)))
}

/// A listener that accepts and drops every connection, so a client fails
/// at once instead of retrying a refused port.
async fn hangs_up() -> std::net::SocketAddr {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    tokio::spawn(async move {
        while let Ok((stream, _)) = listener.accept().await {
            drop(stream);
        }
    });
    address
}

fn config(extra: Value) -> Value {
    let mut config = scripted(vec![], None, "");
    config["namespace"] = json!("acme");
    for (key, value) in extra.as_object().unwrap() {
        config[key] = value.clone();
    }
    config
}

#[tokio::test]
async fn a_provider_endpoint_must_share_an_allowed_origin() {
    let urls = strings(&["http://127.0.0.1:9/v1"]);
    let rpc = Rpc::remote(acme(|allow| allow.provider_base_urls(&urls).unwrap()));
    let vllm = |url: &str| config(json!({"provider": {"id": "vllm", "base_url": url}}));
    // The key of a self-hosted server is optional, so these open.
    for allowed in ["http://127.0.0.1:9/v1", "http://127.0.0.1:9/other/v1/"] {
        assert_eq!(refused(&rpc, vllm(allowed)).await, None, "{allowed}");
        rpc.open(vllm(allowed)).await;
    }
    let outside = [
        "http://127.0.0.1:10/v1",
        "https://127.0.0.1:9/v1",
        "http://127.0.0.1:9@attacker.example/v1",
        "http://127.0.0.1.attacker.example:9/v1",
        "not a url",
    ];
    for url in outside {
        let kind = refused(&rpc, vllm(url)).await;
        assert_eq!(kind.as_deref(), Some("provider_base_url"), "{url}");
    }
    let deepseek = config(json!({"provider": {"id": "deepseek",
                                              "base_url": "https://api.deepseek.com.attacker.example"}}));
    assert_eq!(
        refused(&rpc, deepseek).await.as_deref(),
        Some("provider_base_url")
    );
    // The public endpoint is the key's own; it needs no entry.
    let public = config(json!({"provider": {"id": "deepseek"}}));
    assert_eq!(refused(&rpc, public).await, None);
}

#[tokio::test]
async fn a_workspace_must_lie_under_an_allowed_root() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path().join("tenants").join("acme");
    for sub in ["tenants/acme/project", "tenants/acme2", "tenants/beta"] {
        std::fs::create_dir_all(dir.path().join(sub)).unwrap();
    }
    std::os::unix::fs::symlink(dir.path().join("tenants/beta"), root.join("link")).unwrap();
    let roots = [root.clone()];
    let rpc = Rpc::remote(acme(|allow| allow.workspace_roots(&roots).unwrap()));
    for inside in [
        root.clone(),
        root.join("project"),
        root.join("project/../project"),
    ] {
        let open = config(json!({"workspace": inside}));
        assert_eq!(refused(&rpc, open).await, None, "{}", inside.display());
    }
    let outside = [
        dir.path().join("tenants/beta"),
        dir.path().join("tenants/acme2"),
        root.join("../beta"),
        root.join("link"),
        root.join("missing"),
        PathBuf::from("/"),
    ];
    for path in outside {
        let open = config(json!({"workspace": path}));
        let kind = refused(&rpc, open).await;
        assert_eq!(kind.as_deref(), Some("workspace"), "{}", path.display());
    }
}

#[tokio::test]
async fn a_config_dir_must_lie_under_an_allowed_root() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path().join("tenants").join("acme");
    let beta = dir.path().join("tenants/beta/.linch");
    for skill in [
        root.join("project/.linch/skills/notes"),
        beta.join("skills/secret"),
    ] {
        std::fs::create_dir_all(&skill).unwrap();
        let text = "---\ndescription: Notes.\n---\nNotes.";
        std::fs::write(skill.join("SKILL.md"), text).unwrap();
    }
    for sub in ["empty", "escape", "dangles"] {
        std::fs::create_dir_all(root.join(sub)).unwrap();
    }
    std::os::unix::fs::symlink(&beta, root.join("link")).unwrap();
    std::os::unix::fs::symlink(&beta, root.join("escape/.linch")).unwrap();
    std::os::unix::fs::symlink(dir.path().join("gone"), root.join("dangles/.linch")).unwrap();
    let roots = [root.clone()];
    let rpc = Rpc::remote(acme(|allow| allow.workspace_roots(&roots).unwrap()));
    let project = root.join("project");
    let inside = [
        json!({"skills": true, "workspace": project}),
        json!({"subagents": true, "workspace": project, "config_dir": ".linch"}),
        json!({"skills": true, "workspace": root, "config_dir": "project/.linch"}),
        json!({"skills": true, "config_dir": project.join(".linch")}),
        // No `.linch` at all: only the built-in skill loads.
        json!({"skills": true, "workspace": root.join("empty")}),
        json!({"skills": true}),
        // Unused while skills and subagents are off.
        json!({"workspace": project, "config_dir": beta}),
    ];
    for extra in inside {
        assert_eq!(refused(&rpc, config(extra.clone())).await, None, "{extra}");
    }
    // The checked path still loads: the `notes` skill changes the contract.
    let contract = |extra: Value| async {
        let opened = rpc.call(method::AGENT_OPEN, config(extra)).await.unwrap();
        opened["contract"].clone()
    };
    let notes = contract(json!({"skills": true, "config_dir": project.join(".linch")}));
    assert_ne!(notes.await, contract(json!({"skills": true})).await);
    let outside = [
        json!({"skills": true, "workspace": project, "config_dir": beta}),
        json!({"skills": true, "workspace": project, "config_dir": "../../beta/.linch"}),
        json!({"skills": true, "workspace": root, "config_dir": "link"}),
        json!({"subagents": true, "workspace": root, "config_dir": "link"}),
        json!({"skills": true, "workspace": root.join("escape")}),
        json!({"skills": true, "workspace": root.join("dangles")}),
        json!({"skills": true, "workspace": project, "config_dir": "missing"}),
        json!({"skills": true, "config_dir": beta}),
        json!({"skills": true, "config_dir": "/"}),
    ];
    for extra in outside {
        let kind = refused(&rpc, config(extra.clone())).await;
        assert_eq!(kind.as_deref(), Some("config_dir"), "{extra}");
    }
}

#[tokio::test]
async fn an_mcp_stdio_server_must_match_an_entry_exactly() {
    let program = example("mcp_server");
    assert!(program.exists(), "{} was not built", program.display());
    let listed = program.to_str().unwrap();
    let entry = McpCommand {
        command: listed.into(),
        args: strings(&["--tenant", "acme"]),
        env: strings(&["MCP_GREETING"]),
    };
    let entries = [entry];
    let rpc = Rpc::remote(acme(|allow| allow.mcp_commands(&entries).unwrap()));
    let stdio = |command: &str, args: &[&str], env: Value| {
        json!({"mcp_servers": [{"name": "tools", "transport": {"type": "stdio",
                                "command": command, "args": args, "env": env}}]})
    };
    let listed_args = ["--tenant", "acme"];
    let commands = [
        ("/bin/sh", &listed_args[..]),
        ("mcp_server", &listed_args),
        (&format!("{listed} "), &listed_args),
        (listed, &[]),
        (listed, &["--tenant", "beta"]),
        (listed, &["--tenant", "acme", "-c"]),
    ];
    for (command, args) in commands {
        let kind = refused(&rpc, config(stdio(command, args, json!({})))).await;
        assert_eq!(kind.as_deref(), Some("mcp_command"), "{command:?} {args:?}");
    }
    for env in [
        json!({"PATH": "/tmp"}),
        json!({"MCP_GREETING": "hi", "X": "1"}),
    ] {
        let kind = refused(&rpc, config(stdio(listed, &listed_args, env.clone()))).await;
        assert_eq!(kind.as_deref(), Some("mcp_env"), "{env}");
    }
    let open = config(stdio(listed, &listed_args, json!({"MCP_GREETING": "hi"})));
    let agent = rpc.open(open).await;
    let closed = rpc.call(method::AGENT_CLOSE, json!({"agent": agent})).await;
    assert_eq!(closed.unwrap(), json!({}));
}

#[tokio::test]
async fn a_local_connection_checks_no_allowlist() {
    let dir = tempfile::tempdir().unwrap();
    let rpc = Rpc::new();
    let open = json!({"namespace": "anything", "spec": {"model": MODEL},
                      "provider": {"id": "vllm", "base_url": "http://127.0.0.1:10/v1"},
                      "workspace": dir.path()});
    rpc.open(open).await;
}

#[tokio::test]
async fn the_scripted_provider_needs_the_servers_consent() {
    let refusing = Rpc::remote(tenant("acme", Allowlists::new()));
    assert_eq!(
        refused(&refusing, config(json!({}))).await.as_deref(),
        Some("provider")
    );
    let consenting = Rpc::remote(acme(|allow| allow));
    assert_eq!(refused(&consenting, config(json!({}))).await, None);
}

#[tokio::test]
async fn skip_dangerous_needs_the_servers_consent() {
    let dir = tempfile::tempdir().unwrap();
    let roots = [dir.path().to_path_buf()];
    let mode =
        |mode: &str| config(json!({"permissions": {"mode": mode, "project_root": dir.path()}}));
    let rpc = Rpc::remote(acme(|allow| allow.workspace_roots(&roots).unwrap()));
    for allowed in ["default", "accept_edits"] {
        assert_eq!(refused(&rpc, mode(allowed)).await, None, "{allowed}");
    }
    let kind = refused(&rpc, mode("skip_dangerous")).await;
    assert_eq!(kind.as_deref(), Some("permission_mode"));
    let error = rpc.call(method::AGENT_OPEN, mode("skip_dangerous")).await;
    let message = error.unwrap_err().to_string();
    assert!(
        message.starts_with("permission mode skip_dangerous is not"),
        "the config's spelling: {message}"
    );
    let only = Rpc::remote(acme(|allow| {
        let allow = allow.workspace_roots(&roots).unwrap();
        allow.permission_modes(&[linch::PermissionMode::SkipDangerous])
    }));
    assert_eq!(refused(&only, mode("skip_dangerous")).await, None);
    // No policy means mode `default`.
    let kind = refused(&only, config(json!({}))).await;
    assert_eq!(kind.as_deref(), Some("permission_mode"));
}

#[tokio::test]
async fn a_store_dsn_must_name_an_allowed_server() {
    let address = hangs_up().await;
    let dsns = [
        format!("postgres://acme@{address}/linch"),
        format!("postgres://pinned@{address}/linch?options=-c%20search_path%3Dacme"),
    ];
    let rpc = Rpc::remote(acme(|allow| allow.postgres_dsns(&dsns).unwrap()));
    let store = |dsn: String| config(json!({"stores": {"type": "postgres", "dsn": dsn}}));
    // Allowed: the open goes on and fails on the fake server.
    for ours in [
        format!("postgres://acme:secret@{address}/linch?sslmode=disable"),
        format!("postgres://{address}/linch?user=pinned&options=-c%20search_path%3Dacme"),
    ] {
        let error = rpc.call(method::AGENT_OPEN, store(ours)).await.unwrap_err();
        assert_eq!(refusal(error), (code::STORE_UNAVAILABLE, None));
    }
    let outside = [
        format!("postgres://acme@{address}/other"),
        "postgres://acme@127.0.0.1:1/linch".to_owned(),
        format!("postgres://acme@{address}/linch?host=10.255.255.1"),
        // Another role, or the server's own (no user named).
        format!("postgres://beta:secret@{address}/linch"),
        format!("postgres://acme@{address}/linch?user=postgres"),
        format!("postgres://{address}/linch"),
        // Startup options the entry does not have.
        format!("postgres://acme@{address}/linch?options=-c%20role%3Dpostgres"),
        format!("postgres://acme@{address}/linch?options[search_path]=beta"),
        format!("postgres://pinned@{address}/linch"),
        format!("postgres://pinned@{address}/linch?options=-c%20search_path%3Dbeta"),
        "not a dsn".to_owned(),
    ];
    for dsn in outside {
        let kind = refused(&rpc, store(dsn.clone())).await;
        assert_eq!(kind.as_deref(), Some("postgres"), "{dsn}");
    }
    // Memory stores need no entry.
    let memory = config(json!({"stores": {"type": "memory"}}));
    assert_eq!(refused(&rpc, memory).await, None);
}

#[tokio::test]
async fn a_store_dsn_must_name_an_entrys_tls_files_exactly() {
    let address = hangs_up().await;
    let two = "sslrootcert=/etc/linch/ca.pem&sslcert=/etc/linch/acme.crt";
    let files = format!("{two}&sslkey=/etc/linch/acme.key");
    let dsns = [
        format!("postgres://acme@{address}/linch"),
        format!("postgres://pinned@{address}/linch?{files}"),
    ];
    let rpc = Rpc::remote(acme(|allow| allow.postgres_dsns(&dsns).unwrap()));
    let store = |dsn: String| config(json!({"stores": {"type": "postgres", "dsn": dsn}}));
    // The same files, also under the other names the store reads them by.
    for ours in [
        format!("postgres://pinned@{address}/linch?sslmode=disable&{files}"),
        format!(
            "postgres://pinned@{address}/linch?sslmode=disable&ssl-ca=/etc/linch/ca.pem\
             &ssl-cert=/etc/linch/acme.crt&ssl-key=/etc/linch/acme.key"
        ),
    ] {
        let error = rpc.call(method::AGENT_OPEN, store(ours)).await.unwrap_err();
        assert_eq!(refusal(error), (code::STORE_UNAVAILABLE, None));
    }
    let outside = [
        // An entry without files refuses every one.
        format!("postgres://acme@{address}/linch?sslrootcert=/srv/beta/ca.pem"),
        format!("postgres://acme@{address}/linch?sslcert=/srv/beta/client.crt"),
        format!("postgres://acme@{address}/linch?ssl-key=/srv/beta/client.key"),
        // One missing, one other, or one named again later.
        format!("postgres://pinned@{address}/linch?{two}"),
        format!("postgres://pinned@{address}/linch?{two}&sslkey=/srv/beta/client.key"),
        format!("postgres://pinned@{address}/linch?{files}&sslkey=/srv/beta/client.key"),
    ];
    for dsn in outside {
        let kind = refused(&rpc, store(dsn.clone())).await;
        assert_eq!(kind.as_deref(), Some("postgres"), "{dsn}");
    }
}

#[tokio::test]
async fn an_mcp_http_url_must_share_an_allowed_origin() {
    let address = hangs_up().await;
    let urls = strings(&[&format!("http://{address}/")]);
    let rpc = Rpc::remote(acme(|allow| allow.mcp_urls(&urls).unwrap()));
    let http = |url: String| {
        json!({"mcp_servers": [{"name": "remote", "reconnect": {"attempts": 0},
                                "transport": {"type": "http", "url": url}}]})
    };
    let error = rpc
        .call(
            method::AGENT_OPEN,
            config(http(format!("http://{address}/mcp"))),
        )
        .await
        .unwrap_err();
    assert_eq!(refusal(error).0, code::MCP_UNAVAILABLE);
    let outside = config(http("http://127.0.0.1:1/mcp".into()));
    assert_eq!(refused(&rpc, outside).await.as_deref(), Some("mcp_url"));
}
