//! `monitoring.cache_diagnostic`: off the wire and off by default, an unknown
//! switch is refused, and turning it on changes neither the run contract nor
//! the durable events, only adds the stream-only diagnostics.

use futures::StreamExt;
use linch::provider::scripted::TurnBuilder;
use linch::{AgentConfig, Event, RunOptions};
use serde_json::{Value, json};

fn config(monitoring: Option<Value>) -> AgentConfig {
    let usage = |cached: u64, fresh: u64| linch::Usage {
        input_tokens: fresh,
        output_tokens: 5,
        cache_read_tokens: cached,
        cache_creation_tokens: 0,
    };
    let turns = [
        TurnBuilder::new("m")
            .text("one")
            .usage(usage(0, 3000))
            .build(),
        TurnBuilder::new("m")
            .text("two")
            .usage(usage(2900, 200))
            .build(),
    ];
    let mut value = json!({
        "namespace": "t",
        "spec": {"model": "m"},
        "provider": {"id": "scripted", "turns": turns},
    });
    if let Some(monitoring) = monitoring {
        value["monitoring"] = monitoring;
    }
    serde_json::from_value(value).unwrap()
}

#[test]
fn it_is_off_by_default_and_off_the_wire() {
    let unset = config(None);
    assert!(
        serde_json::to_value(&unset)
            .unwrap()
            .get("monitoring")
            .is_none()
    );
    let off = config(Some(json!({"cache_diagnostic": false})));
    assert!(
        serde_json::to_value(&off)
            .unwrap()
            .get("monitoring")
            .is_none()
    );
    let on = config(Some(json!({"cache_diagnostic": true})));
    assert_eq!(
        serde_json::to_value(&on).unwrap()["monitoring"],
        json!({"cache_diagnostic": true})
    );
}

#[test]
fn a_switch_it_does_not_have_is_refused() {
    let mut value = serde_json::to_value(config(None)).unwrap();
    value["monitoring"] = json!({"cache_diagnostics": true});
    assert!(serde_json::from_value::<AgentConfig>(value).is_err());
}

async fn run_of(config: AgentConfig) -> (impl PartialEq + std::fmt::Debug, Vec<Event>) {
    let opened = config.open().await.unwrap();
    let contract = opened.agent().contract();
    let stream = opened.agent().run("go", RunOptions::new("s"));
    let events = stream.map(|item| item.unwrap().event).collect().await;
    (contract, events)
}

#[tokio::test]
async fn turning_it_on_keeps_the_contract_and_the_events_of_a_run() {
    let (bare, off) = run_of(config(None)).await;
    let (watched, on) = run_of(config(Some(json!({"cache_diagnostic": true})))).await;
    assert_eq!(bare, watched, "the run contract is the same");
    // One provider call has no previous call to compare with.
    assert!(on.iter().all(|e| !matches!(e, Event::CacheDiagnostic(_))));
    assert_eq!(on.len(), off.len());
}
