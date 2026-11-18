//! The cache diagnostic (`AgentBuilder::cache_diagnostic`): a stream-only
//! event after each provider call that reports its input tokens, saying why
//! the provider's prompt cache read what it did against the run's previous
//! call. Off, an agent streams, stores and writes what it always did; on,
//! the only difference is those events.

mod common;

use std::sync::Arc;

use common::*;
use linch_core::events::kinds::{CacheDiagnosticEvent, CacheMissReason};
use linch_core::provider::scripted::TurnBuilder;
use linch_core::{
    Agent, AgentBuilder, Envelope, Event, PermissionMode, Persistence, RunId, RunOptions, RunStore,
    ScriptedProvider, ScriptedTurn, ToolScope, Usage,
};
use serde_json::json;

/// A turn that calls `Read` (when `call`) or answers, and reports `cached`
/// tokens read from the cache and `fresh` that were not.
fn turn(n: usize, call: bool, cached: u64, fresh: u64) -> ScriptedTurn {
    let mut builder = TurnBuilder::new(MODEL).text("working");
    if call {
        builder = builder.tool_call(&format!("call_{n}"), "Read", &json!({"value": "v"}));
    }
    builder
        .usage(Usage {
            input_tokens: fresh,
            output_tokens: 5,
            cache_read_tokens: cached,
            cache_creation_tokens: 0,
        })
        .build()
}

/// Three calls: the first writes the cache, the second reads nearly all of
/// it, the third reads almost none of what the second left.
fn script() -> Vec<ScriptedTurn> {
    vec![
        turn(0, true, 0, 5000),
        turn(1, true, 4800, 400),
        turn(2, false, 100, 5400),
    ]
}

fn agent_of(stores: Arc<Stores>, diagnostic: bool) -> Agent {
    let tools = registry(vec![Echo::new("Read", ToolScope::Read, &Runs::default())]);
    let mut builder = AgentBuilder::new(spec())
        .provider(Arc::new(ScriptedProvider::new(script())))
        .tools(tools)
        .permissions(policy(PermissionMode::SkipDangerous))
        .run_store(stores.clone())
        .session_store(stores)
        .namespace(NS);
    if diagnostic {
        builder = builder.cache_diagnostic();
    }
    builder.build().unwrap()
}

struct Observed {
    envelopes: Vec<Envelope>,
    stored: Vec<(u64, serde_json::Value)>,
    writes: String,
}

async fn observe(diagnostic: bool) -> Observed {
    let stores = Arc::new(Stores::default());
    let agent = agent_of(stores.clone(), diagnostic);
    let mut options = RunOptions::new("s1");
    options.run_id = Some(RunId::new("r1"));
    let envelopes = finish(agent.run("go", options)).await;
    let stored = stores.runs.events(NS, &RunId::new("r1"), 0).await.unwrap();
    let stored = (stored.iter())
        .map(|envelope| (envelope.seq, envelope.event.to_value().unwrap()))
        .collect();
    let writes = format!("{:?}", stores.log.lock().unwrap());
    Observed {
        envelopes,
        stored,
        writes,
    }
}

fn diagnostics(envelopes: &[Envelope]) -> Vec<CacheDiagnosticEvent> {
    (envelopes.iter())
        .filter_map(|envelope| match &envelope.event {
            Event::CacheDiagnostic(event) => Some(event.clone()),
            _ => None,
        })
        .collect()
}

#[tokio::test]
async fn each_call_after_the_first_says_why_the_cache_read_what_it_did() {
    let run = observe(true).await;
    let found = diagnostics(&run.envelopes);
    let summary: Vec<_> = (found.iter())
        .map(|e| (e.reason, e.previous_cached, e.cached, e.prompt_tokens))
        .collect();
    assert_eq!(
        summary,
        [
            (CacheMissReason::NoMiss, 0, 4800, 5200),
            (CacheMissReason::LowRead, 4800, 100, 5500),
        ]
    );
    assert!(found.iter().all(|e| e.first_changed_message.is_none()));
}

#[tokio::test]
async fn the_events_are_stream_only_and_never_stored() {
    let run = observe(true).await;
    let streamed: Vec<_> = (run.envelopes.iter())
        .filter(|envelope| matches!(envelope.event, Event::CacheDiagnostic(_)))
        .collect();
    assert_eq!(streamed.len(), 2);
    for envelope in streamed {
        assert_eq!(envelope.event.persistence(), Persistence::StreamOnly);
        // A stream-only event repeats the last durable seq.
        assert!(run.stored.iter().any(|(seq, _)| *seq == envelope.seq));
    }
    assert!(
        run.stored
            .iter()
            .all(|(_, event)| event["type"] != "cache_diagnostic")
    );
}

#[tokio::test]
async fn off_the_stream_the_log_and_the_writes_are_what_they_always_were() {
    let off = observe(false).await;
    assert!(diagnostics(&off.envelopes).is_empty());

    // Switching it on adds those events to the stream and changes nothing else.
    let on = observe(true).await;
    let strip = |envelopes: &[Envelope]| -> Vec<(u64, serde_json::Value)> {
        (envelopes.iter())
            .filter(|envelope| !matches!(envelope.event, Event::CacheDiagnostic(_)))
            .map(|envelope| (envelope.seq, envelope.event.to_value().unwrap()))
            .collect()
    };
    assert_eq!(strip(&on.envelopes), strip(&off.envelopes));
    assert_eq!(on.stored, off.stored, "the durable log is the same");
    assert_eq!(on.writes, off.writes, "and so are the store writes");
}
