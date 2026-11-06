//! Bench side "rs-kernel" of `linch-rust/bench`: Rust direct, through the
//! facade. `bench_driver JOB.json` runs the job's case and prints one JSON
//! result line; `bench/run_all.py` builds the jobs and reads the results.
//!
//! The runtime is multi-threaded, as in `linch-server`, so the rs-client
//! minus rs-kernel difference is the RPC hop. CPU time is the process's
//! CPU-time clock (every thread), peak RSS is `VmHWM` (Linux).
//!
//! Build: `cargo build --release -p linch --example bench_driver`.

use std::sync::Arc;
use std::time::Instant;

use async_trait::async_trait;
use futures::StreamExt;
use linch::events::kinds::ResultSubtype;
use linch::{
    Agent, AgentBuilder, AgentConfig, AgentSpec, Event, Hooks, JsonObject, MemoryRunStore,
    MemorySessionStore, PermissionEngine, PermissionMode, PermissionPolicy, PgPools, RunOptions,
    ScriptedProvider, ScriptedTurn, Tool, ToolContext, ToolOutput, ToolRegistry, ToolResult,
    ToolScope,
};
use serde_json::{Value, json};

type Outcome = Result<Value, String>;

fn main() {
    let Some(path) = std::env::args().nth(1) else {
        eprintln!("usage: bench_driver JOB.json");
        std::process::exit(2);
    };
    let job: Value = match std::fs::read_to_string(&path)
        .map_err(|e| e.to_string())
        .and_then(|text| serde_json::from_str(&text).map_err(|e| e.to_string()))
    {
        Ok(job) => job,
        Err(error) => {
            eprintln!("bench_driver: cannot read {path}: {error}");
            std::process::exit(2);
        }
    };
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()
        .expect("a tokio runtime");
    let outcome = runtime.block_on(dispatch(&job));
    let line = match outcome {
        Ok(value) => value,
        Err(error) => json!({"status": "error", "error": error}),
    };
    println!("{line}");
}

async fn dispatch(job: &Value) -> Outcome {
    match text(job, "case")? {
        "loop" => case_loop(job).await,
        "pg_loop" => case_pg_loop(job).await,
        _ => Ok(json!({"status": "n/a", "reason": "not implemented on this side"})),
    }
}

// ---------------------------------------------------------------- helpers

fn text<'a>(job: &'a Value, key: &str) -> Result<&'a str, String> {
    job[key]
        .as_str()
        .ok_or_else(|| format!("job.{key} is not a string"))
}

fn count(job: &Value, key: &str) -> Result<usize, String> {
    job[key]
        .as_u64()
        .map(|n| n as usize)
        .ok_or_else(|| format!("job.{key} is not a count"))
}

fn err(error: impl std::fmt::Display) -> String {
    error.to_string()
}

/// The fixture's provider turns.
fn turns(job: &Value) -> Result<Vec<Value>, String> {
    let text = std::fs::read_to_string(text(job, "fixture")?).map_err(err)?;
    let fixture: Value = serde_json::from_str(&text).map_err(err)?;
    fixture["turns"]
        .as_array()
        .cloned()
        .ok_or_else(|| "fixture has no turns".to_owned())
}

fn repeat(turns: &[Value], times: usize) -> Vec<Value> {
    let mut out = Vec::with_capacity(turns.len() * times);
    for _ in 0..times {
        out.extend_from_slice(turns);
    }
    out
}

/// CPU time this process used so far, every thread, in ms.
fn cpu_ms() -> f64 {
    let time = rustix::time::clock_gettime(rustix::time::ClockId::ProcessCPUTime);
    time.tv_sec as f64 * 1000.0 + time.tv_nsec as f64 / 1e6
}

/// Peak resident set size in MiB, from `/proc/self/status` (Linux).
fn peak_rss_mib() -> f64 {
    let Ok(status) = std::fs::read_to_string("/proc/self/status") else {
        return 0.0;
    };
    status
        .lines()
        .find(|line| line.starts_with("VmHWM:"))
        .and_then(|line| line.split_whitespace().nth(1)?.parse::<f64>().ok())
        .map_or(0.0, |kib| kib / 1024.0)
}

/// Wall time per iteration and CPU time over all timed iterations.
#[derive(Default)]
struct Clock {
    wall_ms: Vec<f64>,
    cpu_ms: f64,
    started: Option<(Instant, f64)>,
}

impl Clock {
    fn start(&mut self) {
        self.started = Some((Instant::now(), cpu_ms()));
    }

    fn stop(&mut self) {
        if let Some((wall, cpu)) = self.started.take() {
            self.wall_ms.push(wall.elapsed().as_secs_f64() * 1000.0);
            self.cpu_ms += cpu_ms() - cpu;
        }
    }

    fn ok(self, ops: f64, op: &str, extra: Value) -> Value {
        let mut out = json!({
            "status": "ok",
            "iterations": self.wall_ms.len(),
            "wall_ms": self.wall_ms,
            "cpu_ms": self.cpu_ms,
            "ops_per_iter": ops,
            "op": op,
            "peak_rss_mb": peak_rss_mib(),
        });
        if let (Some(out), Value::Object(extra)) = (out.as_object_mut(), extra) {
            out.extend(extra);
        }
        out
    }
}

/// The bench's read tool: returns its `value` input.
struct Echo {
    name: String,
    description: String,
    schema: JsonObject,
}

#[async_trait]
impl Tool for Echo {
    fn name(&self) -> &str {
        &self.name
    }
    fn description(&self) -> &str {
        &self.description
    }
    fn input_schema(&self) -> &JsonObject {
        &self.schema
    }
    fn scope(&self) -> ToolScope {
        ToolScope::Read
    }
    async fn execute(&self, input: JsonObject, _ctx: ToolContext) -> ToolResult {
        Ok(ToolOutput::new(input["value"].clone()))
    }
}

/// A registry holding the job's Echo tool.
fn echo_registry(job: &Value) -> Result<Arc<ToolRegistry>, String> {
    let spec = &job["tool"];
    let tools = Arc::new(ToolRegistry::new());
    let echo = Echo {
        name: text(spec, "name")?.to_owned(),
        description: text(spec, "description")?.to_owned(),
        schema: spec["input_schema"]
            .as_object()
            .cloned()
            .unwrap_or_default(),
    };
    let _ = tools.register(Arc::new(echo)).map_err(err)?;
    Ok(tools)
}

fn spec(job: &Value) -> Result<AgentSpec, String> {
    let mut spec = AgentSpec::new(text(job, "model")?);
    spec.system_prompt = text(job, "system")?.to_owned();
    if let Some(n) = job["max_tool_concurrency"].as_u64() {
        spec.max_tool_concurrency = n as u32;
    }
    Ok(spec)
}

/// The `AgentConfig` the rs-client side sends, as JSON.
fn config(job: &Value, provider: Value) -> Result<Value, String> {
    let mut spec = json!({"model": text(job, "model")?, "system_prompt": text(job, "system")?});
    if let Some(n) = job["max_tool_concurrency"].as_u64() {
        spec["max_tool_concurrency"] = n.into();
    }
    let mut config = json!({
        "namespace": "bench",
        "spec": spec,
        "provider": provider,
        "permissions": {"mode": "skip_dangerous", "project_root": "/"},
    });
    if job["stores"] == "postgres" {
        config["stores"] = json!({"type": "postgres", "dsn": text(job, "pg_dsn")?});
    }
    Ok(config)
}

fn scripted(turns: Vec<Value>) -> Value {
    json!({"id": "scripted", "turns": turns})
}

async fn open(config: Value, tools: Arc<ToolRegistry>) -> Result<linch::OpenedAgent, String> {
    let config: AgentConfig = serde_json::from_value(config).map_err(err)?;
    config
        .open_with(tools, Arc::new(Hooks::default()), &PgPools::new())
        .await
        .map_err(err)
}

/// Drive one run to its end; fails unless it ends with `success`.
async fn run_once(agent: &Agent, session: &str, prompt: &str) -> Result<usize, String> {
    let mut stream = agent.run(prompt, RunOptions::new(session));
    let (mut delivered, mut subtype) = (0, None);
    while let Some(item) = stream.next().await {
        let envelope = item.map_err(err)?;
        if let Event::Result(done) = &envelope.event {
            subtype = Some(done.subtype);
        }
        delivered += 1;
    }
    match subtype {
        Some(ResultSubtype::Success) => Ok(delivered),
        other => Err(format!("run ended {other:?}")),
    }
}

/// Warm-up runs, then timed runs, each in a new session.
async fn timed_runs(job: &Value, agent: &Agent, prompt: &str) -> Result<Clock, String> {
    let mut clock = Clock::default();
    for i in 0..count(job, "warmup")? {
        run_once(agent, &format!("w{i}"), prompt).await?;
    }
    for i in 0..count(job, "iterations")? {
        clock.start();
        run_once(agent, &format!("s{i}"), prompt).await?;
        clock.stop();
    }
    Ok(clock)
}

// ------------------------------------------------------------------ cases

/// Case 1, memory stores: built like `AgentConfig::open` builds a scripted
/// agent, with the stores kept so a last, untimed run counts their writes.
async fn case_loop(job: &Value) -> Outcome {
    let turns = turns(job)?;
    let times = count(job, "warmup")? + count(job, "iterations")? + 1;
    let fixture: Vec<ScriptedTurn> =
        serde_json::from_value(Value::Array(repeat(&turns, times))).map_err(err)?;
    let runs = Arc::new(MemoryRunStore::new());
    let sessions = Arc::new(MemorySessionStore::new());
    let policy = PermissionPolicy::new(PermissionMode::SkipDangerous, "/");
    let agent = AgentBuilder::new(spec(job)?)
        .provider(Arc::new(ScriptedProvider::new(fixture)))
        .tools(echo_registry(job)?)
        .permissions(PermissionEngine::new(policy).map_err(err)?)
        .run_store(runs.clone())
        .session_store(sessions.clone())
        .namespace("bench")
        .build()
        .map_err(err)?;
    let clock = timed_runs(job, &agent, "go").await?;
    let (run_before, session_before) = (runs.writes(), sessions.writes());
    run_once(&agent, "counted", "go").await?;
    let (run_after, session_after) = (runs.writes(), sessions.writes());
    let writes = json!({
        "run": {
            "create": run_after.create - run_before.create,
            "claim": run_after.claim - run_before.claim,
            "commit": run_after.commit - run_before.commit,
        },
        "session": {
            "create": session_after.create - session_before.create,
            "append": session_after.append - session_before.append,
        },
    });
    Ok(clock.ok(
        turns.len() as f64,
        "turn",
        json!({"writes_per_run": writes, "runs_total": times}),
    ))
}

/// Case 3: the loop fixtures on Postgres run and session stores, opened
/// from the same config the rs-client side sends.
async fn case_pg_loop(job: &Value) -> Outcome {
    let turns = turns(job)?;
    let times = count(job, "warmup")? + count(job, "iterations")?;
    let config = config(job, scripted(repeat(&turns, times)))?;
    let opened = open(config, echo_registry(job)?).await?;
    let clock = timed_runs(job, opened.agent(), "go").await;
    opened.close().await;
    Ok(clock?.ok(turns.len() as f64, "turn", json!({"runs_total": times})))
}
