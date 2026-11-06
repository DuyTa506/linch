use serde_json::json;

use super::*;

fn no_env(_: &str) -> Option<String> {
    None
}

#[test]
fn a_config_names_listeners_tenants_tokens_and_allowlists() {
    let text = json!({
        "socket": "/run/linch.sock",
        "http": "127.0.0.1:8731",
        "tenants": {
            "acme": {
                "tokens": [{"env": "ACME_TOKEN"}, {"value": "tok-acme-inline-0001"}],
                "allow": {"provider_base_urls": ["http://10.0.0.5:8000/v1"],
                          "permission_modes": ["skip_dangerous"],
                          "mcp_commands": ["/usr/local/bin/git-mcp"]}
            },
            "beta": {"tokens": [{"value": "tok-beta-inline-0001"}]}
        }
    })
    .to_string();
    let config = ServerConfig::from_json(&text).unwrap();
    assert_eq!(config.http, Some("127.0.0.1:8731".parse().unwrap()));
    let debug = format!("{config:?}");
    assert!(!debug.contains("tok-acme-inline-0001"), "{debug}");
    let env = |name: &str| (name == "ACME_TOKEN").then(|| "tok-acme-env-0001".to_owned());
    let tokens = config.tokens(env).unwrap();
    for (token, tenant) in [
        ("tok-acme-env-0001", "acme"),
        ("tok-acme-inline-0001", "acme"),
        ("tok-beta-inline-0001", "beta"),
    ] {
        assert_eq!(tokens.tenant(token).unwrap().name(), tenant);
    }
    assert!(tokens.tenant("tok-other").is_none());
}

#[test]
fn bad_configs_are_refused_without_echoing_tokens() {
    let cases = [
        (json!({"tenants": {}}), "neither socket nor http"),
        (
            json!({"http": "127.0.0.1:1", "tenants": {}, "tls": true}),
            "unknown field",
        ),
        (
            json!({"http": "127.0.0.1:1", "tenants": {"a": {"tokens": [{"file": "x"}]}}}),
            "unknown variant",
        ),
    ];
    for (text, expected) in cases {
        let error = ServerConfig::from_json(&text.to_string()).unwrap_err();
        assert!(error.to_string().contains(expected), "{error}");
    }
    let built = |tenants: serde_json::Value| {
        let text = json!({"http": "127.0.0.1:1", "tenants": tenants}).to_string();
        ServerConfig::from_json(&text)
            .unwrap()
            .tokens(no_env)
            .unwrap_err()
    };
    let error = built(json!({}));
    assert!(matches!(error, ConfigError::NoToken), "{error}");
    let error = built(json!({"acme": {"tokens": [{"env": "LINCH_P11C_UNSET"}]}}));
    assert!(error.to_string().contains("LINCH_P11C_UNSET"), "{error}");
    let error = built(json!({"a/b": {"tokens": [{"value": "tok-0001"}]}}));
    assert!(matches!(error, ConfigError::Access { .. }), "{error}");
    let error = built(json!({
        "acme": {"tokens": [{"value": "tok-shared-secret"}]},
        "beta": {"tokens": [{"value": "tok-shared-secret"}]}
    }));
    assert!(!error.to_string().contains("tok-shared-secret"), "{error}");
    let error = built(json!({"acme": {"tokens": [{"value": "t"}],
                                      "allow": {"workspace_roots": ["/nonexistent/p11c"]}}}));
    assert!(error.to_string().contains("/nonexistent/p11c"), "{error}");
}
