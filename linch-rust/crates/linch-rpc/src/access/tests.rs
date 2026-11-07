use std::path::PathBuf;
use std::sync::Arc;

use super::*;

fn tenant(name: &str) -> Arc<Tenant> {
    Arc::new(Tenant::new(name, Allowlists::default()).unwrap())
}

#[test]
fn tenant_names_are_plain_and_never_hold_a_slash() {
    for good in ["acme", "a.b_c-9", &"x".repeat(64)] {
        assert!(Tenant::new(good, Allowlists::default()).is_ok(), "{good}");
    }
    for bad in ["", "acme/support", "a b", "é", &"x".repeat(65)] {
        let error = Tenant::new(bad, Allowlists::default()).unwrap_err();
        assert!(matches!(error, AccessError::TenantName { .. }), "{bad}");
    }
}

#[test]
fn a_token_names_its_tenant_and_nothing_else_does() {
    let mut tokens = Tokens::new();
    assert!(tokens.is_empty());
    tokens.insert("tok-acme-0001", tenant("acme")).unwrap();
    tokens.insert("tok-acme-0002", tenant("acme")).unwrap();
    tokens.insert("tok-beta-0001", tenant("beta")).unwrap();
    assert_eq!(tokens.tenant("tok-acme-0002").unwrap().name(), "acme");
    assert_eq!(tokens.tenant("tok-beta-0001").unwrap().name(), "beta");
    for wrong in [
        "",
        "tok-acme-000",
        "tok-acme-00011",
        "TOK-ACME-0001",
        " tok-beta-0001",
    ] {
        assert!(tokens.tenant(wrong).is_none(), "{wrong:?}");
    }
}

#[test]
fn blank_and_repeated_tokens_are_refused_without_echoing_them() {
    let mut tokens = Tokens::new();
    let blank = tokens.insert("  ", tenant("acme")).unwrap_err();
    assert!(matches!(blank, AccessError::BlankToken { .. }));
    tokens.insert("tok-shared-secret", tenant("acme")).unwrap();
    let repeated = tokens
        .insert("tok-shared-secret", tenant("beta"))
        .unwrap_err();
    assert!(matches!(repeated, AccessError::DuplicateToken { .. }));
    assert!(!repeated.to_string().contains("tok-shared-secret"));
    let debug = format!("{tokens:?}");
    assert!(
        debug.contains("acme") && !debug.contains("tok-shared-secret"),
        "{debug}"
    );
}

#[test]
fn allowlists_take_only_web_urls_and_existing_roots() {
    let urls = |list: &[&str]| list.iter().map(|url| url.to_string()).collect::<Vec<_>>();
    let ok = Allowlists::new().provider_base_urls(&urls(&[
        "https://api.example.com/v1",
        "http://10.0.0.5:8000",
    ]));
    assert_eq!(ok.unwrap().provider_origins.len(), 2);
    for bad in ["file:///etc", "api.example.com", "not a url"] {
        let list = urls(&["https://ok.example", bad]);
        let error = Allowlists::new().mcp_urls(&list).unwrap_err();
        let expected = AccessError::Url {
            list: "mcp_urls",
            index: 1,
        };
        assert_eq!(error.to_string(), expected.to_string(), "{bad}");
    }
    let dsns = urls(&["postgres://db.internal:5432/linch", "not a dsn"]);
    let error = Allowlists::new().postgres_dsns(&dsns).unwrap_err();
    assert!(matches!(
        error,
        AccessError::Url {
            list: "postgres_dsns",
            index: 1
        }
    ));
    let dir = tempfile::tempdir().unwrap();
    let roots = [
        dir.path().join("."),
        PathBuf::from("/nonexistent/linch-p11c"),
    ];
    let error = Allowlists::new().workspace_roots(&roots).unwrap_err();
    assert!(
        matches!(error, AccessError::WorkspaceRoot { .. }),
        "{error}"
    );
    let allow = Allowlists::new().workspace_roots(&roots[..1]).unwrap();
    assert_eq!(allow.workspace_roots, [dir.path().canonicalize().unwrap()]);
}

#[test]
fn an_mcp_entry_never_lets_a_client_choose_what_code_loads() {
    let entry = |name: &str| McpCommand {
        command: "/usr/local/bin/git-mcp".into(),
        args: vec![],
        env: vec!["GIT_TOKEN".into(), name.into()],
    };
    for name in [
        "PATH",
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
        "DYLD_INSERT_LIBRARIES",
        "PYTHONPATH",
        "NODE_OPTIONS",
        // Where programs find their rc and config files.
        "HOME",
        "XDG_CONFIG_HOME",
        // Package managers' settings, such as an index or a registry.
        "npm_config_registry",
        "NPM_CONFIG_USERCONFIG",
        "Npm_Config_Node_Options",
        "PIP_INDEX_URL",
        "UV_INDEX_URL",
        // Load paths and options of other runtimes.
        "PYTHONUSERBASE",
        "PERLLIB",
        "_JAVA_OPTIONS",
        "JDK_JAVA_OPTIONS",
        "GCONV_PATH",
        "GEM_HOME",
        "GEM_PATH",
    ] {
        let error = Allowlists::new()
            .mcp_commands(&[entry("LDAP_URL"), entry(name)])
            .unwrap_err();
        assert!(
            matches!(error, AccessError::McpEnv { index: 1, .. }),
            "{name}: {error}"
        );
        assert!(error.to_string().contains(name), "{error}");
    }
    // Names that only start like a refused one stay allowed.
    for name in [
        "HOMEBREW_PREFIX",
        "PIPELINE_ID",
        "UVICORN_PORT",
        "GEM_TOKEN",
    ] {
        let listed = Allowlists::new().mcp_commands(&[entry(name)]);
        assert!(listed.is_ok(), "{name}");
    }
    let allow = Allowlists::new()
        .mcp_commands(&[entry("LDAP_URL")])
        .unwrap();
    assert_eq!(allow.mcp_commands, [entry("LDAP_URL")]);
}
