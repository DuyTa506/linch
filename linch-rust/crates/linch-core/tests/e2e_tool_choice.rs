//! `tool_choice` and the final tool: the choice on the agent and per run,
//! the synthetic final tool a provider without native structured output
//! is offered, its call ending the run with the checked object, retries,
//! and a crash at every write.

mod common;

use std::sync::Arc;

use common::*;
use linch_core::{
    Agent, AgentBuilder, AgentSpec, Hooks, OutputSchema, PermissionMode, Provider,
    ScriptedProvider, ToolScope,
};
use serde_json::json;

fn schema() -> OutputSchema {
    OutputSchema {
        name: "invoice".into(),
        schema: object(json!({
            "type": "object",
            "properties": {"total": {"type": "number"}},
            "required": ["total"],
            "additionalProperties": false,
        })),
        strict: true,
        description: None,
    }
}

fn shaped(retries: u32) -> AgentSpec {
    let mut spec = spec();
    spec.output_schema = Some(schema());
    spec.structured_output_retries = retries;
    spec
}

fn build(spec: AgentSpec, provider: Arc<dyn Provider>) -> Agent {
    let stores = Arc::new(Stores::default());
    AgentBuilder::new(spec)
        .provider(provider)
        .tools(registry(vec![Echo::new(
            "Read",
            ToolScope::Read,
            &Runs::default(),
        )]))
        .permissions(policy(PermissionMode::SkipDangerous))
        .hooks(Arc::new(Hooks::default()))
        .run_store(stores.clone())
        .session_store(stores)
        .namespace(NS)
        .build()
        .unwrap()
}

/// Digests taken before `tool_choice` and the final tool existed: an agent
/// that sets neither, on a provider with native structured output, keeps
/// the contract its runs were created with.
#[test]
fn contracts_without_tool_choice_are_unchanged() {
    let provider = || Arc::new(ScriptedProvider::new([]));
    let plain = build(spec(), provider());
    assert_eq!(
        plain.contract().to_string(),
        "b2853525e856d78c5977856318e7b5563dbc291c1d1e9e8bedf0500577b3011e"
    );
    let shaped = build(shaped(2), provider());
    assert_eq!(
        shaped.contract().to_string(),
        "20375b704ee604b451865db65a907649a24e2cd6c4d0cdacc7e0c408071687c4"
    );
}
