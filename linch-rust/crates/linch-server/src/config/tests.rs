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
                "allow": {"exec_tools": true, "max_runs": 3, "max_connections": 2,
                          "max_agents": 5,
                          "provider_base_urls": ["http://10.0.0.5:8000/v1"],
                          "permission_modes": ["skip_dangerous"],
                          "mcp_commands": [{"command": "/usr/local/bin/git-mcp",
                                            "args": ["--read-only"],
                                            "env": ["GIT_TOKEN"]}]}
            },
            "beta": {"tokens": [{"value": "tok-beta-inline-0001"}]}
        }
    })
    .to_string();
    let config = ServerConfig::from_json(&text).unwrap();
    assert_eq!(config.http, Some("127.0.0.1:8731".parse().unwrap()));
    let (acme, beta) = (&config.tenants["acme"].allow, &config.tenants["beta"].allow);
    assert!(acme.exec_tools && !beta.exec_tools);
    assert_eq!((acme.max_runs, acme.max_connections), (Some(3), Some(2)));
    assert_eq!((beta.max_runs, beta.max_connections), (None, None));
    assert_eq!((acme.max_agents, beta.max_agents), (Some(5), None));
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
        // An MCP entry is an object: a program alone names no arguments.
        (
            json!({"http": "127.0.0.1:1", "tenants": {"a": {"tokens": [],
                   "allow": {"mcp_commands": ["/usr/local/bin/git-mcp"]}}}}),
            "invalid type",
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
    let error = built(json!({"acme": {"tokens": [{"value": "t"}],
                                      "allow": {"mcp_commands": [{"command": "/bin/x",
                                                                  "env": ["LD_PRELOAD"]}]}}}));
    assert!(error.to_string().contains("LD_PRELOAD"), "{error}");
}

#[test]
fn a_cap_of_zero_is_refused_when_the_config_loads() {
    for field in ["max_runs", "max_connections", "max_agents"] {
        let text = json!({"http": "127.0.0.1:1",
                          "tenants": {"acme": {"tokens": [{"value": "t"}],
                                               "allow": {field: 0}}}});
        let error = ServerConfig::from_json(&text.to_string()).unwrap_err();
        let expected = format!("tenant \"acme\": {field} is 0, which lets it do nothing");
        assert!(error.to_string().starts_with(&expected), "{error}");
    }
    let one = json!({"http": "127.0.0.1:1",
                     "tenants": {"acme": {"tokens": [{"value": "t"}],
                                          "allow": {"max_runs": 1, "max_connections": 1,
                                                    "max_agents": 1}}}});
    assert!(ServerConfig::from_json(&one.to_string()).is_ok());
}
