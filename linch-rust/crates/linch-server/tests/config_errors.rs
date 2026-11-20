//! A secret in the wrong shape or in the wrong place of the config is not
//! repeated by the parse error, which is printed on stderr.

use linch_server::ServerConfig;
use serde_json::{Value, json};

/// Each case gives a secret where the config expects another JSON type;
/// the refusal, which `linch-server` prints on stderr, must not repeat it.
#[test]
fn a_config_error_does_not_echo_a_secret_one_level_up() {
    let cases = [
        (
            "tokens as one string",
            r#"{"socket": "/tmp/x.sock", "tenants": {"acme": {"tokens": "tok-SENTINEL-1"}}}"#,
        ),
        (
            "a tenant as its token",
            r#"{"socket": "/tmp/x.sock", "tenants": {"acme": "tok-SENTINEL-2"}}"#,
        ),
        (
            "worker as its dsn",
            r#"{"tenants": {}, "worker": "postgres://linch:pw-SENTINEL-3@db/linch"}"#,
        ),
        (
            "allow.postgres_dsns as one string",
            r#"{"socket": "/tmp/x.sock", "tenants": {"acme": {"tokens": [{"env": "T"}],
                "allow": {"postgres_dsns": "postgres://linch:pw-SENTINEL-4@db/linch"}}}}"#,
        ),
        (
            "tenants as a list of tokens",
            r#"{"socket": "/tmp/x.sock", "tenants": ["tok-SENTINEL-5"]}"#,
        ),
        (
            "an unknown key next to tokens that is the secret",
            r#"{"socket": "/tmp/x.sock", "tenants": {"acme": {"tokens": [{"env": "T"}],
                "tok-SENTINEL-6": true}}}"#,
        ),
    ];
    let mut leaked = Vec::new();
    for (what, text) in cases {
        let error = ServerConfig::from_json(text).unwrap_err().to_string();
        if error.contains("SENTINEL") {
            leaked.push(format!("{what}: {error}"));
        }
    }
    assert!(leaked.is_empty(), "secrets in the error text: {leaked:#?}");
}

/// What the operator needs to fix the file stays: the kind of mistake, what
/// was expected, and the line and column.
#[test]
fn a_config_error_keeps_what_was_expected_and_where() {
    let text = "{\n  \"socket\": \"/tmp/x.sock\",\n  \"tenants\": {\"acme\": {\"tokens\": \"tok-SENTINEL-7\"}}\n}";
    let error = ServerConfig::from_json(text).unwrap_err().to_string();
    assert!(!error.contains("SENTINEL"), "{error}");
    assert!(error.contains("invalid type: string"), "{error}");
    assert!(error.contains("expected"), "{error}");
    assert!(error.contains("line 3 column"), "{error}");

    let unknown = r#"{"socket": "/tmp/x.sock", "tok-SENTINEL-8": 1}"#;
    let error = ServerConfig::from_json(unknown).unwrap_err().to_string();
    assert!(!error.contains("SENTINEL"), "{error}");
    assert!(error.contains("unknown field"), "{error}");
    assert!(error.contains("expected one of"), "{error}");
    assert!(error.contains("`socket`"), "{error}");
}

/// A config with every section, valid for the parser.
fn full() -> Value {
    json!({
        "socket": "/tmp/x.sock",
        "http": "127.0.0.1:0",
        "server_id": "s1",
        "cache_salt_secret": {"env": "SALT"},
        "liveness": {"keepalive_idle_secs": 30, "ping_interval_ms": 1000},
        "worker": {
            "dsn": {"env": "DSN"}, "owner": "o", "queues": ["q"], "max_jobs": 1,
            "retention": {"batch_size": 1}, "local_namespaces": ["n"]
        },
        "tenants": {"acme": {
            "tokens": [{"env": "T"}],
            "allow": {
                "scripted_provider": true, "max_runs": 1,
                "workflow": {"max_records": 5, "max_cost_usd": 1.5},
                "provider_base_urls": ["https://x.test"],
                "permission_modes": ["default"],
                "workspace_roots": ["/tmp"],
                "postgres_dsns": ["postgres://u@h/d"],
                "mcp_commands": [{"command": "c", "args": ["a"]}],
                "mcp_urls": ["https://m.test"],
                "execution": [{"backend": "local"}],
                "system_sections": [{"name": "n", "text": "t", "order": 1, "cacheable": true}],
                "required_policy": {"max_corrections": 1}
            }
        }}
    })
}

/// Every place of `root` with `with` put there: as the value, as the one
/// element of a list, as the value of a map, and as a key next to the others.
fn walk(root: &Value, at: &mut Vec<String>, with: &str, out: &mut Vec<Value>) {
    let pointer: String = at.iter().map(|part| format!("/{part}")).collect();
    let here = root.pointer(&pointer).unwrap().clone();
    for put in [
        json!(with),
        json!([with]),
        json!({"env": with}),
        json!({with: with}),
    ] {
        let mut copy = root.clone();
        *copy.pointer_mut(&pointer).unwrap() = put;
        out.push(copy);
    }
    match here {
        Value::Object(map) => {
            let mut copy = root.clone();
            let object = copy.pointer_mut(&pointer).unwrap();
            (object.as_object_mut().unwrap()).insert(with.to_owned(), json!(1));
            out.push(copy);
            for key in map.keys() {
                at.push(key.clone());
                walk(root, at, with, out);
                at.pop();
            }
        }
        Value::Array(items) => {
            for index in 0..items.len() {
                at.push(index.to_string());
                walk(root, at, with, out);
                at.pop();
            }
        }
        _ => {}
    }
}

fn leaks(secret: &str) -> Vec<String> {
    let base = full();
    if let Err(error) = ServerConfig::from_json(&base.to_string()) {
        let text = error.to_string();
        assert!(!text.starts_with("invalid config:"), "control: {text}");
    }
    let mut configs = Vec::new();
    walk(&base, &mut Vec::new(), secret, &mut configs);
    let mut leaked = Vec::new();
    for config in configs {
        let Err(error) = ServerConfig::from_json(&config.to_string()) else {
            continue;
        };
        let text = error.to_string();
        if text.starts_with("invalid config:") && text.contains("SENTINEL") {
            leaked.push(text);
        }
    }
    leaked.sort();
    leaked.dedup();
    leaked
}

/// A plain secret, or one with a double quote, is repeated nowhere.
#[test]
fn no_parse_error_repeats_a_plain_or_quoted_secret() {
    for secret in ["pw-SENTINEL", "pw\"-SENTINEL", "pw\\\"-SENTINEL"] {
        let leaked = leaks(secret);
        assert!(leaked.is_empty(), "{} leaks: {leaked:#?}", leaked.len());
    }
}

/// A secret with a backtick, or with the words of the error's own text, is
/// repeated nowhere either.
#[test]
fn no_parse_error_repeats_a_secret_with_a_backtick() {
    for secret in [
        "pw`-SENTINEL",
        "pw`a`-SENTINEL",
        "pw, expected `tokens`-SENTINEL",
        "pw`, expected one of `x`-SENTINEL",
        "pw`, there are no fields-SENTINEL",
    ] {
        let leaked = leaks(secret);
        assert!(leaked.is_empty(), "{} leaks: {leaked:#?}", leaked.len());
    }
}
