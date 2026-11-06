//! W5 live bench (BLUEPRINT 13.6): the W5 live prompts through the Rust
//! agent on DeepSeek (`deepseek-flash`), printed as one JSON object.
//!
//! Each prompt needs one `get_weather` call, so a run is two provider turns.
//! Per turn it reports wall time, CPU time, tokens, provider time (from the
//! `stream()` call to the stream's last item) and kernel time (wall minus
//! provider: the loop, the stores, the tool), and kernel time as a share of
//! provider time.
//!
//! CPU time is the sum of the on-CPU nanoseconds in `/proc/self/task/*/
//! schedstat` (Linux), so it covers the HTTP and TLS stack too, and misses
//! threads that exited during the run. Pass `--thinking` to turn thinking
//! on (effort low); it is off by default.
//!
//! Run from `linch-rust/` with the key loaded from the repo's `.env`:
//! `(set -a; . ../.env; set +a; cargo run --release -p linch-providers --example e2e_bench)`

#[path = "../tests/support/live.rs"]
mod support;

use std::time::{Duration, Instant};

use futures::StreamExt;
use linch_core::{Event, RunOptions, Usage};
use serde_json::{Value, json};

/// The W5 live prompts.
const PROMPTS: [&str; 3] = [
    "What is the weather in Paris right now?",
    "What is the weather in Tokyo right now?",
    "What is the weather in Lima right now?",
];

/// On-CPU time of every live thread of this process.
fn cpu_time() -> Duration {
    let Ok(tasks) = std::fs::read_dir("/proc/self/task") else {
        return Duration::ZERO;
    };
    let nanos: u64 = tasks
        .filter_map(Result::ok)
        .filter_map(|task| std::fs::read_to_string(task.path().join("schedstat")).ok())
        .filter_map(|stat| stat.split_whitespace().next()?.parse::<u64>().ok())
        .sum();
    Duration::from_nanos(nanos)
}

fn ms(duration: Duration) -> f64 {
    duration.as_secs_f64() * 1000.0
}

fn tokens(usage: &Usage) -> Value {
    json!({
        "input": usage.input_tokens,
        "output": usage.output_tokens,
        "cache_read": usage.cache_read_tokens,
    })
}

#[tokio::main(flavor = "current_thread")]
async fn main() {
    let thinking = std::env::args().any(|arg| arg == "--thinking");
    let Some(live) = support::live(thinking) else {
        eprintln!("e2e_bench: DEEPSEEK_API_KEY is not set");
        std::process::exit(2);
    };
    let (mut wall, mut cpu, mut provider) = (Duration::ZERO, Duration::ZERO, Duration::ZERO);
    let (mut turns, mut total) = (0usize, Usage::default());
    let mut runs = Vec::new();
    for (index, prompt) in PROMPTS.into_iter().enumerate() {
        let first_call = live.calls.lock().unwrap().len();
        let (cpu_before, started) = (cpu_time(), Instant::now());
        let options = RunOptions::new(format!("bench-{index}"));
        let mut stream = live.agent.run(prompt, options);
        let mut result = None;
        while let Some(item) = stream.next().await {
            if let Event::Result(done) = item.expect("the run continues durably").event {
                result = Some(done);
            }
        }
        let (run_wall, run_cpu) = (started.elapsed(), cpu_time().saturating_sub(cpu_before));
        let result = result.expect("the run ends with a result");
        let calls = live.calls.lock().unwrap()[first_call..].to_vec();
        let run_provider: Duration = calls.iter().map(|call| call.total).sum();
        let usage = calls
            .iter()
            .filter_map(|call| call.usage)
            .fold(Usage::default(), |a, b| a + b);
        let per_call: Vec<Value> = calls
            .iter()
            .map(|call| {
                json!({
                    "stop": call.stop,
                    "first_event_ms": call.first_event.map(ms),
                    "total_ms": ms(call.total),
                    "tokens": call.usage.as_ref().map(tokens),
                })
            })
            .collect();
        runs.push(json!({
            "prompt": prompt,
            "result": result.subtype,
            "final_text": result.final_text,
            "turns": calls.len(),
            "wall_ms": ms(run_wall),
            "cpu_ms": ms(run_cpu),
            "provider_ms": ms(run_provider),
            "kernel_ms": ms(run_wall.saturating_sub(run_provider)),
            "tokens": tokens(&usage),
            "calls": per_call,
        }));
        (wall, cpu, provider) = (wall + run_wall, cpu + run_cpu, provider + run_provider);
        turns += calls.len();
        total += usage;
    }
    let kernel = wall.saturating_sub(provider);
    let per_turn = |duration: Duration| ms(duration) / turns.max(1) as f64;
    let report = json!({
        "model": support::MODEL,
        "thinking": thinking,
        "profile": if cfg!(debug_assertions) { "debug" } else { "release" },
        "prompts": PROMPTS,
        "turns": turns,
        "per_turn": {
            "wall_ms": per_turn(wall),
            "cpu_ms": per_turn(cpu),
            "provider_ms": per_turn(provider),
            "kernel_ms": per_turn(kernel),
        },
        "kernel_share_of_provider": kernel.as_secs_f64() / provider.as_secs_f64().max(f64::EPSILON),
        "tokens": tokens(&total),
        "tool_runs": live.weather.runs.load(std::sync::atomic::Ordering::SeqCst),
        "runs": runs,
    });
    println!(
        "{}",
        serde_json::to_string_pretty(&report).expect("JSON encodes")
    );
}
