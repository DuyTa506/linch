//! A cache salt is runtime-only: it is not part of the config, the stored
//! definition or the run contract, so a run stored before the option existed
//! (or by a tenant without it) resumes under an agent that has it, and the
//! other way round, without a contract mismatch.

use linch::AgentConfig;
use linch::providers::CacheSalt;
use serde_json::json;

fn config() -> AgentConfig {
    serde_json::from_value(json!({
        "namespace": "acme",
        "spec": {"model": "m"},
        "provider": {"id": "vllm", "base_url": "http://127.0.0.1:9/v1"},
    }))
    .unwrap()
}

fn salted(salt: &str) -> AgentConfig {
    let mut config = config();
    let request = config.provider.request_mut().expect("a live provider");
    request.cache_salt = CacheSalt::new(salt);
    config
}

#[tokio::test]
async fn a_salt_changes_neither_the_run_contract_nor_the_stored_definition() {
    let bare = config();
    let with = salted("a-salt-nobody-sees");
    let contract =
        |config: AgentConfig| async move { config.open().await.unwrap().agent().contract() };
    let before = contract(bare.clone()).await;
    assert_eq!(before, contract(with.clone()).await);
    assert_eq!(before, contract(salted("another-salt")).await);

    // Written out, the salted config is the bare one: nothing of it is stored.
    let written = serde_json::to_string(&with).unwrap();
    assert_eq!(written, serde_json::to_string(&bare).unwrap());
    assert!(!written.contains("a-salt-nobody-sees"));
    let with_secrets = with.to_value_with_secrets().unwrap().to_string();
    assert!(!with_secrets.contains("a-salt-nobody-sees"));
    assert!(!format!("{with:?}").contains("a-salt-nobody-sees"));
}
