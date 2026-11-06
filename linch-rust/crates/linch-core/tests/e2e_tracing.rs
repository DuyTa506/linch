//! What the stream tells an observer beyond the conversation: the provider
//! and the model each call used, the time a compaction took, and the code
//! of a failed tool call.

mod common;

use std::sync::Arc;
use std::time::Duration;

use async_trait::async_trait;
use common::*;
use linch_core::events::kinds::{CompactionEvent, InitEvent, ToolCallEndEvent, UsageEvent};
use linch_core::provider::scripted::TurnBuilder;
use linch_core::{
    CompactionSpec, Envelope, Event, JsonObject, PermissionMode, Provider, ProviderError,
    ProviderRequest, ProviderStream, RunOptions, ScriptedProvider, Tool, ToolChoice, ToolContext,
    ToolOutputError, ToolRegistry, ToolResult, ToolScope,
};
use serde_json::json;
use tokio_util::sync::CancellationToken;

fn events<T>(envelopes: &[Envelope], pick: impl Fn(&Event) -> Option<&T>) -> Vec<&T> {
    envelopes.iter().filter_map(|e| pick(&e.event)).collect()
}

fn init(envelopes: &[Envelope]) -> &InitEvent {
    let inits = events(envelopes, |e| match e {
        Event::Init(init) => Some(init),
        _ => None,
    });
    inits[0]
}

#[tokio::test]
async fn init_names_the_provider_of_the_run() {
    let provider = Arc::new(ScriptedProvider::new([text_turn("hi")]));
    let agent = agent(
        provider,
        registry(vec![]),
        policy(PermissionMode::Default),
        Arc::new(Stores::default()),
    );
    let envelopes = finish(agent.run("go", RunOptions::new("s1"))).await;

    assert_eq!(init(&envelopes).provider, "scripted");
}

#[tokio::test]
async fn usage_names_the_model_the_provider_reported() {
    let served = TurnBuilder::new("served-model-2026")
        .text("hi")
        .usage(usage(3, 1))
        .build();
    let provider = Arc::new(ScriptedProvider::new([served]));
    let agent = agent(
        provider,
        registry(vec![]),
        policy(PermissionMode::Default),
        Arc::new(Stores::default()),
    );
    let envelopes = finish(agent.run("go", RunOptions::new("s1"))).await;

    let usages: Vec<&UsageEvent> = events(&envelopes, |e| match e {
        Event::Usage(usage) => Some(usage),
        _ => None,
    });
    assert_eq!(usages.len(), 1);
    assert_eq!(usages[0].actual_model.as_deref(), Some("served-model-2026"));
}

/// Answers a summary request after `delay`, any other request from its
/// script in order.
struct SlowSummaries {
    script: ScriptedProvider,
    delay: Duration,
}

#[async_trait]
impl Provider for SlowSummaries {
    fn id(&self) -> &str {
        "slow"
    }

    async fn stream(
        &self,
        request: ProviderRequest,
        cancel: CancellationToken,
    ) -> Result<ProviderStream, ProviderError> {
        if request.tool_choice == Some(ToolChoice::Disabled {}) {
            tokio::time::sleep(self.delay).await;
        }
        self.script.stream(request, cancel).await
    }
}

#[tokio::test]
async fn a_compaction_reports_how_long_it_took() {
    let delay = Duration::from_millis(40);
    let summary = TurnBuilder::new(MODEL).text("short").build();
    // Turn 2 is the first with an older turn to fold.
    let turns = vec![
        tool_turn(0, 1, "Read"),
        tool_turn(1, 1, "Read"),
        summary,
        text_turn("done"),
    ];
    let provider = Arc::new(SlowSummaries {
        script: ScriptedProvider::new(turns),
        delay,
    });
    let mut spec = spec();
    spec.compaction = Some(CompactionSpec {
        keep_recent_turns: 1,
        ..CompactionSpec::new(1)
    });
    let runs = Runs::default();
    let tools = registry(vec![Echo::new("Read", ToolScope::Read, &runs)]);
    let agent = agent_with(
        spec,
        provider,
        tools,
        policy(PermissionMode::SkipDangerous),
        Arc::new(Stores::default()),
    );
    let envelopes = finish(agent.run("go", RunOptions::new("s1"))).await;

    let compactions: Vec<&CompactionEvent> = events(&envelopes, |e| match e {
        Event::Compaction(compaction) => Some(compaction),
        _ => None,
    });
    assert_eq!(compactions.len(), 1);
    assert!(
        compactions[0].duration_ms >= 40,
        "the summary call alone took 40 ms: {}",
        compactions[0].duration_ms
    );
}

/// Fails every call with a coded error.
struct Coded {
    schema: JsonObject,
}

#[async_trait]
impl Tool for Coded {
    fn name(&self) -> &str {
        "Coded"
    }
    fn description(&self) -> &str {
        "Fails with a code."
    }
    fn input_schema(&self) -> &JsonObject {
        &self.schema
    }
    fn scope(&self) -> ToolScope {
        ToolScope::Read
    }
    async fn execute(&self, _: JsonObject, _: ToolContext) -> ToolResult {
        let mut error = ToolOutputError::new("no such row");
        error.code = Some("not_found".into());
        Err(error)
    }
}

#[tokio::test]
async fn a_failed_tool_call_ends_with_its_error_code() {
    let turns = [
        TurnBuilder::new(MODEL)
            .tool_call("c1", "Coded", &json!({}))
            .tool_call("c2", "Nope", &json!({}))
            .build(),
        text_turn("done"),
    ];
    let tools = ToolRegistry::new();
    let schema = json!({"type": "object"}).as_object().unwrap().clone();
    let _ = tools.register(Arc::new(Coded { schema })).unwrap();
    let agent = agent(
        Arc::new(ScriptedProvider::new(turns)),
        Arc::new(tools),
        policy(PermissionMode::SkipDangerous),
        Arc::new(Stores::default()),
    );
    let envelopes = finish(agent.run("go", RunOptions::new("s1"))).await;

    let ends: Vec<&ToolCallEndEvent> = events(&envelopes, |e| match e {
        Event::ToolCallEnd(end) => Some(end),
        _ => None,
    });
    let code = |id: &str| {
        let end = ends.iter().find(|end| end.tool_use_id() == id).unwrap();
        end.error_code.as_deref()
    };
    assert_eq!(code("c1"), Some("not_found"));
    assert_eq!(code("c2"), None, "an unknown tool's refusal has no code");
}
