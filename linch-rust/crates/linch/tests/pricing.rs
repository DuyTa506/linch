//! Per-agent price overrides: `AgentConfig.pricing` adds or replaces the
//! rates of a model for this agent alone.

use futures::StreamExt;
use linch::provider::scripted::TurnBuilder;
use linch::{AgentConfig, Event, RunOptions, Usage};
use serde_json::{Value, json};

fn config(model: &str, turns: Value, pricing: Value) -> AgentConfig {
    let mut value = json!({
        "namespace": "t",
        "spec": {"model": model},
        "provider": {"id": "scripted", "turns": turns},
    });
    if !pricing.is_null() {
        value["pricing"] = pricing;
    }
    serde_json::from_value(value).unwrap()
}

/// The `total_cost_usd` of one run of `config` over one scripted turn.
async fn cost_of(model: &str, pricing: Value, usage: Usage) -> Option<f64> {
    let turns = json!([TurnBuilder::new(model).text("hi").usage(usage).build()]);
    let opened = config(model, turns, pricing).open().await.unwrap();
    let mut stream = opened.agent().run("go", RunOptions::new("s1"));
    let mut last = None;
    while let Some(item) = stream.next().await {
        last = Some(item.unwrap().event);
    }
    match last {
        Some(Event::Result(result)) => result.total_cost_usd,
        other => panic!("last event {other:?}"),
    }
}

fn million(input: u64, cache_read: u64) -> Usage {
    Usage {
        input_tokens: input,
        output_tokens: 1_000_000,
        cache_read_tokens: cache_read,
        cache_creation_tokens: 0,
    }
}

#[tokio::test]
async fn a_model_the_table_lacks_costs_nothing_known_until_the_config_prices_it() {
    let model = "Qwen/Qwen3-32B";
    let usage = million(1_000_000, 0);
    assert_eq!(cost_of(model, Value::Null, usage).await, None);
    let rates = json!({model: {"input": 0.0, "output": 0.0}});
    assert_eq!(cost_of(model, rates, usage).await, Some(0.0));
    let rates = json!({model: {"input": 0.2, "output": 0.6}});
    assert_eq!(cost_of(model, rates, usage).await, Some(0.8));
}

#[tokio::test]
async fn an_override_replaces_the_table_for_its_model_only() {
    let usage = million(1_000_000, 1_000_000);
    let rates = json!({"claude-opus-4-8": {
        "input": 1.0, "output": 2.0, "cache_read": 0.5, "cache_creation": 3.0}});
    assert_eq!(
        cost_of("claude-opus-4-8", rates.clone(), usage).await,
        Some(3.5)
    );
    // Another model keeps the published rates.
    let published = linch_providers::cost("claude-haiku-4-5", &usage);
    assert_eq!(cost_of("claude-haiku-4-5", rates, usage).await, published);
}

/// DeepSeek has no built-in rates: its price changes with the hour (peak
/// and off-peak differ by 2x), so no single row is right. An agent states
/// the rates it pays, and a cache hit is priced apart from a miss.
#[tokio::test]
async fn deepseek_costs_nothing_known_until_the_config_states_its_rates() {
    // The peak rates of https://api-docs.deepseek.com/quick_start/pricing,
    // read 2026-11-17, USD per million tokens.
    let usage = million(1_000_000, 1_000_000);
    for model in ["deepseek-flash", "deepseek-v4-pro"] {
        assert_eq!(cost_of(model, Value::Null, usage).await, None, "{model}");
    }
    let rates = json!({"deepseek-flash": {"input": 0.3, "output": 1.2, "cache_read": 0.006}});
    let cost = cost_of("deepseek-flash", rates, usage).await.unwrap();
    // 1M missed input + 1M output + 1M cache hits.
    assert!((cost - (0.3 + 1.2 + 0.006)).abs() < 1e-9, "{cost}");
}

/// Cache rates left out are the input rate: a cached token costs what an
/// uncached one does, never less than the config said.
#[tokio::test]
async fn cache_rates_left_out_default_to_the_input_rate() {
    let rates = json!({"local": {"input": 1.0, "output": 0.0}});
    let usage = Usage {
        input_tokens: 1_000_000,
        cache_read_tokens: 1_000_000,
        cache_creation_tokens: 1_000_000,
        output_tokens: 0,
    };
    assert_eq!(cost_of("local", rates, usage).await, Some(3.0));
}

#[test]
fn rates_must_be_finite_and_not_negative_and_the_echo_is_stable() {
    for bad in [
        json!({"m": {"input": -1.0, "output": 1.0}}),
        json!({"m": {"input": 1.0}}),
        json!({"m": {"input": 1.0, "output": 1.0, "extra": 2.0}}),
        json!({"": {"input": 1.0, "output": 1.0}}),
    ] {
        let mut value = json!({"namespace": "t", "spec": {"model": "m"},
            "provider": {"id": "deepseek"}});
        value["pricing"] = bad.clone();
        assert!(
            serde_json::from_value::<AgentConfig>(value).is_err(),
            "{bad}"
        );
    }
    let bare = config("m", json!([]), Value::Null);
    assert!(
        serde_json::to_value(&bare)
            .unwrap()
            .get("pricing")
            .is_none()
    );
    let priced = config("m", json!([]), json!({"m": {"input": 1.0, "output": 2.0}}));
    let echoed = serde_json::to_value(&priced).unwrap();
    let again: AgentConfig = serde_json::from_value(echoed).unwrap();
    assert_eq!(again, priced);
}
