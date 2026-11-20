use std::num::NonZeroU32;
use std::path::PathBuf;
use std::sync::Arc;

use linch::AgentConfig;

use super::*;

fn tenant(name: &str) -> Arc<Tenant> {
    Arc::new(Tenant::new(name, Allowlists::default()).unwrap())
}

/// What hides the operator's policy is the kind of the core error, not the
/// words of its message.
#[test]
fn a_policy_error_is_hidden_whatever_its_wording() {
    use linch::{BuildError, OpenError};

    use crate::error::ErrorObject;

    let required: linch::ReliabilityPolicy = serde_json::from_value(serde_json::json!({
        "prerequisites": [{"tool": "update", "requires": ["lookup"]}],
    }))
    .unwrap();
    let allow = Allowlists::default().required_policy(Some(required));
    let acme = Tenant::new("acme", allow).unwrap();
    let open = |build: BuildError| acme.open_error(ErrorObject::from(&OpenError::Build(build)));

    // A message the old phrases never matched.
    let reworded = open(BuildError::Policy(
        "a rule cites \"update\", unknown here".into(),
    ));
    assert_eq!(
        reworded.message,
        "policy: the policy required for this tenant cannot run on this agent"
    );
    assert_eq!(reworded.data, None);
    // Another kind of invalid agent keeps its message, whatever it says.
    let other = open(BuildError::Invalid(
        "policy: x is not a tool the agent has".into(),
    ));
    assert!(other.message.contains("not a tool the agent has"));
    // Without a required policy the tenant reads its own error.
    let free = Tenant::new("beta", Allowlists::default()).unwrap();
    let error = OpenError::Build(BuildError::Policy("its own rule is wrong".into()));
    let shown = free.open_error(ErrorObject::from(&error));
    assert!(shown.message.contains("its own rule is wrong"));
}

/// A required policy with a defect of its own is the operator's to fix at
/// the start, so a defect found at an open is always the tenant's text.
#[test]
fn a_required_policy_that_is_wrong_by_itself_is_refused_at_the_start() {
    let wrong = |policy: serde_json::Value| {
        let required: linch::ReliabilityPolicy = serde_json::from_value(policy).unwrap();
        Tenant::new(
            "acme",
            Allowlists::default().required_policy(Some(required)),
        )
    };
    for policy in [
        serde_json::json!({"gates": [{"name": "secret-sauce"}]}),
        serde_json::json!({"gates": [{"name": "g", "succeeded": ["a"], "min_calls": 0}]}),
        serde_json::json!({"prerequisites": [{"tool": "a", "requires": ["a"]}]}),
        serde_json::json!({"prerequisites": [
            {"tool": "a", "requires": ["b"]}, {"tool": "b", "requires": ["a"]},
        ]}),
    ] {
        let error = wrong(policy.clone()).err();
        assert!(
            matches!(error, Some(AccessError::RequiredPolicy { .. })),
            "{policy}: {error:?}"
        );
    }
    let sound = serde_json::json!({
        "prerequisites": [{"tool": "update", "requires": ["lookup"]}],
        "gates": [{"name": "looked-up", "succeeded": ["lookup"]}],
    });
    assert!(wrong(sound).is_ok());
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
    assert_eq!(ok.unwrap().provider_endpoints.len(), 2);
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

fn scripted_config(limit: Option<u32>) -> AgentConfig {
    let mut value = serde_json::json!({
        "namespace": "acme",
        "spec": {"model": "m", "max_turns": 8},
        "provider": {"id": "scripted", "turns": []},
    });
    if let Some(limit) = limit {
        value["max_provider_concurrency"] = limit.into();
    }
    serde_json::from_value(value).unwrap()
}

/// A tenant may lower its provider concurrency below the operator's cap,
/// not raise it above; one that asks for none gets the cap; with no cap
/// the config is left as it is.
#[tokio::test]
async fn a_tenant_may_lower_the_provider_concurrency_not_raise_it() {
    let allow = Allowlists::new()
        .scripted_provider(true)
        .max_provider_concurrency(NonZeroU32::new(4));
    let capped = Tenant::new("acme", allow).unwrap();
    for (asked, kept) in [(None, 4), (Some(4), 4), (Some(1), 1)] {
        let mut config = scripted_config(asked);
        capped.admit(&mut config).await.unwrap();
        let admitted = config.max_provider_concurrency.map(NonZeroU32::get);
        assert_eq!(admitted, Some(kept));
    }
    let mut raised = scripted_config(Some(5));
    let refusal = capped.admit(&mut raised).await.unwrap_err();
    assert_eq!(refusal.code, crate::error::code::NOT_ALLOWED);
    let data = serde_json::json!({"kind": "provider_concurrency", "max": 4});
    assert_eq!(refusal.data, Some(data));

    let open = Tenant::new("acme", Allowlists::new().scripted_provider(true)).unwrap();
    for asked in [None, Some(1000)] {
        let mut config = scripted_config(asked);
        open.admit(&mut config).await.unwrap();
        assert_eq!(config.max_provider_concurrency.map(NonZeroU32::get), asked);
    }
}

/// A tenant's run has to end on something the operator can count on: with
/// neither `max_turns` nor a budget cap, a model that keeps calling a tool
/// runs until the loop guard, which an input that keeps changing evades.
#[tokio::test]
async fn a_tenant_agent_with_no_bound_on_its_runs_is_refused() {
    let allow = Allowlists::new()
        .scripted_provider(true)
        .permission_modes(&[
            linch::PermissionMode::Default,
            linch::PermissionMode::SkipDangerous,
        ]);
    let tenant = Tenant::new("acme", allow).unwrap();
    let with = |spec: serde_json::Value, deep: Option<&str>| -> AgentConfig {
        let mut value = serde_json::json!({
            "namespace": "acme",
            "spec": spec,
            "provider": {"id": "scripted", "turns": []},
        });
        if let Some(profile) = deep {
            value["deep_agent"] = serde_json::json!({"profile": profile});
        }
        serde_json::from_value(value).unwrap()
    };
    // The domain profile replaces the spec's `max_turns` when it opens.
    let mut profiled = with(serde_json::json!({"model": "m"}), None);
    profiled.profile =
        serde_json::from_value(serde_json::json!({"id": "triage", "version": "1", "max_turns": 6}))
            .unwrap();
    tenant.admit(&mut profiled).await.unwrap();
    let bounded = [
        (serde_json::json!({"model": "m", "max_turns": 8}), None),
        (
            serde_json::json!({"model": "m", "budget": {"max_tokens": 1000}}),
            None,
        ),
        (
            serde_json::json!({"model": "m", "budget": {"max_cost_usd": 1.0}}),
            None,
        ),
        // The deep agent's balanced profile sets 64 turns when the spec sets none.
        (serde_json::json!({"model": "m"}), Some("balanced")),
    ];
    for (spec, deep) in bounded {
        let mut config = with(spec.clone(), deep);
        tenant
            .admit(&mut config)
            .await
            .unwrap_or_else(|e| panic!("{spec}: {e:?}"));
    }
    let unbounded = [
        (serde_json::json!({"model": "m"}), None),
        // A budget with only a warning ratio caps nothing.
        (
            serde_json::json!({"model": "m", "budget": {"warn_ratio": 0.5}}),
            None,
        ),
        (serde_json::json!({"model": "m"}), Some("unbounded")),
    ];
    for (spec, deep) in unbounded {
        let mut config = with(spec.clone(), deep);
        let refusal = tenant.admit(&mut config).await.unwrap_err();
        assert_eq!(refusal.code, crate::error::code::NOT_ALLOWED, "{spec}");
        assert_eq!(
            refusal.data,
            Some(serde_json::json!({"kind": "run_bound"})),
            "{spec}"
        );
        assert!(refusal.message.contains("max_turns"), "{}", refusal.message);
        assert!(refusal.message.contains("budget"), "{}", refusal.message);
    }
}

/// Admitting a config twice, as a stored background config is admitted
/// again, adds the operator's sections once and keeps the tenant's.
#[tokio::test]
async fn admitting_a_config_again_adds_the_operator_sections_once() {
    use linch::{AgentConfig, SystemSection};

    let policy = SystemSection::new("policy", "Company policy.").order(-1);
    let allow = Allowlists::new()
        .scripted_provider(true)
        .system_sections(std::slice::from_ref(&policy))
        .unwrap();
    let tenant = Tenant::new("acme", allow).unwrap();
    let mut config: AgentConfig = serde_json::from_value(serde_json::json!({
        "namespace": "acme",
        "spec": {"model": "m", "max_turns": 8, "system": {"sections": [
            {"name": "policy", "text": "Mine."}, {"name": "tone", "text": "Brief."}]}},
        "provider": {"id": "scripted", "turns": []},
    }))
    .unwrap();
    tenant.admit(&mut config).await.unwrap();
    let once = config.spec.system.sections.clone();
    assert_eq!(once, [policy, SystemSection::new("tone", "Brief.")]);
    tenant.admit(&mut config).await.unwrap();
    assert_eq!(config.spec.system.sections, once);
}

/// `linch_core::system::layout` sends every section, the operator's and the
/// tenant's, sorted by `order`, each with its `cacheable` flag. The restore
/// after a tenant's `before_provider_call` rewrite puts an operator block
/// back where the request that was sent had it, so a rewrite that changes
/// nothing yields the very same system (a cache hit), and an operator
/// section the operator ordered last (a footer that is not cacheable) stays
/// after the tenant's cacheable blocks.
#[test]
fn a_rewrite_that_changes_nothing_keeps_the_order_of_the_system() {
    let sent = footer_request(&["Tone.", "Operator footer."]);
    let confined = footer_tenant().confine_rewrite(&sent, sent.clone());
    assert_eq!(confined.unwrap(), sent);
}

#[test]
fn a_rewrite_puts_the_operators_blocks_back_where_they_were_sent() {
    use linch::SystemBlock;

    let sent = footer_request(&["Tone.", "Operator footer.", "Child body."]);
    let tenant = footer_tenant();
    let texts = |request: &linch::ProviderRequest| -> Vec<String> {
        (request.system.iter())
            .map(|block| block.text.clone())
            .collect()
    };
    // The rewrite dropped every system block: the footer comes back as it
    // was, not cacheable.
    let mut dropped = sent.clone();
    dropped.system.clear();
    let confined = tenant.confine_rewrite(&sent, dropped).unwrap();
    assert_eq!(confined.system, vec![sent.system[1].clone()]);
    assert!(!confined.system[0].cacheable);
    // The tenant's own blocks change around it.
    let mut edited = sent.clone();
    edited.system = vec![
        SystemBlock {
            text: "New tone.".into(),
            cacheable: true,
        },
        SystemBlock {
            text: "Extra.".into(),
            cacheable: false,
        },
    ];
    let confined = tenant.confine_rewrite(&sent, edited).unwrap();
    assert_eq!(
        texts(&confined),
        ["New tone.", "Operator footer.", "Extra."]
    );
    // A request that carries none of the operator's (nothing to put back)
    // is the callback's own.
    let bare = footer_request(&["Tone."]);
    let mut other = bare.clone();
    other.system[0].text = "Changed.".into();
    let confined = tenant.confine_rewrite(&bare, other.clone()).unwrap();
    assert_eq!(confined, other);
}

fn footer_tenant() -> Tenant {
    let footer = linch::SystemSection::new("footer", "Operator footer.")
        .order(10)
        .cacheable(false);
    let allow = Allowlists::new()
        .system_sections(std::slice::from_ref(&footer))
        .unwrap();
    Tenant::new("acme", allow).unwrap()
}

/// A request whose system is `texts`: the first cacheable (the tenant's
/// section), "Operator footer." not.
fn footer_request(texts: &[&str]) -> linch::ProviderRequest {
    let system: Vec<_> = (texts.iter())
        .enumerate()
        .map(|(at, text)| serde_json::json!({"text": text, "cacheable": at == 0}))
        .collect();
    serde_json::from_value(serde_json::json!({
        "model": "m", "tools": [], "messages": [], "system": system,
    }))
    .unwrap()
}
