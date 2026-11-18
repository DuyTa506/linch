//! The tenant allowlists on a remote connection: provider endpoints that
//! may receive the server's keys, workspace roots, and MCP stdio commands.
//! A local connection checks none of them.

mod common;

use std::num::NonZeroU32;
use std::path::PathBuf;

use common::*;
use linch_rpc::protocol::method;
use linch_rpc::{Access, Allowlists, ClientError, McpCommand, code};
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
    for allowed in ["http://127.0.0.1:9/v1", "http://127.0.0.1:9/v1/"] {
        assert_eq!(refused(&rpc, vllm(allowed)).await, None, "{allowed}");
        rpc.open(vllm(allowed)).await;
    }
    let outside = [
        // The entry names a path, so it is the one path allowed.
        "http://127.0.0.1:9/other/v1/",
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
    let open = json!({"namespace": "anything", "spec": {"model": MODEL, "max_turns": 100},
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
        format!("postgres://acme@{address}/linch?sslmode=disable"),
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
        format!("postgres://pinned@{address}/linch?sslmode=disable&{files}"),
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
async fn a_store_dsn_must_ask_for_tls_at_least_as_strictly_as_its_entry() {
    let address = hangs_up().await;
    let dsns = [
        // No `sslmode`: the store's default, `prefer`.
        format!("postgres://acme@{address}/linch"),
        format!("postgres://pinned@{address}/linch?sslmode=require"),
        format!("postgres://strict@{address}/linch?ssl-mode=verify-ca"),
    ];
    let rpc = Rpc::remote(acme(|allow| allow.postgres_dsns(&dsns).unwrap()));
    let store = |dsn: String| config(json!({"stores": {"type": "postgres", "dsn": dsn}}));
    let modes = |user: &str, modes: &[&str]| -> Vec<String> {
        (modes.iter())
            .map(|mode| match *mode {
                "" => format!("postgres://{user}@{address}/linch"),
                mode => format!("postgres://{user}@{address}/linch?sslmode={mode}"),
            })
            .collect()
    };
    // The entry's mode or a stricter one: the open goes on and fails on
    // the fake server.
    let allowed = [
        modes(
            "acme",
            &["", "prefer", "require", "verify-ca", "verify-full"],
        ),
        modes("pinned", &["require", "Verify-CA", "verify-full"]),
        modes("strict", &["verify-ca", "verify-full"]),
    ];
    for dsn in allowed.concat() {
        let kind = refused(&rpc, store(dsn.clone())).await;
        assert_eq!(kind, None, "{dsn}");
    }
    let weaker = [
        modes("acme", &["disable", "allow"]),
        modes("pinned", &["", "disable", "allow", "prefer"]),
        modes("strict", &["prefer", "require"]),
    ];
    for dsn in weaker.concat() {
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

#[tokio::test]
async fn a_store_pool_may_not_exceed_the_tenants_max_pool_connections() {
    let address = hangs_up().await;
    let dsn = format!("postgres://acme@{address}/linch?sslmode=disable");
    let dsns = [dsn.clone()];
    let store = |max: Option<u32>| {
        let mut stores = json!({"type": "postgres", "dsn": dsn});
        if let Some(max) = max {
            stores["max_connections"] = json!(max);
        }
        config(json!({ "stores": stores }))
    };
    let cap = NonZeroU32::new(5).unwrap();
    let rpc = Rpc::remote(acme(|allow| {
        let allow = allow.postgres_dsns(&dsns).unwrap();
        allow.max_pool_connections(cap)
    }));
    // Past the cap: refused before any connect, naming the cap.
    let error = rpc.call(method::AGENT_OPEN, store(Some(6))).await;
    let linch_rpc::ClientError::Rpc(error) = error.unwrap_err() else {
        panic!("expected an error object");
    };
    assert_eq!(error.code, code::NOT_ALLOWED);
    let data = json!({"kind": "pool_connections", "max": 5});
    assert_eq!(error.data, Some(data));
    let message = &error.message;
    assert!(message.contains("max_pool_connections"), "{message}");
    // At the cap, below it, or absent: admitted, so the open goes on to
    // the fake server and fails there.
    for max in [Some(5), Some(1), None] {
        let error = rpc.call(method::AGENT_OPEN, store(max)).await.unwrap_err();
        assert_eq!(refusal(error), (code::STORE_UNAVAILABLE, None), "{max:?}");
    }

    // Without the field the cap is the server's default pool size.
    let default = linch::postgres::default_max_connections().get();
    let rpc = Rpc::remote(acme(|allow| allow.postgres_dsns(&dsns).unwrap()));
    let kind = refused(&rpc, store(Some(default + 1))).await;
    assert_eq!(kind.as_deref(), Some("pool_connections"));
    let error = rpc.call(method::AGENT_OPEN, store(Some(default))).await;
    assert_eq!(refusal(error.unwrap_err()), (code::STORE_UNAVAILABLE, None));
}

#[tokio::test]
async fn a_task_ledger_needs_the_tenants_allowlist() {
    use linch::PermissionMode::{Default, SkipDangerous};
    let ledger = config(json!({"tasks": true, "task_ledger": {"list": "ledger"}}));
    let modes = [Default, SkipDangerous];
    let denied = Rpc::remote(acme(|allow| allow.permission_modes(&modes)));
    let kind = refused(&denied, ledger.clone()).await;
    assert_eq!(kind.as_deref(), Some("task_ledger"));
    // The same agent without the ledger opens: only the ledger is refused.
    let plain = config(json!({"tasks": true}));
    assert!(denied.call(method::AGENT_OPEN, plain).await.is_ok());

    let allowed = Rpc::remote(acme(|allow| {
        allow.permission_modes(&modes).task_ledger(true)
    }));
    assert!(allowed.call(method::AGENT_OPEN, ledger).await.is_ok());
}

/// A tenant's run has to end on `max_turns` or a budget cap: a model that
/// keeps calling a tool would otherwise run on the operator's key until the
/// loop guard, which an input that keeps changing evades. A local connection
/// is not held to it.
#[tokio::test]
async fn a_tenant_agent_needs_max_turns_or_a_budget_cap() {
    let unbounded = || {
        let mut config = config(json!({}));
        config["spec"].as_object_mut().unwrap().remove("max_turns");
        config
    };
    let tenant = Rpc::remote(acme(|allow| allow));
    let error = tenant
        .call(method::AGENT_OPEN, unbounded())
        .await
        .unwrap_err();
    let ClientError::Rpc(error) = error else {
        panic!("not an error object");
    };
    assert_eq!(error.code, code::NOT_ALLOWED);
    assert_eq!(error.data, Some(json!({"kind": "run_bound"})));
    assert!(error.message.contains("max_turns"), "{}", error.message);
    assert!(error.message.contains("budget"), "{}", error.message);
    for bound in [
        json!({"max_turns": 8}),
        json!({"budget": {"max_tokens": 1000}}),
        json!({"budget": {"max_cost_usd": 1.0}}),
    ] {
        let mut config = unbounded();
        for (key, value) in bound.as_object().unwrap() {
            config["spec"][key] = value.clone();
        }
        // Admission passes; a cost cap on the unpriced scripted model then
        // fails the build, which is another refusal.
        assert_eq!(refused(&tenant, config).await, None, "{bound}");
    }
    // A trusted local connection keeps its behaviour.
    Rpc::new().open(unbounded()).await;
}

/// A vLLM provider at the one URL the tenant may use, with `extra` merged
/// into the provider config.
fn vllm_with(extra: Value) -> Value {
    let mut provider = json!({"id": "vllm", "base_url": "http://127.0.0.1:9/v1"});
    for (key, value) in extra.as_object().unwrap() {
        provider[key] = value.clone();
    }
    config(json!({ "provider": provider }))
}

fn request_allowed() -> Rpc {
    let urls = strings(&["http://127.0.0.1:9/v1"]);
    Rpc::remote(acme(|allow| {
        allow
            .provider_base_urls(&urls)
            .unwrap()
            .provider_request(true)
    }))
}

#[tokio::test]
async fn provider_request_settings_are_denied_unless_the_tenant_may_set_them() {
    let urls = strings(&["http://127.0.0.1:9/v1"]);
    let denied = Rpc::remote(acme(|allow| allow.provider_base_urls(&urls).unwrap()));
    for extra in [
        json!({"request": {"default_headers": {"x-gateway-token": "t"}}}),
        json!({"request": {"extra_body": {"top_k": 5}}}),
        json!({"request": {"timeout_ms": 1000}}),
        json!({"chat": {"parallel_tool_calls": false}}),
        json!({"chat": {"include_stream_options": false}}),
        json!({"chat": {"sampling_params": {"top_k": 5}}}),
    ] {
        let kind = refused(&denied, vllm_with(extra.clone())).await;
        assert_eq!(kind.as_deref(), Some("provider_request"), "{extra}");
    }
    // The same endpoint without settings opens as before.
    assert_eq!(refused(&denied, vllm_with(json!({}))).await, None);

    let allowed = request_allowed();
    let request = json!({"default_headers": {"x-gateway-token": "t"},
                         "extra_body": {"top_k": 5}, "timeout_ms": 1000});
    assert_eq!(
        refused(&allowed, vllm_with(json!({"request": request}))).await,
        None
    );
    // Allowed to set them, still not to point the key elsewhere.
    let other = config(
        json!({"provider": {"id": "vllm", "base_url": "http://127.0.0.1:10/v1",
                                            "request": request}}),
    );
    assert_eq!(
        refused(&allowed, other).await.as_deref(),
        Some("provider_base_url")
    );
}

#[tokio::test]
async fn a_tenant_cannot_set_a_header_that_carries_a_credential_or_reroutes() {
    let rpc = request_allowed();
    for name in [
        "Authorization",
        "X-Api-Key",
        "x-goog-api-key",
        "api-key",
        "Cookie",
        "proxy-authorization",
        "Proxy-Connection",
        "forwarded",
        "X-Forwarded-For",
        "x-forwarded-host",
    ] {
        let headers = json!({name: "value-123456"});
        let config = vllm_with(json!({"request": {"default_headers": headers}}));
        let kind = refused(&rpc, config).await;
        assert_eq!(kind.as_deref(), Some("provider_request"), "{name}");
    }
    let ok = json!({"anthropic-beta": "tools-2024", "x-gateway-token": "t"});
    let config = vllm_with(json!({"request": {"default_headers": ok}}));
    assert_eq!(refused(&rpc, config).await, None);
}

#[tokio::test]
async fn provider_request_settings_are_bounded() {
    // The bounds are the documented ones: 16 headers of 1 KiB, 16 KiB of
    // body fields, a 10 minute timeout.
    const MAX_REQUEST_HEADERS: usize = 16;
    const MAX_REQUEST_HEADER_BYTES: usize = 1024;
    const MAX_REQUEST_BODY_BYTES: usize = 16 * 1024;
    const MAX_REQUEST_TIMEOUT_MS: u64 = 600_000;
    let rpc = request_allowed();
    let too_many: serde_json::Map<String, Value> = (0..=MAX_REQUEST_HEADERS)
        .map(|n| (format!("x-h{n}"), json!("v")))
        .collect();
    let long = "v".repeat(MAX_REQUEST_HEADER_BYTES);
    let big = "b".repeat(MAX_REQUEST_BODY_BYTES);
    let cases = [
        json!({"request": {"default_headers": too_many}}),
        json!({"request": {"default_headers": {"x-long": long}}}),
        json!({"request": {"extra_body": {"blob": big}}}),
        json!({"chat": {"sampling_params": {"blob": big}}}),
        json!({"chat": {"chat_template_kwargs": {"blob": big}}}),
        json!({"request": {"timeout_ms": MAX_REQUEST_TIMEOUT_MS + 1}}),
    ];
    for extra in cases {
        let kind = refused(&rpc, vllm_with(extra.clone())).await;
        assert_eq!(kind.as_deref(), Some("provider_request"), "{extra:.80}");
    }
    let at_the_cap = json!({"request": {"timeout_ms": MAX_REQUEST_TIMEOUT_MS}});
    assert_eq!(refused(&rpc, vllm_with(at_the_cap)).await, None);
}

#[tokio::test]
async fn a_provider_question_with_request_settings_needs_the_allowlist_too() {
    let url = "http://127.0.0.1:9/v1".to_owned();
    let provider = json!({"id": "vllm", "base_url": url,
                          "request": {"extra_body": {"top_k": 5}}});
    let models = json!({"provider": provider});
    let one = json!({"provider": provider, "model": "m"});
    let asking = Allowlists::new()
        .provider_info(true)
        .provider_base_urls(std::slice::from_ref(&url))
        .unwrap();
    let rpc = Rpc::remote(tenant("acme", asking));
    for (method, params) in [
        (method::PROVIDER_CAPABILITIES, &one),
        (method::PROVIDER_MODELS, &models),
        (method::PROVIDER_MODEL, &one),
    ] {
        let error = rpc.call(method, params.clone()).await.unwrap_err();
        assert_eq!(
            refusal(error),
            (code::NOT_ALLOWED, Some("provider_request".into())),
            "{method}"
        );
    }
}
