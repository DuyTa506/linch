//! Compaction when the harness's estimate undercounts the provider's real
//! prompt: live runs measured the four-characters-per-token estimate 20% to
//! 2x below the prompt tokens a provider reports, before counting the
//! system prompt and tools the estimate leaves out. The provider here
//! models that: a request really weighs `ratio` times its estimate plus a
//! fixed overhead, every response reports that as its prompt tokens, and a
//! request over the window is refused as longer than it. A long run must
//! still end `success`, and the trigger must follow what the provider
//! reports.

mod common;

use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use common::*;
use futures::StreamExt;
use linch_core::provider::scripted::TurnBuilder;
use linch_core::store::faulty::{FaultTiming, Faulty};
use linch_core::{
    Agent, CompactionSpec, ContentBlock, Envelope, Event, Message, PermissionMode, Provider,
    ProviderError, ProviderRequest, ProviderStream, Role, RunId, RunOptions, RunStatus, RunStore,
    ToolResultPart, ToolScope,
};
use serde_json::json;
use tokio_util::sync::CancellationToken;

/// The model's context window, in real tokens.
const WINDOW: u64 = 8000;

/// How a provider's real prompt relates to the estimate.
#[derive(Clone, Copy)]
struct World {
    /// Real tokens per estimated token: `num / den`.
    num: u64,
    den: u64,
    /// Real tokens of the system prompt and tools, which the estimate omits.
    overhead: u64,
    /// Tool turns of the run, then an answer.
    turns: usize,
    /// Characters of the first tool turn's value, and the growth per turn.
    base: usize,
    grow: usize,
}

/// Twice the estimate and 1500 over, tool results growing with the run.
const UNDERCOUNTED: World = World {
    num: 2,
    den: 1,
    overhead: 1500,
    turns: 30,
    base: 300,
    grow: 40,
};

/// What the provider saw, by request kind.
#[derive(Default)]
struct Seen {
    primary: u32,
    summaries: u32,
    refused_primary: u32,
    refused_summaries: u32,
    biggest: u64,
    /// Messages and real tokens of the last primary request served.
    last: Option<(usize, u64)>,
    /// Primary requests sent at a turn's start whose previous request's
    /// reported prompt plus the estimate of what followed it was already
    /// over the trigger: a compaction that should have come first.
    missed: Vec<String>,
}

struct Counting {
    world: World,
    seen: Mutex<Seen>,
}

/// The harness's estimate of `request`'s messages: four characters per
/// token over text, tool inputs and tool results.
fn estimate(messages: &[Message]) -> u64 {
    let mut chars = 0;
    for block in messages.iter().flat_map(|m| &m.content) {
        match block {
            ContentBlock::Text(text) => chars += text.text.len(),
            ContentBlock::ToolUse(call) => {
                chars += call.name.len() + serde_json::to_string(&call.input).unwrap().len();
            }
            ContentBlock::ToolResult(result) => {
                for part in &result.content {
                    if let ToolResultPart::Text(text) = part {
                        chars += text.text.len();
                    }
                }
            }
            _ => {}
        }
    }
    chars as u64 / 4
}

/// The turn a primary request asks for: 0 for the prompt, `t + 1` after
/// the results of `call_{t}_*`.
fn primary_turn(request: &ProviderRequest) -> usize {
    match &request.messages.last().unwrap().content[0] {
        ContentBlock::ToolResult(result) => {
            result
                .tool_use_id
                .split('_')
                .nth(1)
                .unwrap()
                .parse::<usize>()
                .unwrap()
                + 1
        }
        _ => 0,
    }
}

#[async_trait]
impl Provider for Counting {
    fn id(&self) -> &str {
        "counting"
    }

    fn context_window(&self, _model: &str) -> u64 {
        WINDOW
    }

    async fn stream(
        &self,
        request: ProviderRequest,
        _cancel: CancellationToken,
    ) -> Result<ProviderStream, ProviderError> {
        let World {
            num,
            den,
            overhead,
            turns,
            base,
            grow,
        } = self.world;
        let real = estimate(&request.messages) * num / den + overhead;
        let summarizing = is_summary_call(&request);
        let mut seen = self.seen.lock().unwrap();
        if real > WINDOW {
            match summarizing {
                true => seen.refused_summaries += 1,
                false => seen.refused_primary += 1,
            }
            return Err(ProviderError::ContextLength {
                status: Some(400),
                message: format!("prompt is too long: {real} tokens > {WINDOW} maximum"),
            });
        }
        seen.biggest = seen.biggest.max(real);
        let turn = if summarizing {
            seen.summaries += 1;
            let n = request.messages.len();
            TurnBuilder::new(MODEL)
                .text(format!("summary of {n} messages"))
                .usage(usage(real, 5))
                .build()
        } else {
            seen.primary += 1;
            // The next turn after a response (the view grew since the last
            // request) measures what that request reported plus the estimate
            // from the assistant message on, and is due at 80% of the window
            // less the reserve of a fifth of it.
            if let Some((len, reported)) = seen.last
                && request.messages.len() > len
                && let Some(at) = request
                    .messages
                    .iter()
                    .rposition(|m| m.role == Role::Assistant)
            {
                let measure = reported + estimate(&request.messages[at..]);
                if measure >= WINDOW * 4 / 5 - WINDOW / 5 {
                    seen.missed.push(format!(
                        "turn {} went out at {real} real tokens after a measure of {measure}",
                        primary_turn(&request)
                    ));
                }
            }
            seen.last = Some((request.messages.len(), real));
            match primary_turn(&request) {
                t if t < turns => {
                    let value = json!({"value": format!("v{t}{}", "x".repeat(base + grow * t))});
                    TurnBuilder::new(MODEL)
                        .text("working")
                        .tool_call(&format!("call_{t}_0"), "Read", &value)
                        .usage(usage(real, 5))
                        .build()
                }
                _ => TurnBuilder::new(MODEL)
                    .text("done")
                    .usage(usage(real, 5))
                    .build(),
            }
        };
        let events: Vec<_> = turn.events.into_iter().map(Ok).collect();
        Ok(futures::stream::iter(events).boxed())
    }
}

fn agent_on<S: RunStore + linch_core::SessionStore + 'static>(
    provider: Arc<Counting>,
    compaction: CompactionSpec,
    stores: Arc<S>,
) -> Agent {
    let mut spec = spec();
    spec.compaction = Some(compaction);
    let tools = registry(vec![Echo::new("Read", ToolScope::Read, &Runs::default())]);
    agent_with(
        spec,
        provider,
        tools,
        policy(PermissionMode::SkipDangerous),
        stores,
    )
}

fn provider(world: World) -> Arc<Counting> {
    Arc::new(Counting {
        world,
        seen: Mutex::default(),
    })
}

/// The window-derived trigger, keeping one turn.
fn window_trigger() -> CompactionSpec {
    CompactionSpec {
        keep_recent_turns: 1,
        ..CompactionSpec::default()
    }
}

/// What a run's `compaction` events say, without their wall time.
fn compactions(events: &[Envelope]) -> Vec<(u64, u64, Option<u64>, bool, bool)> {
    (events.iter())
        .filter_map(|e| match &e.event {
            Event::Compaction(c) => Some((
                c.tokens_before,
                c.tokens_after,
                c.measured_tokens,
                c.forced,
                c.rejected.is_some(),
            )),
            _ => None,
        })
        .collect()
}

/// Finish a run a crash left, or start over when it never began.
async fn recover(agent: &Agent, stores: &Stores, run_id: RunId) -> RunId {
    match stores.runs.load(NS, &run_id).await {
        Ok(record) if record.status.is_terminal() => run_id,
        Ok(_) => {
            finish(agent.resume(run_id.clone(), RunOptions::new("s1"))).await;
            run_id
        }
        Err(_) => {
            let stream = agent.run("go", RunOptions::new("s1"));
            let run_id = stream.run_id().clone();
            finish(stream).await;
            run_id
        }
    }
}

async fn run(agent: &Agent, stores: &Stores) -> (RunId, RunStatus, Vec<Envelope>) {
    let stream = agent.run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    let _ = drain(stream).await;
    let status = stores.runs.load(NS, &run_id).await.unwrap().status;
    let events = stores.runs.events(NS, &run_id, 0).await.unwrap();
    (run_id, status, events)
}

/// The run ends `success` on a window the estimate cannot see: compaction
/// triggers on what the provider reported, so no prompt is ever refused and
/// no summary is forced.
#[tokio::test]
async fn a_long_run_survives_an_estimator_that_undercounts() {
    let stores = Arc::new(Stores::default());
    let provider = provider(UNDERCOUNTED);
    let agent = agent_on(provider.clone(), window_trigger(), stores.clone());
    let (_, status, events) = run(&agent, &stores).await;

    let compactions = compactions(&events);
    let forced = compactions.iter().filter(|c| c.3).count();
    let proactive = compactions.len() - forced;
    let seen = provider.seen.lock().unwrap();
    let stats = format!(
        "status {status:?}; primary calls {} ok, {} refused; summary calls {} ok, {} refused; \
         compactions {forced} forced, {proactive} proactive; biggest accepted request {} real \
         tokens of {WINDOW}",
        seen.primary, seen.refused_primary, seen.summaries, seen.refused_summaries, seen.biggest
    );
    eprintln!("{stats}");
    assert_eq!(status, RunStatus::Completed, "{stats}");
    assert_eq!(seen.primary as usize, UNDERCOUNTED.turns + 1, "{stats}");
    assert_eq!(seen.refused_primary + seen.refused_summaries, 0, "{stats}");
    assert_eq!(forced, 0, "{stats}");
    assert!(proactive >= 1, "{stats}");
    assert!(seen.missed.is_empty(), "{:?}", seen.missed);
    // A measure above the estimate rides the event.
    assert!(compactions.iter().any(|c| c.2.is_some()), "{stats}");
}

/// A provider that reports less than the view weighs (a count that leaves
/// cached tokens out, say) never makes a run compact later than the
/// estimate does.
#[tokio::test]
async fn a_provider_that_reports_less_than_the_estimate_delays_nothing() {
    let world = World {
        num: 1,
        den: 4,
        overhead: 0,
        ..UNDERCOUNTED
    };
    let stores = Arc::new(Stores::default());
    let provider = provider(world);
    let agent = agent_on(provider.clone(), window_trigger(), stores.clone());
    let (_, status, events) = run(&agent, &stores).await;
    let compactions = compactions(&events);
    assert_eq!(status, RunStatus::Completed);
    assert!(!compactions.is_empty(), "the estimate alone is due");
    for (before, _, measured, forced, _) in &compactions {
        assert_eq!(*measured, None, "no report above the estimate");
        assert!(!forced);
        // 80% of the window less the reserve of a fifth of it.
        assert!(*before >= WINDOW * 4 / 5 - WINDOW / 5, "{before}");
    }
}

/// A trigger under what the kept turns and the overhead really weigh (here
/// 1200 against a 1500 overhead) leaves the view due after every summary: a
/// run asks twice, then holds, rather than buying a summary every turn.
#[tokio::test]
async fn a_trigger_the_view_cannot_get_under_is_not_bought_every_turn() {
    let world = World {
        turns: 12,
        base: 300,
        grow: 0,
        ..UNDERCOUNTED
    };
    let stores = Arc::new(Stores::default());
    let provider = provider(world);
    let compaction = CompactionSpec {
        keep_recent_turns: 4,
        ..CompactionSpec::new(1200)
    };
    let agent = agent_on(provider.clone(), compaction, stores.clone());
    let (_, status, events) = run(&agent, &stores).await;
    let seen = provider.seen.lock().unwrap();
    assert_eq!(status, RunStatus::Completed);
    assert!(
        seen.summaries <= 4,
        "{} summaries in 12 turns, compactions {:?}",
        seen.summaries,
        compactions(&events)
    );
}

/// The anchor is read back from the durable events: a crash at any write,
/// then a resume, still compacts before any turn whose measure is over the
/// trigger. (Not at the clean run's turns: a tool call a crash interrupted
/// leaves a short placeholder, so the resumed view weighs a little less.)
#[tokio::test]
async fn a_crash_at_any_write_rebuilds_the_anchor() {
    let world = World {
        turns: 14,
        ..UNDERCOUNTED
    };
    for n in 1..=writes_of(world).await {
        for timing in [FaultTiming::Before, FaultTiming::After] {
            let context = format!("write {n} failing {timing:?}");
            let stores = Arc::new(Stores::default());
            let provider = provider(world);
            let faulty = Arc::new(Faulty::new(stores.clone(), n, timing));
            let stream = agent_on(provider.clone(), window_trigger(), faulty)
                .run("go", RunOptions::new("s1"));
            let run_id = stream.run_id().clone();
            assert!(drain(stream).await.1.is_some(), "{context}");
            let resumed = agent_on(provider.clone(), window_trigger(), stores.clone());
            let run_id = recover(&resumed, &stores, run_id).await;
            let record = stores.runs.load(NS, &run_id).await.unwrap();
            assert_eq!(record.status, RunStatus::Completed, "{context}");
            let seen = provider.seen.lock().unwrap();
            assert!(seen.missed.is_empty(), "{context}: {:?}", seen.missed);
            assert_eq!(
                seen.refused_primary + seen.refused_summaries,
                0,
                "{context}"
            );
        }
    }
}

/// Writes of a clean run of `world`.
async fn writes_of(world: World) -> u64 {
    let clean = Arc::new(Stores::default());
    let agent = agent_on(provider(world), window_trigger(), clean.clone());
    let (_, status, _) = run(&agent, &clean).await;
    assert_eq!(status, RunStatus::Completed);
    clean.log.lock().unwrap().len() as u64
}
