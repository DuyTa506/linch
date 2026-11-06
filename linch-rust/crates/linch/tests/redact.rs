//! Serializing an `AgentConfig` hides its secrets (the DSN password, MCP
//! env and header values); the wire form keeps them.

use linch::AgentConfig;
use serde_json::{Value, json};

const SECRETS: [&str; 4] = ["hunter2", "hunter3", "tok-stdio", "tok-http"];

fn with_secrets() -> AgentConfig {
    serde_json::from_value(json!({
        "namespace": "t",
        "spec": {"model": "m"},
        "provider": {"id": "deepseek"},
        "stores": {"type": "postgres",
                   "dsn": "postgres://linch:hunter2@db.internal:5432/linch?password=hunter3&sslmode=disable"},
        "mcp_servers": [
            {"name": "git", "transport": {"type": "stdio", "command": "git-mcp",
                                          "env": {"GIT_TOKEN": "tok-stdio"}}},
            {"name": "web", "transport": {"type": "http", "url": "https://mcp.internal/mcp",
                                          "headers": {"Authorization": "Bearer tok-http"}}},
        ],
    }))
    .unwrap()
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
