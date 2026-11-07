//! Serializing an `AgentConfig` hides its secrets (the DSN password, MCP
//! env and header values, MCP stdio args, MCP URL userinfo and query), and
//! so does `Debug`; the wire form keeps them.

use linch::AgentConfig;
use serde_json::{Value, json};

const SECRETS: [&str; 8] = [
    "hunter2",
    "hunter3",
    "tok-stdio",
    "tok-http",
    "tok-arg",
    "tok-user",
    "tok-pass",
    "tok-query",
];

fn with_secrets() -> AgentConfig {
    serde_json::from_value(json!({
        "namespace": "t",
        "spec": {"model": "m"},
        "provider": {"id": "deepseek"},
        "stores": {"type": "postgres",
                   "dsn": "postgres://linch:hunter2@db.internal:5432/linch?password=hunter3&sslmode=disable"},
        "mcp_servers": [
            {"name": "git", "transport": {"type": "stdio", "command": "git-mcp",
                                          "args": ["--token", "tok-arg"],
                                          "env": {"GIT_TOKEN": "tok-stdio"}}},
            {"name": "web", "transport": {"type": "http",
                                          "url": "https://tok-user:tok-pass@mcp.internal/mcp?key=tok-query",
                                          "headers": {"Authorization": "Bearer tok-http"}}},
        ],
    }))
    .unwrap()
}

#[test]
fn debug_hides_the_secrets_too() {
    let config = with_secrets();
    let debug = format!("{config:?}");
    assert!(
        SECRETS.iter().all(|secret| !debug.contains(secret)),
        "{debug}"
    );
    assert!(
        debug.contains("mcp.internal") && debug.contains("git-mcp"),
        "{debug}"
    );
}

#[test]
fn a_url_without_secrets_is_shown_as_is() {
    let mut config = with_secrets();
    config.mcp_servers.truncate(1);
    config.mcp_servers[0].transport = serde_json::from_value(json!({
        "type": "http", "url": "https://mcp.internal:8443/v1/mcp",
    }))
    .unwrap();
    let shown = serde_json::to_value(&config).unwrap();
    let url = &shown["mcp_servers"][0]["transport"]["url"];
    assert_eq!(url, "https://mcp.internal:8443/v1/mcp");
}

#[test]
fn serializing_a_config_hides_its_secrets() {
    let config = with_secrets();
    let text = serde_json::to_string(&config).unwrap();
    for secret in SECRETS {
        assert!(!text.contains(secret), "{text}");
    }
    // Where it connects and which variables and headers it sets stay visible.
    let shown: Value = serde_json::from_str(&text).unwrap();
    assert_eq!(
        shown["stores"]["dsn"],
        "postgres://linch:redacted@db.internal:5432/linch?password=redacted&sslmode=disable"
    );
    let servers = &shown["mcp_servers"];
    assert_eq!(
        servers[0]["transport"]["env"],
        json!({"GIT_TOKEN": "[redacted]"})
    );
    assert_eq!(
        servers[1]["transport"]["headers"],
        json!({"Authorization": "[redacted]"})
    );
    // The arguments' count, and the URL's origin and path, stay visible.
    assert_eq!(
        servers[0]["transport"]["args"],
        json!(["[redacted]", "[redacted]"])
    );
    assert_eq!(
        servers[1]["transport"]["url"],
        "https://redacted:redacted@mcp.internal/mcp?redacted"
    );
    for server in &config.mcp_servers {
        let text = serde_json::to_string(&server.transport).unwrap();
        assert!(
            SECRETS.iter().all(|secret| !text.contains(secret)),
            "{text}"
        );
    }
}

#[test]
fn a_dsn_that_is_not_a_url_is_hidden_whole() {
    let mut config = with_secrets();
    config.stores = serde_json::from_value(json!({
        "type": "postgres", "dsn": "host=db.internal password=hunter2",
    }))
    .unwrap();
    let shown = serde_json::to_value(&config).unwrap();
    assert_eq!(shown["stores"]["dsn"], "[redacted]");
}

#[test]
fn the_wire_form_keeps_the_secrets() {
    let config = with_secrets();
    let wire = config.to_value_with_secrets().unwrap();
    let text = wire.to_string();
    assert!(SECRETS.iter().all(|secret| text.contains(secret)), "{text}");
    assert_eq!(serde_json::from_value::<AgentConfig>(wire).unwrap(), config);
}
