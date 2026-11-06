//! Bench side "rs-kernel" of `linch-rust/bench`: Rust direct, through the
//! facade. `bench_driver JOB.json` runs the job's case and prints one JSON
//! result line; `bench/run_all.py` builds the jobs and reads the results.
//!
//! Agents are opened from the same `AgentConfig` JSON the rs-client side
//! sends to `linch-server`, on the runtime the server uses (multi-thread),
//! so rs-client minus rs-kernel is the RPC hop. CPU time is the process's
//! CPU-time clock (every thread), peak RSS is `VmHWM` (Linux).
//!
//! Build: `cargo build --release -p linch --example bench_driver`.

use std::path::Path;
use std::sync::Arc;
use std::time::{Duration, Instant};

use async_trait::async_trait;
use futures::StreamExt;
use linch::events::kinds::ResultSubtype;
use linch::ext::evals::{EvalCase, Scorer, run_eval};
use linch::ext::workflow::{
    JournalStore, MemoryJournalStore, PgJournalStore, WorkflowContext, WorkflowError,
    WorkflowOptions, run_workflow,
};
use linch::{
    Agent, AgentBuilder, AgentConfig, AgentSpec, Event, Hooks, JsonObject, MemoryRunStore,
    MemorySessionStore, PermissionEngine, PermissionMode, PermissionPolicy, PgPools, RunOptions,
    ScriptedProvider, ScriptedTurn, SessionId, Tool, ToolContext, ToolOutput, ToolRegistry,
    ToolResult, ToolScope,
};
use serde_json::{Value, json};
use tokio_util::sync::CancellationToken;

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
        "compaction" => case_compaction(job).await,
        "tools" => case_tools(job).await,
        "mcp" => case_mcp(job).await,
        "decode" => case_decode(job).await,
        "worker" => case_worker(job).await,
        "workflow" => case_workflow(job).await,
        "evals" => case_evals(job).await,
        "startup" => case_startup(job).await,
        "concurrency" => case_concurrency(job).await,
        "live" => case_live(job).await,
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

/// Warm-up plus timed iterations.
fn runs(job: &Value) -> Result<usize, String> {
    Ok(count(job, "warmup")? + count(job, "iterations")?)
}

/// The fixture file: its provider turns and, for evals, its cases.
fn fixture(job: &Value) -> Result<Value, String> {
    let text = std::fs::read_to_string(text(job, "fixture")?).map_err(err)?;
    serde_json::from_str(&text).map_err(err)
}

fn turns(job: &Value) -> Result<Vec<Value>, String> {
    fixture(job)?["turns"]
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

fn ms_since(start: Instant) -> f64 {
    start.elapsed().as_secs_f64() * 1000.0
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
            self.wall_ms.push(ms_since(wall));
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

/// The bench's read tool: Echo returns its `value` input; with a reply
/// template (the live `get_weather`), it answers that with `{city}` filled.
struct BenchTool {
    name: String,
    description: String,
    schema: JsonObject,
    reply: Option<String>,
}

#[async_trait]
impl Tool for BenchTool {
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
        let value = match &self.reply {
            Some(reply) => {
                let city = input.get("city").and_then(Value::as_str).unwrap_or("there");
                Value::String(reply.replace("{city}", city))
            }
            None => input.get("value").cloned().unwrap_or(Value::Null),
        };
        Ok(ToolOutput::new(value))
    }
}

/// A registry holding the job's tool.
fn tool_registry(job: &Value) -> Result<Arc<ToolRegistry>, String> {
    let spec = &job["tool"];
    let tools = Arc::new(ToolRegistry::new());
    let tool = BenchTool {
        name: text(spec, "name")?.to_owned(),
        description: text(spec, "description")?.to_owned(),
        schema: spec["input_schema"]
            .as_object()
            .cloned()
            .unwrap_or_default(),
        reply: job["tool_reply"].as_str().map(str::to_owned),
    };
    let _ = tools.register(Arc::new(tool)).map_err(err)?;
    Ok(tools)
}

fn no_tools() -> Arc<ToolRegistry> {
    Arc::new(ToolRegistry::new())
}

/// The `AgentConfig` the rs-client side sends, as JSON.
fn config(job: &Value, provider: Value) -> Result<Value, String> {
    let mut spec = json!({"model": text(job, "model")?, "system_prompt": text(job, "system")?});
    if let Some(n) = job["max_tool_concurrency"].as_u64() {
        spec["max_tool_concurrency"] = n.into();
    }
    if let Some(extra) = job["spec"].as_object() {
        for (key, value) in extra {
            spec[key] = value.clone();
        }
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

/// The scripted provider replaying `turns` `times` over, in order.
fn scripted(turns: &[Value], times: usize) -> Value {
    json!({"id": "scripted", "turns": repeat(turns, times)})
}

async fn open(config: Value, tools: Arc<ToolRegistry>) -> Result<linch::OpenedAgent, String> {
    let config: AgentConfig = serde_json::from_value(config).map_err(err)?;
    config
        .open_with(tools, Arc::new(Hooks::default()), &PgPools::new())
        .await
        .map_err(err)
}

/// What one run delivered.
#[derive(Default)]
struct Ran {
    compactions: usize,
    turns: usize,
    final_chars: usize,
}

/// Drive one run to its end; fails unless it ends with `success`.
async fn run_once(agent: &Agent, session: &str, prompt: &str) -> Result<Ran, String> {
    let mut stream = agent.run(prompt, RunOptions::new(session));
    let mut ran = Ran::default();
    let (mut subtype, mut tool_error) = (None, None);
    while let Some(item) = stream.next().await {
        match item.map_err(err)?.event {
            Event::Result(done) => {
                ran.final_chars = done.final_text.as_deref().map_or(0, str::len);
                subtype = Some(done.subtype);
            }
            Event::Compaction(_) => ran.compactions += 1,
            Event::Assistant(_) => ran.turns += 1,
            Event::ToolCallEnd(end) if end.result.is_error => {
                tool_error.get_or_insert(format!("{} failed: {:?}", end.tool_name, end.result));
            }
            _ => {}
        }
    }
    if let Some(error) = tool_error {
        return Err(error.chars().take(300).collect());
    }
    match subtype {
        Some(ResultSubtype::Success) => Ok(ran),
        other => Err(format!("run ended {other:?}")),
    }
}

/// Warm-up runs, then timed runs, each in a new session; `before` runs
/// ahead of each, untimed. Returns the clock and the last run.
async fn timed_runs(
    job: &Value,
    agent: &Agent,
    before: &dyn Fn() -> Result<(), String>,
) -> Result<(Clock, Ran), String> {
    let mut clock = Clock::default();
    let mut last = Ran::default();
    for i in 0..runs(job)? {
        before()?;
        let timed = i >= count(job, "warmup")?;
        if timed {
            clock.start();
        }
        last = run_once(agent, &format!("s{i}"), "go").await?;
        if timed {
            clock.stop();
        }
        if job["case"] == "compaction" && last.compactions == 0 {
            return Err("a run did not compact".into());
        }
    }
    Ok((clock, last))
}

fn nothing() -> Result<(), String> {
    Ok(())
}

/// Open `config` with `tools`, run the timed runs, close.
async fn open_and_time(
    job: &Value,
    config: Value,
    tools: Arc<ToolRegistry>,
    before: &dyn Fn() -> Result<(), String>,
) -> Result<(Clock, Ran), String> {
    let opened = open(config, tools).await?;
    let timed = timed_runs(job, opened.agent(), before).await;
    opened.close().await;
    timed
}

/// Put the workspace's `src/` back as `run_all.py` built it.
fn reset_workspace(job: &Value) -> Result<(), String> {
    let src = Path::new(text(job, "workspace")?).join("src");
    std::fs::remove_dir_all(&src).map_err(err)?;
    std::fs::create_dir(&src).map_err(err)?;
    for entry in std::fs::read_dir(text(job, "pristine")?).map_err(err)? {
        let entry = entry.map_err(err)?;
        std::fs::copy(entry.path(), src.join(entry.file_name())).map_err(err)?;
    }
    Ok(())
}

async fn pg_pool(job: &Value) -> Result<linch::postgres::PgPool, String> {
    linch::postgres::connect(text(job, "pg_dsn")?, Duration::from_secs(10))
        .await
        .map_err(err)
}

// ------------------------------------------------------------------ cases

/// Case 1, memory stores: built like `AgentConfig::open` builds a scripted
/// agent, with the stores kept so a last, untimed run counts their writes.
async fn case_loop(job: &Value) -> Outcome {
    let turns = turns(job)?;
    let times = runs(job)? + 1;
    let fixture: Vec<ScriptedTurn> =
        serde_json::from_value(Value::Array(repeat(&turns, times))).map_err(err)?;
    let runs_store = Arc::new(MemoryRunStore::new());
    let sessions = Arc::new(MemorySessionStore::new());
    let mut spec = AgentSpec::new(text(job, "model")?);
    spec.system_prompt = text(job, "system")?.to_owned();
    if let Some(n) = job["max_tool_concurrency"].as_u64() {
        spec.max_tool_concurrency = n as u32;
    }
    let policy = PermissionPolicy::new(PermissionMode::SkipDangerous, "/");
    let agent = AgentBuilder::new(spec)
        .provider(Arc::new(ScriptedProvider::new(fixture)))
        .tools(tool_registry(job)?)
        .permissions(PermissionEngine::new(policy).map_err(err)?)
        .run_store(runs_store.clone())
        .session_store(sessions.clone())
        .namespace("bench")
        .build()
        .map_err(err)?;
    let (clock, _) = timed_runs(job, &agent, &nothing).await?;
    let (run_before, session_before) = (runs_store.writes(), sessions.writes());
    run_once(&agent, "counted", "go").await?;
    let (run_after, session_after) = (runs_store.writes(), sessions.writes());
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
    let extra = json!({"writes_per_run": writes, "runs_total": times});
    Ok(clock.ok(turns.len() as f64, "turn", extra))
}

/// Case 3: the loop fixtures on Postgres run and session stores.
async fn case_pg_loop(job: &Value) -> Outcome {
    let turns = turns(job)?;
    let config = config(job, scripted(&turns, runs(job)?))?;
    let (clock, _) = open_and_time(job, config, tool_registry(job)?, &nothing).await?;
    let extra = json!({"runs_total": runs(job)?});
    Ok(clock.ok(turns.len() as f64, "turn", extra))
}

/// Case 2: a session that crosses the compaction threshold.
async fn case_compaction(job: &Value) -> Outcome {
    let turns = turns(job)?;
    let mut config = config(job, scripted(&turns, runs(job)?))?;
    config["spec"]["compaction"] = json!({
        "threshold_tokens": job["rs_threshold_tokens"],
        "prompt": job["summary_prompt"],
    });
    let (clock, last) = open_and_time(job, config, tool_registry(job)?, &nothing).await?;
    let note = format!("{} compaction(s)/run", last.compactions);
    // The summary turn is not a loop turn.
    Ok(clock.ok((turns.len() - 1) as f64, "turn", json!({"note": note})))
}

/// Case 4: one workspace tool, one call per provider turn.
async fn case_tools(job: &Value) -> Outcome {
    let turns = turns(job)?;
    let mut config = config(job, scripted(&turns, runs(job)?))?;
    config["workspace"] = job["workspace"].clone();
    let reset = || reset_workspace(job);
    let (clock, _) = open_and_time(job, config, no_tools(), &reset).await?;
    Ok(clock.ok(job["calls"].as_f64().unwrap_or(1.0), "call", json!({})))
}

/// Case 5: the `echo` tool of one stdio MCP server, one call per turn.
async fn case_mcp(job: &Value) -> Outcome {
    let turns = turns(job)?;
    let mut config = config(job, scripted(&turns, runs(job)?))?;
    config["mcp_servers"] = json!([{
        "name": "bench",
        "transport": {"type": "stdio", "command": job["mcp_server_bin"], "args": []},
    }]);
    let (clock, _) = open_and_time(job, config, no_tools(), &nothing).await?;
    Ok(clock.ok(job["calls"].as_f64().unwrap_or(1.0), "call", json!({})))
}

/// Case 6: a one-turn run whose provider stream is the vendor's recorded
/// SSE stream, replayed by `sse_server.py` on loopback.
async fn case_decode(job: &Value) -> Outcome {
    let mut provider = json!({"id": text(job, "vendor")?, "base_url": text(job, "base_url")?});
    if job["vendor"] == "openai-responses" {
        provider["store"] = false.into();
    }
    let config = config(job, provider)?;
    let (clock, last) = open_and_time(job, config, no_tools(), &nothing).await?;
    let expected = count(job, "expect_chars")?;
    if last.final_chars != expected {
        let decoded = last.final_chars;
        return Err(format!("decoded {decoded} characters, expected {expected}"));
    }
    let extra = json!({"bytes_per_iter": job["stream_bytes"]});
    Ok(clock.ok(job["stream_events"].as_f64().unwrap_or(1.0), "event", extra))
}

/// Case 7: enqueue N one-turn runs, a worker on the same process drains
/// them; one iteration is enqueue to the last job acked.
async fn case_worker(job: &Value) -> Outcome {
    let jobs = count(job, "jobs")?;
    let pool = pg_pool(job).await?;
    linch::postgres::migrate(&pool).await.map_err(err)?;
    linch_worker::migrate(&pool).await.map_err(err)?;
    let config = config(job, scripted(&turns(job)?, jobs * runs(job)?))?;
    let opened = open(config, no_tools()).await?;
    let shutdown = CancellationToken::new();
    let worker = linch_worker::Worker::new(pool.clone(), linch_worker::WorkerConfig::new("bench"))
        .agent(opened.agent().clone());
    let serving = tokio::spawn(worker.run(shutdown.clone()));
    let mut clock = Clock::default();
    let mut outcome = Ok(());
    for i in 0..runs(job)? {
        let timed = i >= count(job, "warmup")?;
        if timed {
            clock.start();
        }
        outcome = drain(&pool, i, jobs).await;
        if timed {
            clock.stop();
        }
        if outcome.is_err() {
            break;
        }
    }
    shutdown.cancel();
    let served = serving.await.map_err(err)?;
    opened.close().await;
    outcome?;
    served.map_err(err)?;
    let extra = json!({"runs_total": jobs * runs(job)?});
    Ok(clock.ok(jobs as f64, "job", extra))
}

/// Enqueue `jobs` start jobs and wait until the worker acked them all.
async fn drain(
    pool: &linch::postgres::PgPool,
    iteration: usize,
    jobs: usize,
) -> Result<(), String> {
    for j in 0..jobs {
        let session = SessionId::new(format!("i{iteration}-j{j}"));
        linch_worker::enqueue_run(pool, "default", "bench", &session, "go")
            .await
            .map_err(err)?;
    }
    let deadline = Instant::now() + Duration::from_secs(120);
    loop {
        let (left, failed): (i64, i64) = sqlx::query_as(
            "SELECT count(*) FILTER (WHERE failed_at IS NULL), count(*) FILTER (WHERE failed_at IS NOT NULL) \
             FROM linch_worker_jobs WHERE namespace = 'bench'",
        )
        .fetch_one(pool)
        .await
        .map_err(err)?;
        if failed > 0 {
            return Err(format!("{failed} worker jobs failed"));
        }
        if left == 0 {
            return Ok(());
        }
        if Instant::now() > deadline {
            return Err(format!("{left} worker jobs still queued after 120 s"));
        }
        tokio::time::sleep(Duration::from_millis(2)).await;
    }
}

/// Case 8: a workflow of N journaled steps, journal in memory or Postgres.
async fn case_workflow(job: &Value) -> Outcome {
    let steps = count(job, "steps")?;
    let journal: Arc<dyn JournalStore> = if job["stores"] == "postgres" {
        let pool = pg_pool(job).await?;
        linch::ext::migrate(&pool).await.map_err(err)?;
        Arc::new(PgJournalStore::new(pool))
    } else {
        Arc::new(MemoryJournalStore::default())
    };
    let mut clock = Clock::default();
    for i in 0..runs(job)? {
        let options = WorkflowOptions::new(Arc::clone(&journal), "bench", format!("wf-{i}"));
        let timed = i >= count(job, "warmup")?;
        if timed {
            clock.start();
        }
        let total = run_workflow(options, move |wf| journaled_steps(wf, steps))
            .await
            .map_err(err)?;
        if timed {
            clock.stop();
        }
        if total != (0..steps as u64).sum::<u64>() {
            return Err(format!("workflow returned {total}"));
        }
    }
    let extra = json!({"runs_total": runs(job)?});
    Ok(clock.ok(steps as f64, "step", extra))
}

async fn journaled_steps(wf: WorkflowContext, steps: usize) -> Result<u64, WorkflowError> {
    let mut total = 0;
    for i in 0..steps as u64 {
        total += wf
            .step("step", move || async move { Ok::<u64, std::io::Error>(i) })
            .await?;
    }
    Ok(total)
}

/// Case 9: an eval suite of N cases (an Echo call, then the answer)
/// scored by "text contains the expected answer".
async fn case_evals(job: &Value) -> Outcome {
    let fixture = fixture(job)?;
    let cases: Vec<EvalCase> = serde_json::from_value(fixture["cases"].clone()).map_err(err)?;
    let turns = fixture["turns"].as_array().cloned().unwrap_or_default();
    let config = config(job, scripted(&turns, runs(job)?))?;
    let opened = open(config, tool_registry(job)?).await?;
    let scorers = [Scorer::TextContains {
        substring: "{expected}".into(),
    }];
    let mut clock = Clock::default();
    let mut failed = None;
    for i in 0..runs(job)? {
        let timed = i >= count(job, "warmup")?;
        if timed {
            clock.start();
        }
        let result = run_eval(opened.agent(), &cases, &scorers).await;
        if timed {
            clock.stop();
        }
        if result.passed() != result.total() {
            failed = Some(format!(
                "{} of {} cases passed",
                result.passed(),
                result.total()
            ));
            break;
        }
    }
    opened.close().await;
    if let Some(failed) = failed {
        return Err(failed);
    }
    Ok(clock.ok(cases.len() as f64, "case", json!({})))
}

/// Case 10: one cold process: open and a first run (`run_all.py` times the
/// process; Rust has no import phase).
async fn case_startup(job: &Value) -> Outcome {
    let started = Instant::now();
    let config = config(job, scripted(&turns(job)?, 1))?;
    let opened = open(config, tool_registry(job)?).await?;
    let open_ms = ms_since(started);
    let run = run_once(opened.agent(), "s0", "go").await;
    let total_ms = ms_since(started);
    opened.close().await;
    run?;
    Ok(json!({
        "status": "ok",
        "phases_ms": {"open": open_ms, "first_run": total_ms - open_ms},
        "peak_rss_mb": peak_rss_mib(),
    }))
}

/// Case 11: M agents in this process, one session each, run at once.
async fn case_concurrency(job: &Value) -> Outcome {
    let turns = turns(job)?;
    let sessions = count(job, "sessions")?;
    let mut agents = Vec::with_capacity(sessions);
    for _ in 0..sessions {
        let config = config(job, scripted(&turns, runs(job)?))?;
        agents.push(open(config, tool_registry(job)?).await?);
    }
    let mut clock = Clock::default();
    let mut outcome = Ok(());
    for i in 0..runs(job)? {
        let timed = i >= count(job, "warmup")?;
        if timed {
            clock.start();
        }
        let session = format!("s{i}");
        let all = (agents.iter()).map(|opened| run_once(opened.agent(), &session, "go"));
        let done = futures::future::join_all(all).await;
        if timed {
            clock.stop();
        }
        if let Some(Err(error)) = done.into_iter().find(Result::is_err) {
            outcome = Err(error);
            break;
        }
    }
    for opened in agents {
        opened.close().await;
    }
    outcome?;
    Ok(clock.ok(sessions as f64, "run", json!({})))
}

/// Case 13: the W5 live prompts on DeepSeek (the key comes from the
/// environment when the agent opens). Run only with `run_all.py --live`.
async fn case_live(job: &Value) -> Outcome {
    let config = config(job, json!({"id": "deepseek"}))?;
    let opened = open(config, tool_registry(job)?).await?;
    let prompts: Vec<String> = serde_json::from_value(job["prompts"].clone()).map_err(err)?;
    let mut clock = Clock::default();
    let mut turns = 0;
    let mut outcome = Ok(());
    for (i, prompt) in prompts.iter().enumerate() {
        clock.start();
        let ran = run_once(opened.agent(), &format!("live-{i}"), prompt).await;
        clock.stop();
        match ran {
            Ok(ran) => turns += ran.turns,
            Err(error) => {
                outcome = Err(error);
                break;
            }
        }
    }
    opened.close().await;
    outcome?;
    let per_run = turns as f64 / prompts.len().max(1) as f64;
    Ok(clock.ok(per_run, "turn", json!({"turns": turns})))
}
