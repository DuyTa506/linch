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
                          "max_agents": 5, "max_pool_connections": 4,
                          "max_provider_concurrency": 2,
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
    let pool = (acme.max_pool_connections, beta.max_pool_connections);
    assert_eq!(pool, (Some(4), None));
    let provider = (acme.max_provider_concurrency, beta.max_provider_concurrency);
    assert_eq!(provider, (Some(2), None));
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
fn a_tenant_lists_its_execution_backends_with_their_policies() {
    let text = json!({
        "http": "127.0.0.1:1",
        "tenants": {
            "acme": {"tokens": [{"value": "tok-acme-inline-0001"}],
                     "allow": {"execution": [{"backend": "sandbox", "memory_bytes": 1073741824},
                                             {"backend": "docker", "image": "alpine:3.20"}]}},
            "beta": {"tokens": [{"value": "tok-beta-inline-0001"}]}
        }
    })
    .to_string();
    let config = ServerConfig::from_json(&text).unwrap();
    let acme = config.tenants["acme"].allow.execution.as_ref().unwrap();
    let kinds: Vec<&str> = acme.iter().map(ExecutionConfig::kind).collect();
    assert_eq!(kinds, ["sandbox", "docker"]);
    assert_eq!(config.tenants["beta"].allow.execution, None);
    assert!(config.tokens(no_env).is_ok());
    let typo = json!({"http": "127.0.0.1:1", "tenants": {"a": {"tokens": [],
                      "allow": {"execution": [{"backend": "sandbox", "netwrok": true}]}}}});
    assert!(ServerConfig::from_json(&typo.to_string()).is_err());
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
fn memory_is_denied_by_default_and_its_cap_is_read() {
    let text = json!({"http": "127.0.0.1:1",
                      "tenants": {"plain": {"tokens": [{"value": "t"}]},
                                  "acme": {"tokens": [{"value": "u"}],
                                           "allow": {"memory": true, "max_memories": 50}}}});
    let config = ServerConfig::from_json(&text.to_string()).unwrap();
    assert!(!config.tenants["plain"].allow.memory);
    assert_eq!(config.tenants["plain"].allow.max_memories, None);
    let allow = &config.tenants["acme"].allow;
    assert_eq!((allow.memory, allow.max_memories), (true, Some(50)));
    assert!(allow.build().is_ok());
}

#[test]
fn memory_without_a_cap_is_refused_when_the_config_loads() {
    let text = json!({"http": "127.0.0.1:1",
                      "tenants": {"acme": {"tokens": [{"value": "t"}],
                                           "allow": {"memory": true}}}});
    let error = ServerConfig::from_json(&text.to_string()).unwrap_err();
    assert!(
        matches!(&error, ConfigError::MemoryNeedsCap { tenant } if tenant == "acme"),
        "{error}"
    );
    // A cap without the allowance is harmless: memory stays denied.
    let text = json!({"http": "127.0.0.1:1",
                      "tenants": {"acme": {"tokens": [{"value": "t"}],
                                           "allow": {"max_memories": 5}}}});
    assert!(ServerConfig::from_json(&text.to_string()).is_ok());
}

#[test]
fn a_cap_of_zero_is_refused_when_the_config_loads() {
    for field in [
        "max_runs",
        "max_connections",
        "max_agents",
        "max_pool_connections",
        "max_provider_concurrency",
        "max_schedules",
        "max_queued_jobs",
        "max_memories",
    ] {
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

#[test]
fn a_worker_section_runs_without_listeners_or_tokens() {
    let text = json!({
        "worker": {"dsn": {"env": "LINCH_WORKER_DSN"}, "max_jobs": 4, "queues": ["a", "b"],
                   "poll_interval_ms": 250, "local_namespaces": ["dev"]},
        "tenants": {"acme": {"allow": {"max_schedules": 3, "max_queued_jobs": 9,
                                       "min_schedule_interval_ms": 1000}}}
    })
    .to_string();
    let config = ServerConfig::from_json(&text).unwrap();
    let worker = config.worker.as_ref().unwrap();
    assert_eq!(worker.dsn, TokenSource::Env("LINCH_WORKER_DSN".into()));
    assert_eq!(
        (worker.max_jobs, worker.poll_interval_ms),
        (Some(4), Some(250))
    );
    assert_eq!(worker.local_namespaces, ["dev"]);
    let allow = &config.tenants["acme"].allow;
    assert_eq!(
        (
            allow.max_schedules,
            allow.max_queued_jobs,
            allow.min_schedule_interval_ms
        ),
        (Some(3), Some(9), Some(1000))
    );
    // No listener, so no tenant needs a token; the worker still knows it.
    let built = config.build(no_env).unwrap();
    assert!(built.tokens.is_empty());
    assert_eq!(built.tenants.len(), 1);
    assert_eq!(built.tenants[0].name(), "acme");
    // Without a worker section there is none.
    let listening = json!({"http": "127.0.0.1:1",
                           "tenants": {"acme": {"tokens": [{"value": "t"}]}}});
    let listening = ServerConfig::from_json(&listening.to_string()).unwrap();
    assert!(listening.worker.is_none());
    let unknown = json!({"worker": {"dsn": {"value": "postgres://x"}, "threads": 2},
                         "tenants": {}});
    let error = ServerConfig::from_json(&unknown.to_string()).unwrap_err();
    assert!(error.to_string().contains("unknown field"), "{error}");
}

#[test]
fn a_tenant_may_have_system_sections_of_the_operator() {
    let tenant = |sections: serde_json::Value| {
        json!({"http": "127.0.0.1:1",
               "tenants": {"acme": {"tokens": [{"value": "t"}],
                                    "allow": {"system_sections": sections}}}})
        .to_string()
    };
    let good = tenant(json!([{"name": "policy", "text": "Be kind.", "order": -1}]));
    let config = ServerConfig::from_json(&good).unwrap();
    let sections = &config.tenants["acme"].allow.system_sections;
    assert_eq!(sections.len(), 1);
    assert_eq!(sections[0].order, -1);
    assert!(config.build(no_env).is_ok());
    // A section the server could not send is refused when the config loads.
    let blank = tenant(json!([{"name": "policy", "text": " "}]));
    let config = ServerConfig::from_json(&blank).unwrap();
    assert!(config.build(no_env).is_err());
}

#[test]
fn a_tenant_is_given_the_tool_cache_and_its_bounds_by_the_config() {
    let text = |allow: serde_json::Value| {
        json!({"http": "127.0.0.1:1",
               "tenants": {"acme": {"tokens": [{"value": "tok-acme-inline-0001"}],
                                    "allow": allow}}})
        .to_string()
    };
    let open = ServerConfig::from_json(&text(json!({"tool_cache": true,
        "tool_cache_shared": true, "max_tool_cache_bytes": 4096})))
    .unwrap();
    let allow = &open.tenants["acme"].allow;
    assert!(allow.tool_cache && allow.tool_cache_shared);
    assert_eq!(allow.max_tool_cache_bytes, Some(4096));
    assert!(open.build(no_env).is_ok());
    let closed = ServerConfig::from_json(&text(json!({}))).unwrap();
    let allow = &closed.tenants["acme"].allow;
    assert!(!allow.tool_cache && !allow.tool_cache_shared);
    assert_eq!(allow.max_tool_cache_bytes, None);
    let zero = ServerConfig::from_json(&text(json!({"max_tool_cache_bytes": 0})));
    assert!(matches!(zero, Err(ConfigError::ZeroCap { .. })));
}

#[test]
fn a_tenant_may_have_a_required_policy() {
    let policy = json!({"prerequisites": [{"tool": "Edit", "requires": ["Read"]}],
                        "gates": [{"name": "tested", "succeeded": ["Test"]}]});
    let text = json!({"http": "127.0.0.1:1",
                      "tenants": {"acme": {"tokens": [{"value": "t"}],
                                           "allow": {"required_policy": policy}}}});
    let config = ServerConfig::from_json(&text.to_string()).unwrap();
    let required = (config.tenants["acme"].allow.required_policy.as_ref()).unwrap();
    assert_eq!(required.prerequisites[0].tool, "Edit");
    assert_eq!(required.gates[0].name, "tested");
    assert!(config.build(no_env).is_ok());
    let typo = json!({"http": "127.0.0.1:1",
                      "tenants": {"acme": {"tokens": [{"value": "t"}],
                                           "allow": {"required_policy": {"gate": []}}}}});
    assert!(ServerConfig::from_json(&typo.to_string()).is_err());
    let broken = json!({"http": "127.0.0.1:1",
                        "tenants": {"acme": {"tokens": [{"value": "t"}],
                                             "allow": {"required_policy": {"gates": [
                                                 {"name": "g", "check":
                                                  {"type": "matches", "pattern": "("}}]}}}}});
    let config = ServerConfig::from_json(&broken.to_string()).unwrap();
    let error = config.build(no_env).unwrap_err();
    assert!(error.to_string().contains("required_policy"), "{error}");
}
