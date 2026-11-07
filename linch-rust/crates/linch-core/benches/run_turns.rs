//! W5 kernel-overhead bench (BLUEPRINT 13.6): a whole run of the shared W5
//! fixture (10 tool turns of k Echo calls, then a text turn, card B JSON)
//! on the scripted provider and memory stores.
//!
//! The fixtures under `benches/fixtures/` are byte-identical copies of the
//! ones the Python side (`py_kernel.py`) replays. Criterion reports time per
//! turn (one element = one provider turn); the run does no I/O and runs on a
//! current-thread runtime, so wall time is CPU time here. Store writes per
//! run, and per tool turn, are counted once outside the timing loop and
//! printed with the peak RSS.

use std::sync::Arc;

use async_trait::async_trait;
use criterion::{BatchSize, Criterion, Throughput, criterion_group, criterion_main};
use futures::StreamExt;
use linch_core::{
    Agent, AgentBuilder, AgentSpec, JsonObject, MemoryRunStore, MemorySessionStore,
    PermissionEngine, PermissionMode, PermissionPolicy, RunOptions, ScriptedFixture,
    ScriptedProvider, Tool, ToolContext, ToolOutput, ToolRegistry, ToolResult, ToolScope,
};
use serde_json::json;
use tokio::runtime::Runtime;

const FIXTURES: [(&str, &str); 2] = [
    ("w5_10x1", include_str!("fixtures/w5_10x1.json")),
    ("w5_10x3", include_str!("fixtures/w5_10x3.json")),
];

/// The W5 tool: read scope, returns its `value` input.
struct Echo(JsonObject);

#[async_trait]
impl Tool for Echo {
    fn name(&self) -> &str {
        "Echo"
    }
    fn description(&self) -> &str {
        "Echo the value."
    }
    fn input_schema(&self) -> &JsonObject {
        &self.0
    }
    fn scope(&self) -> ToolScope {
        ToolScope::Read
    }
    async fn execute(&self, input: JsonObject, _ctx: ToolContext) -> ToolResult {
        Ok(ToolOutput::new(input["value"].clone()))
    }
}

struct Run {
    agent: Agent,
    runs: Arc<MemoryRunStore>,
    sessions: Arc<MemorySessionStore>,
}

/// A fresh agent and stores for one run; the registry is shared so schema
/// compilation is not timed.
fn setup(fixture: &ScriptedFixture, tools: &Arc<ToolRegistry>) -> Run {
    let runs = Arc::new(MemoryRunStore::new());
    let sessions = Arc::new(MemorySessionStore::new());
    let provider = ScriptedProvider::from(fixture.clone()).without_recording();
    let policy = PermissionPolicy::new(PermissionMode::SkipDangerous, "/");
    let agent = AgentBuilder::new(AgentSpec::new("scripted-model"))
        .provider(Arc::new(provider))
        .tools(Arc::clone(tools))
        .permissions(PermissionEngine::new(policy).expect("valid policy"))
        .run_store(runs.clone())
        .session_store(sessions.clone())
        .namespace("w5")
        .build()
        .expect("valid agent");
    Run {
        agent,
        runs,
        sessions,
    }
}

/// Drive one run to its end; returns how many envelopes it delivered.
async fn drive(agent: &Agent) -> usize {
    let mut stream = agent.run("go", RunOptions::new("s1"));
    let mut delivered = 0;
    while let Some(item) = stream.next().await {
        item.expect("the W5 run completes");
        delivered += 1;
    }
    delivered
}

/// Peak resident set size in MiB, from `/proc/self/status` (Linux).
fn peak_rss_mib() -> Option<f64> {
    let status = std::fs::read_to_string("/proc/self/status").ok()?;
    let line = status.lines().find(|line| line.starts_with("VmHWM:"))?;
    let kib: f64 = line.split_whitespace().nth(1)?.parse().ok()?;
    Some(kib / 1024.0)
}

fn run_turns(c: &mut Criterion) {
    let runtime = tokio::runtime::Builder::new_current_thread()
        .build()
        .expect("a current-thread runtime");
    let schema = json!({"type": "object", "properties": {"value": {"type": "string"}}});
    let tools = Arc::new(ToolRegistry::new());
    let _ = tools
        .register(Arc::new(Echo(
            schema.as_object().cloned().unwrap_or_default(),
        )))
        .expect("Echo registers");
    let mut group = c.benchmark_group("run_turns");
    for (name, json) in FIXTURES {
        let fixture: ScriptedFixture = serde_json::from_str(json).expect("a card B fixture");
        let turns = fixture.turns.len() as u64;
        report(&runtime, name, &fixture, &tools, turns);
        group.throughput(Throughput::Elements(turns));
        group.bench_function(name, |b| {
            b.iter_batched(
                || setup(&fixture, &tools),
                |run| runtime.block_on(drive(&run.agent)),
                BatchSize::SmallInput,
            );
        });
    }
    group.finish();
    if let Some(rss) = peak_rss_mib() {
        eprintln!("run_turns: peak RSS {rss:.1} MiB");
    }
}

/// Count the store writes of one run and print them per run and per turn.
fn report(
    runtime: &Runtime,
    name: &str,
    fixture: &ScriptedFixture,
    tools: &Arc<ToolRegistry>,
    turns: u64,
) {
    let run = setup(fixture, tools);
    let envelopes = runtime.block_on(drive(&run.agent));
    let (runs, sessions) = (run.runs.writes(), run.sessions.writes());
    let tool_turns = turns - 1;
    let commits_per_tool_turn = (runs.commit - 3) as f64 / tool_turns as f64;
    eprintln!(
        "{name}: {turns} turns, {envelopes} envelopes; run writes create {} claim {} commit {} \
         ({commits_per_tool_turn:.1} commits per tool turn); session writes create {} append {}",
        runs.create, runs.claim, runs.commit, sessions.create, sessions.append,
    );
}

criterion_group!(benches, run_turns);
criterion_main!(benches);
