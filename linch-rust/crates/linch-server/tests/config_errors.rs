//! Second-pass review of `fix/fix-workflow`: a secret in the wrong shape one
//! level above `TokenSource`, or in another field that holds a secret.

use linch_server::ServerConfig;

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
