//! Live structured output through DeepSeek's final tool. Skips when
//! `DEEPSEEK_API_KEY` is unset.
//!
//! Run from `linch-rust/` with the key loaded from the repo's `.env`:
//! `(set -a; . ../.env; set +a; cargo test -p linch-providers --test live_deepseek_final_tool -- --nocapture)`

use std::sync::Arc;

use futures::StreamExt;
use linch_core::events::kinds::ResultSubtype;
use linch_core::store::memory::{MemoryRunStore, MemorySessionStore};
use linch_core::{
    AgentBuilder, AgentSpec, Event, OutputSchema, PermissionEngine, PermissionMode,
    PermissionPolicy, RunOptions, Thinking, ToolRegistry,
};
use linch_providers::{DeepSeek, DeepSeekOptions};
use serde_json::json;

const MODEL: &str = "deepseek-v4-flash";

/// With thinking off the final tool is forced; the answer is its call's
/// input, checked against the schema.
#[tokio::test]
async fn structured_output_comes_back_through_the_final_tool() {
    let Some(options) = DeepSeekOptions::from_env() else {
        eprintln!("skipped, DEEPSEEK_API_KEY is not set");
        return;
    };
    let provider = Arc::new(DeepSeek::new(options).expect("HTTP client builds"));
    let schema = json!({
        "type": "object",
        "properties": {
            "city": {"type": "string"},
            "country": {"type": "string"},
            "population_millions": {"type": "number"},
        },
        "required": ["city", "country", "population_millions"],
        "additionalProperties": false,
    });
    let mut spec = AgentSpec::new(MODEL);
    spec.system_prompt = "Answer with the facts asked for.".into();
    spec.thinking = Some(Thinking::Disabled {});
    spec.max_output_tokens = Some(512);
    spec.max_turns = Some(4);
    spec.structured_output_retries = 1;
    spec.output_schema = Some(OutputSchema {
        name: "city_facts".into(),
        schema: schema.as_object().cloned().unwrap(),
        strict: true,
        description: Some("Facts about the city the user asks about.".into()),
    });
    let (runs, sessions) = (MemoryRunStore::default(), MemorySessionStore::default());
    let agent = AgentBuilder::new(spec)
        .provider(provider)
        .tools(Arc::new(ToolRegistry::new()))
        .permissions(
            PermissionEngine::new(PermissionPolicy::new(PermissionMode::SkipDangerous, "/"))
                .unwrap(),
        )
        .run_store(Arc::new(runs))
        .session_store(Arc::new(sessions))
        .namespace("live")
        .build()
        .expect("the agent builds");
    let mut stream = agent.run(
        "Give the facts for Tokyo, Japan (metro population, in millions).",
        RunOptions::new("live-final-tool"),
    );
    let mut events = Vec::new();
    while let Some(item) = stream.next().await {
        events.push(item.expect("the run does not error").event);
    }
    let final_calls = (events.iter())
        .filter(|event| {
            matches!(event, Event::Assistant(assistant)
            if assistant.message.tool_uses().any(|call| call.name == "city_facts"))
        })
        .count();
    for event in &events {
        if let Event::Error(error) = event {
            eprintln!("error event: {:?}", error.error);
        }
    }
    let Some(Event::Result(result)) = events.last() else {
        panic!("the run ends with a result");
    };
    eprintln!(
        "subtype={:?} final_calls={final_calls} structured_output={:?} error={:?}",
        result.subtype, result.structured_output, result.structured_error
    );
    assert_eq!(result.subtype, ResultSubtype::Success);
    assert!(final_calls >= 1, "the answer came through the final tool");
    let value = result
        .structured_output
        .as_ref()
        .expect("a structured answer");
    assert!(
        value["city"]
            .as_str()
            .is_some_and(|city| city.contains("Tokyo"))
    );
    assert!(value["population_millions"].is_number());
}
