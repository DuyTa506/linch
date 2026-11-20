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

#[test]
fn provider_header_values_are_hidden_but_the_wire_form_keeps_them() {
    let config: AgentConfig = serde_json::from_value(json!({
        "namespace": "t",
        "spec": {"model": "m"},
        "provider": {"id": "vllm", "base_url": "http://gw.internal/v1",
                     "request": {"default_headers": {"x-gateway-token": "tok-gw"},
                                 "extra_body": {"top_k": 5}, "timeout_ms": 1500}},
    }))
    .unwrap();
    let shown = serde_json::to_value(&config).unwrap();
    assert_eq!(
        shown["provider"]["request"]["default_headers"]["x-gateway-token"],
        "[redacted]"
    );
    assert_eq!(shown["provider"]["request"]["extra_body"]["top_k"], 5);
    assert!(!format!("{config:?}").contains("tok-gw"));
    let wire = config.to_value_with_secrets().unwrap();
    assert_eq!(
        wire["provider"]["request"]["default_headers"]["x-gateway-token"],
        "tok-gw"
    );
    assert_eq!(serde_json::from_value::<AgentConfig>(wire).unwrap(), config);
}

#[test]
fn a_provider_base_url_hides_its_userinfo_and_query_but_the_wire_form_keeps_them() {
    let config: AgentConfig = serde_json::from_value(json!({
        "namespace": "t",
        "spec": {"model": "m"},
        "provider": {"id": "vllm",
                     "base_url": "https://tok-user:tok-pass@gw.internal/v1?key=tok-query"},
    }))
    .unwrap();
    let text = serde_json::to_string(&config).unwrap();
    for secret in ["tok-user", "tok-pass", "tok-query"] {
        assert!(!text.contains(secret), "{text}");
        assert!(!format!("{config:?}").contains(secret), "{config:?}");
    }
    let shown = serde_json::to_value(&config).unwrap();
    assert_eq!(
        shown["provider"]["base_url"],
        "https://redacted:redacted@gw.internal/v1?redacted"
    );
    let wire = config.to_value_with_secrets().unwrap();
    assert_eq!(
        wire["provider"]["base_url"],
        "https://tok-user:tok-pass@gw.internal/v1?key=tok-query"
    );
    assert_eq!(serde_json::from_value::<AgentConfig>(wire).unwrap(), config);
}

#[test]
fn a_provider_base_url_without_secrets_is_written_as_given() {
    for url in [
        "http://gw.internal",
        "http://GW.internal:80/v1/",
        "https://api.x.test/v1",
    ] {
        let config: AgentConfig = serde_json::from_value(json!({
            "namespace": "t",
            "spec": {"model": "m"},
            "provider": {"id": "vllm", "base_url": url},
        }))
        .unwrap();
        let shown = serde_json::to_value(&config).unwrap();
        assert_eq!(shown["provider"]["base_url"], url);
    }
}

/// Where a token can sit in a `base_url` that is serialized or printed: the
/// query, the userinfo, escapes, the fragment, and a URL written without its
/// scheme (it parses as scheme `user`, with no host). The wire form keeps it.
#[test]
fn a_base_url_matrix_hides_the_token() {
    let mut leaks = Vec::new();
    for url in [
        "https://gw.internal/v1?tok-secret",
        "https://tok-secret@gw.internal/v1",
        "https://user:tok-secret@[::1]:8443/v1",
        "https://u:tok%2Dsecret@gw.internal/v1?k=tok%2Dsecret",
        "ws://u:tok-secret@gw.internal/v1",
        " https://u:tok-secret@gw.internal/v1",
        "https://gw.internal/v1#access_token=tok-secret",
        "user:tok-secret@gw.internal:8443/v1",
        "gw.internal:8443/v1?key=tok-secret",
        "//u:tok-secret@gw.internal/v1",
    ] {
        let config: AgentConfig = serde_json::from_value(json!({
            "namespace": "t",
            "spec": {"model": "m"},
            "provider": {"id": "vllm", "base_url": url},
        }))
        .unwrap();
        let shown = serde_json::to_value(&config).unwrap()["provider"]["base_url"].clone();
        let debug = format!("{:?}", config.provider);
        let wire = config.to_value_with_secrets().unwrap();
        assert_eq!(wire["provider"]["base_url"], url, "the wire form keeps the URL");
        for text in [shown.to_string(), debug] {
            if text.contains("tok-secret") || text.contains("tok%2Dsecret") {
                leaks.push(text);
            }
        }
    }
    assert!(leaks.is_empty(), "{leaks:#?}");
}

#[test]
fn an_mcp_url_with_a_fragment_or_no_scheme_is_hidden_too() {
    for url in [
        "https://mcp.internal/mcp#access_token=tok-secret",
        "user:tok-secret@mcp.internal:8443/mcp",
    ] {
        let config: AgentConfig = serde_json::from_value(json!({
            "namespace": "t",
            "spec": {"model": "m"},
            "provider": {"id": "deepseek"},
            "mcp_servers": [{"name": "web", "transport": {"type": "http", "url": url}}],
        }))
        .unwrap();
        let text = format!("{} {config:?}", serde_json::to_string(&config).unwrap());
        assert!(!text.contains("tok-secret"), "{text}");
        let wire = config.to_value_with_secrets().unwrap();
        assert_eq!(wire["mcp_servers"][0]["transport"]["url"], url);
    }
}
