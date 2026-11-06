//! The tenant allowlists on a remote connection: provider endpoints that
//! may receive the server's keys, workspace roots, and MCP stdio commands.
//! A local connection checks none of them.

mod common;

use std::path::{Path, PathBuf};

use common::*;
use linch_rpc::protocol::method;
use linch_rpc::{Access, Allowlists, code};
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

fn acme(urls: &[&str], roots: &[&Path], commands: &[&str]) -> Access {
    let urls: Vec<String> = urls.iter().map(|url| url.to_string()).collect();
    let roots: Vec<PathBuf> = roots.iter().map(|root| root.to_path_buf()).collect();
    let commands: Vec<String> = commands.iter().map(|c| c.to_string()).collect();
    tenant("acme", Allowlists::new(&urls, &roots, &commands).unwrap())
}

fn config(extra: Value) -> Value {
    let mut config = scripted(vec![], None, "");
    config["namespace"] = json!("acme");
    for (key, value) in extra.as_object().unwrap() {
        config[key] = value.clone();
    }
    config
}

async fn refused(rpc: &Rpc, config: Value) -> Option<String> {
    match rpc.call(method::AGENT_OPEN, config).await {
        Ok(_) => None,
        Err(error) => {
            let (code, kind) = refusal(error);
            if code == code::NOT_ALLOWED {
                kind
            } else {
                None
            }
        }
    }
}

#[tokio::test]
async fn a_provider_endpoint_must_share_an_allowed_origin() {
    let rpc = Rpc::remote(acme(&["http://127.0.0.1:9/v1"], &[], &[]));
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
    let rpc = Rpc::remote(acme(&[], &[&root], &[]));
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
async fn an_mcp_stdio_command_must_be_listed() {
    let program = example("mcp_server");
    assert!(program.exists(), "{} was not built", program.display());
    let listed = program.to_str().unwrap();
    let rpc = Rpc::remote(acme(&[], &[], &[listed]));
    let stdio = |command: &str| {
        json!({"mcp_servers": [{"name": "tools", "transport": {"type": "stdio",
                                                               "command": command}}]})
    };
    for command in ["/bin/sh", "mcp_server", &format!("{listed} ")] {
        let kind = refused(&rpc, config(stdio(command))).await;
        assert_eq!(kind.as_deref(), Some("mcp_command"), "{command:?}");
    }
    let agent = rpc.open(config(stdio(listed))).await;
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
