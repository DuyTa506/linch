//! `max_provider_concurrency`: the cap holds across every agent opened with
//! one `PgPools` on one provider key and endpoint, not across agents opened
//! with their own; it stays off the wire and out of the run contract.

use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::Duration;

use async_trait::async_trait;
use futures::StreamExt;
use linch::provider::scripted::TurnBuilder;
use linch::{
    AgentConfig, CallbackError, Hook, Hooks, PgPools, Point, RunOptions, ToolRegistry,
    hooks::ProviderCallStart,
};
use serde_json::{Value, json};

/// Counts the provider calls in flight while it holds each one for a while;
/// it runs inside the call's place in the gate.
#[derive(Default)]
struct Probe {
    running: AtomicUsize,
    peak: AtomicUsize,
}

#[async_trait]
impl Hook<ProviderCallStart> for Probe {
    fn policy_id(&self) -> &str {
        "probe"
    }
    fn policy_version(&self) -> u32 {
        1
    }
    async fn call(
        &self,
        _request: &<ProviderCallStart as Point>::Request,
    ) -> Result<<ProviderCallStart as Point>::Response, CallbackError> {
        let now = self.running.fetch_add(1, Ordering::SeqCst) + 1;
        self.peak.fetch_max(now, Ordering::SeqCst);
        tokio::time::sleep(Duration::from_millis(80)).await;
        self.running.fetch_sub(1, Ordering::SeqCst);
        Ok(())
    }
}

fn config(namespace: &str, limit: Option<u32>) -> AgentConfig {
    let turns: Vec<Value> = (0..4)
        .map(|_| json!(TurnBuilder::new("m").text("done").build()))
        .collect();
    let mut value = json!({
        "namespace": namespace,
        "spec": {"model": "m"},
        "provider": {"id": "scripted", "turns": turns},
    });
    if let Some(limit) = limit {
        value["max_provider_concurrency"] = json!(limit);
    }
    serde_json::from_value(value).unwrap()
}

/// Open `n` agents (limit 2) and drive 3 runs of each at once; the most
/// calls in flight at any moment.
async fn peak(pools: Option<&PgPools>, n: usize) -> usize {
    let probe = Arc::new(Probe::default());
    let mut agents = Vec::new();
    for i in 0..n {
        let hooks = Hooks::default();
        let _ = hooks.provider_call_start.register(probe.clone()).unwrap();
        let (tools, hooks) = (Arc::new(ToolRegistry::new()), Arc::new(hooks));
        let config = config(&format!("t{i}"), Some(2));
        let opened = match pools {
            Some(pools) => config.open_with(tools, hooks, pools).await,
            None => config.open_with(tools, hooks, &PgPools::new()).await,
        };
        agents.push(opened.unwrap());
    }
    let mut runs = Vec::new();
    for opened in &agents {
        for s in 0..3 {
            let mut stream = opened.agent().run("go", RunOptions::new(format!("s{s}")));
            runs.push(tokio::spawn(async move {
                while let Some(item) = stream.next().await {
                    item.unwrap();
                }
            }));
        }
    }
    for run in runs {
        run.await.unwrap();
    }
    probe.peak.load(Ordering::SeqCst)
}

#[tokio::test]
async fn agents_opened_with_one_pools_share_the_cap() {
    let pools = PgPools::new();
    assert_eq!(
        peak(Some(&pools), 3).await,
        2,
        "9 runs, 3 agents, 2 at once"
    );
}

#[tokio::test]
async fn agents_opened_with_their_own_pools_each_have_the_cap() {
    assert_eq!(peak(None, 3).await, 6, "each agent's own gate lets 2 in");
}

#[tokio::test]
async fn one_agents_runs_share_its_cap() {
    assert_eq!(peak(Some(&PgPools::new()), 1).await, 2);
}

/// Unset stays off the wire; set is written and zero is refused; and the
/// contract is the same set, unset or changed.
#[tokio::test]
async fn the_cap_is_off_the_wire_when_unset_and_out_of_the_contract() {
    let unset = config("t", None);
    assert!(
        serde_json::to_value(&unset)
            .unwrap()
            .get("max_provider_concurrency")
            .is_none()
    );
    let set = config("t", Some(3));
    assert_eq!(
        serde_json::to_value(&set).unwrap()["max_provider_concurrency"],
        3
    );
    let mut zero = serde_json::to_value(&unset).unwrap();
    zero["max_provider_concurrency"] = json!(0);
    assert!(serde_json::from_value::<AgentConfig>(zero).is_err());

    let digest = |config: AgentConfig| async move {
        let opened = config.open().await.unwrap();
        opened.agent().contract()
    };
    let bare = digest(unset).await;
    assert_eq!(bare, digest(set).await);
    assert_eq!(bare, digest(config("t", Some(9))).await);
}
