//! `spec.tool_cache`: an identical call of a named read tool is served from
//! the cache, after permission and the hooks; entries expire, stay in their
//! namespace and are cleared by a write; and a crash at every write of a
//! run that hits the cache runs nothing twice.

mod common;

use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use async_trait::async_trait;
use common::hooks::hook;
use common::*;
use linch_core::hooks::{PostToolUse, PostToolUseResponse, PreToolUse, PreToolUseResponse};
use linch_core::permissions::{Rule, RuleDecision, ToolRule};
use linch_core::provider::scripted::TurnBuilder;
use linch_core::store::faulty::{FaultTiming, Faulty};
use linch_core::{
    Agent, AgentBuilder, AgentSpec, Envelope, Event, Hooks, InMemoryToolCache, JsonObject,
    LoopGuard, PermissionEngine, PermissionLayer, PermissionMode, PermissionPolicy, RunId,
    RunOptions, RunStatus, RunStore, ScriptedTurn, SessionStore, Tool, ToolCache, ToolCacheStore,
    ToolContext, ToolOutput, ToolOutputError, ToolRegistry, ToolResult, ToolScope,
};
use serde_json::json;

/// A response calling `tool` with the same input every time.
fn call(turn: usize, tool: &str) -> ScriptedTurn {
    TurnBuilder::new(MODEL)
        .text("working")
        .tool_call(&format!("call_{turn}"), tool, &json!({"value": "same"}))
        .usage(usage(10, 5))
        .build()
}

fn script(tools: &[&str]) -> Vec<ScriptedTurn> {
    let mut turns: Vec<_> = tools.iter().enumerate().map(|(n, t)| call(n, t)).collect();
    turns.push(text_turn("done"));
    turns
}

/// A spec that caches `tools`, with the loop guard's repeat check off so a
/// run may repeat a call.
fn cached(tools: &[&str]) -> AgentSpec {
    let mut spec = spec();
    spec.tool_cache = Some(ToolCache::new(tools.iter().copied()));
    spec.loop_guard = LoopGuard {
        max_identical_tool_calls: 0,
        ..LoopGuard::default()
    };
    spec
}

struct Setup {
    spec: AgentSpec,
    hooks: Hooks,
    permissions: PermissionEngine,
    namespace: &'static str,
    cache: Option<Arc<dyn ToolCacheStore>>,
}

impl Setup {
    fn new(spec: AgentSpec) -> Self {
        Self {
            spec,
            hooks: Hooks::default(),
            permissions: policy(PermissionMode::SkipDangerous),
            namespace: NS,
            cache: None,
        }
    }

    fn build<S: RunStore + SessionStore + 'static>(
        self,
        turns: Vec<ScriptedTurn>,
        runs: &Runs,
        stores: Arc<S>,
    ) -> Agent {
        let mut builder = AgentBuilder::new(self.spec)
            .provider(Arc::new(Replay::new(turns)))
            .tools(registry(vec![
                Echo::new("Read", ToolScope::Read, runs),
                Echo::new("Write", ToolScope::Write, runs),
            ]))
            .permissions(self.permissions)
            .hooks(Arc::new(self.hooks))
            .run_store(stores.clone())
            .session_store(stores)
            .namespace(self.namespace);
        if let Some(cache) = self.cache {
            builder = builder.tool_cache_store(cache);
        }
        builder.build().unwrap()
    }
}

fn ends(events: &[Envelope]) -> Vec<(String, bool, bool)> {
    (events.iter())
        .filter_map(|envelope| match &envelope.event {
            Event::ToolCallEnd(end) => {
                Some((end.tool_use_id().to_owned(), end.is_error(), end.cache_hit))
            }
            _ => None,
        })
        .collect()
}

fn ran(runs: &Runs, id: &str) -> u32 {
    runs.lock().unwrap().get(id).copied().unwrap_or(0)
}

#[tokio::test]
async fn a_second_identical_call_is_served_from_the_cache() {
    let runs = Runs::default();
    let agent = Setup::new(cached(&["Read"])).build(
        script(&["Read", "Read"]),
        &runs,
        Arc::new(Stores::default()),
    );
    let events = finish(agent.run("go", RunOptions::new("s1"))).await;
    assert_eq!(
        ends(&events),
        [
            ("call_0".to_owned(), false, false),
            ("call_1".to_owned(), false, true)
        ]
    );
    assert_eq!((ran(&runs, "call_0"), ran(&runs, "call_1")), (1, 0));
    let hit = events
        .iter()
        .position(|e| matches!(&e.event, Event::ToolCallEnd(end) if end.cache_hit));
    let wire = events[hit.unwrap()].event.to_value().unwrap();
    assert_eq!(wire["cache_hit"], true);
}

#[tokio::test]
async fn a_call_of_another_input_or_an_unlisted_tool_is_not_served() {
    let runs = Runs::default();
    let agent = Setup::new(cached(&["Read"])).build(
        script(&["Write", "Write"]),
        &runs,
        Arc::new(Stores::default()),
    );
    let events = finish(agent.run("go", RunOptions::new("s1"))).await;
    assert!(ends(&events).iter().all(|(_, _, hit)| !hit));
    assert_eq!((ran(&runs, "call_0"), ran(&runs, "call_1")), (1, 1));
    // A write tool named in the list is still never cached.
    let runs = Runs::default();
    let agent = Setup::new(cached(&["Write"])).build(
        script(&["Write", "Write"]),
        &runs,
        Arc::new(Stores::default()),
    );
    finish(agent.run("go", RunOptions::new("s1"))).await;
    assert_eq!((ran(&runs, "call_0"), ran(&runs, "call_1")), (1, 1));
}

#[tokio::test]
async fn a_hit_goes_through_the_hooks_and_the_result_stored_is_not_the_hooks() {
    let seen = Arc::new(Mutex::new(Vec::new()));
    let hooks = Hooks::default();
    let log = Arc::clone(&seen);
    let post = hook::<PostToolUse>("scrub", move |request| {
        log.lock().unwrap().push(request.tool_use_id.clone());
        Ok(PostToolUseResponse::Mutate {
            content: Some(json!("scrubbed")),
            is_error: None,
        })
    });
    let _ = hooks.post_tool_use.register(post.arc()).unwrap();
    let block = hook::<PreToolUse>("gate", |request| {
        Ok(if request.tool_use_id == "call_2" {
            PreToolUseResponse::Block {
                reason: "no third".into(),
            }
        } else {
            PreToolUseResponse::Continue
        })
    });
    let _ = hooks.pre_tool_use.register(block.arc()).unwrap();
    let mut setup = Setup::new(cached(&["Read"]));
    setup.hooks = hooks;
    let (runs, stores) = (Runs::default(), Arc::new(Stores::default()));
    let agent = setup.build(script(&["Read", "Read", "Read"]), &runs, stores.clone());
    let events = finish(agent.run("go", RunOptions::new("s1"))).await;
    assert_eq!(
        ends(&events),
        [
            ("call_0".to_owned(), false, false),
            ("call_1".to_owned(), false, true),
            ("call_2".to_owned(), true, false)
        ],
        "a blocked call never reaches the cache"
    );
    assert_eq!(
        *seen.lock().unwrap(),
        ["call_0", "call_1"],
        "post_tool_use ran on the hit"
    );
    let session = messages(&*stores, "s1").await;
    let texts: Vec<_> = session.iter().flat_map(result_texts).collect();
    assert_eq!(texts[..2], ["scrubbed", "scrubbed"]);
}

#[tokio::test]
async fn a_hit_never_skips_permission() {
    let shared: Arc<dyn ToolCacheStore> = Arc::new(InMemoryToolCache::new());
    let runs = Runs::default();
    let mut first = Setup::new(cached(&["Read"]));
    first.cache = Some(shared.clone());
    let agent = first.build(script(&["Read"]), &runs, Arc::new(Stores::default()));
    finish(agent.run("go", RunOptions::new("s1"))).await;
    assert_eq!(ran(&runs, "call_0"), 1);

    // The same namespace and cache, with a rule that denies the tool.
    let mut policy = PermissionPolicy::new(PermissionMode::SkipDangerous, "/");
    let deny = Rule::Tool(ToolRule::new("Read", RuleDecision::Deny));
    policy.layers.push(PermissionLayer::new("deny", [deny]));
    let mut second = Setup::new(cached(&["Read"]));
    second.cache = Some(shared);
    second.permissions = PermissionEngine::new(policy).unwrap();
    let runs = Runs::default();
    let agent = second.build(script(&["Read"]), &runs, Arc::new(Stores::default()));
    let events = finish(agent.run("go", RunOptions::new("s2"))).await;
    assert_eq!(ends(&events), [("call_0".to_owned(), true, false)]);
    assert_eq!(ran(&runs, "call_0"), 0);
}

#[tokio::test(start_paused = true)]
async fn an_entry_expires_after_its_ttl() {
    let shared: Arc<dyn ToolCacheStore> = Arc::new(InMemoryToolCache::new());
    let runs = Runs::default();
    let make = |shared: &Arc<dyn ToolCacheStore>| {
        let mut spec = cached(&["Read"]);
        spec.tool_cache.as_mut().unwrap().ttl_ms = 10_000;
        let mut setup = Setup::new(spec);
        setup.cache = Some(shared.clone());
        setup.build(script(&["Read"]), &runs, Arc::new(Stores::default()))
    };
    finish(make(&shared).run("go", RunOptions::new("s1"))).await;
    let events = finish(make(&shared).run("go", RunOptions::new("s2"))).await;
    assert_eq!(ends(&events)[0].2, true, "a live entry is served");
    tokio::time::advance(Duration::from_secs(11)).await;
    let events = finish(make(&shared).run("go", RunOptions::new("s3"))).await;
    assert_eq!(ends(&events)[0].2, false, "an expired entry is not");
}

#[tokio::test]
async fn namespaces_never_share_entries() {
    let shared: Arc<dyn ToolCacheStore> = Arc::new(InMemoryToolCache::new());
    let mut hit = Vec::new();
    for namespace in ["acme", "acme", "other"] {
        let mut setup = Setup::new(cached(&["Read"]));
        setup.cache = Some(shared.clone());
        setup.namespace = namespace;
        let agent = setup.build(
            script(&["Read"]),
            &Runs::default(),
            Arc::new(Stores::default()),
        );
        let events = finish(agent.run("go", RunOptions::new("s1"))).await;
        hit.push(ends(&events)[0].2);
    }
    assert_eq!(hit, [false, true, false]);
}

#[tokio::test]
async fn a_write_tool_clears_the_namespace_when_the_config_says_so() {
    for (invalidate, third_hits) in [(true, false), (false, true)] {
        let mut spec = cached(&["Read"]);
        spec.tool_cache.as_mut().unwrap().invalidate_on_write = invalidate;
        let runs = Runs::default();
        let agent = Setup::new(spec).build(
            script(&["Read", "Write", "Read"]),
            &runs,
            Arc::new(Stores::default()),
        );
        let events = finish(agent.run("go", RunOptions::new("s1"))).await;
        let hits: Vec<_> = ends(&events).into_iter().map(|e| e.2).collect();
        assert_eq!(
            hits,
            [false, false, third_hits],
            "invalidate_on_write {invalidate}"
        );
        assert_eq!(ran(&runs, "call_2"), u32::from(!third_hits));
    }
}

/// A read tool that fails its first call and answers after.
struct Sometimes(AtomicU32, JsonObject);

#[async_trait]
impl Tool for Sometimes {
    fn name(&self) -> &str {
        "Read"
    }
    fn description(&self) -> &str {
        "Fails once."
    }
    fn input_schema(&self) -> &JsonObject {
        &self.1
    }
    fn scope(&self) -> ToolScope {
        ToolScope::Read
    }
    async fn execute(&self, _input: JsonObject, _ctx: ToolContext) -> ToolResult {
        match self.0.fetch_add(1, Ordering::SeqCst) {
            0 => Err(ToolOutputError::new("not yet")),
            n => Ok(ToolOutput::new(format!("ok {n}"))),
        }
    }
}

#[tokio::test]
async fn an_error_is_never_cached() {
    let registry = ToolRegistry::new();
    let schema = json!({"type": "object"}).as_object().unwrap().clone();
    let tool = Arc::new(Sometimes(AtomicU32::new(0), schema));
    let _ = registry.register(tool.clone()).unwrap();
    let stores = Arc::new(Stores::default());
    let agent = AgentBuilder::new(cached(&["Read"]))
        .provider(Arc::new(Replay::new(script(&["Read", "Read", "Read"]))))
        .tools(Arc::new(registry))
        .permissions(policy(PermissionMode::SkipDangerous))
        .run_store(stores.clone())
        .session_store(stores)
        .namespace(NS)
        .build()
        .unwrap();
    let events = finish(agent.run("go", RunOptions::new("s1"))).await;
    assert_eq!(
        ends(&events),
        [
            ("call_0".to_owned(), true, false),
            ("call_1".to_owned(), false, false),
            ("call_2".to_owned(), false, true)
        ]
    );
    assert_eq!(tool.0.load(Ordering::SeqCst), 2);
}

#[tokio::test]
async fn a_result_over_the_size_limit_is_not_stored() {
    let mut spec = cached(&["Read"]);
    spec.tool_cache.as_mut().unwrap().max_value_bytes = 3;
    let runs = Runs::default();
    let agent = Setup::new(spec).build(
        script(&["Read", "Read"]),
        &runs,
        Arc::new(Stores::default()),
    );
    let events = finish(agent.run("go", RunOptions::new("s1"))).await;
    assert!(ends(&events).iter().all(|(_, _, hit)| !hit));
}

/// A crash at any write of a run that fills and then hits the cache, with
/// the cache surviving (as a Postgres one does), runs no tool twice and
/// ends with one durable end per call.
#[tokio::test]
async fn a_crash_at_any_write_of_a_run_that_hits_the_cache_runs_nothing_twice() {
    let turns = || script(&["Read", "Read"]);
    let clean = Arc::new(Stores::default());
    let agent = Setup::new(cached(&["Read"])).build(turns(), &Runs::default(), clean.clone());
    finish(agent.run("go", RunOptions::new("s1"))).await;
    let writes = clean.log.lock().unwrap().len() as u64;
    for n in 1..=writes {
        for timing in [FaultTiming::Before, FaultTiming::After] {
            let context = format!("write {n} failing {timing:?}");
            let shared: Arc<dyn ToolCacheStore> = Arc::new(InMemoryToolCache::new());
            let (stores, runs) = (Arc::new(Stores::default()), Runs::default());
            let faulty = Arc::new(Faulty::new(stores.clone(), n, timing));
            let mut crashed = Setup::new(cached(&["Read"]));
            crashed.cache = Some(shared.clone());
            let stream = crashed
                .build(turns(), &runs, faulty)
                .run("go", RunOptions::new("s1"));
            let run_id = stream.run_id().clone();
            assert!(drain(stream).await.1.is_some(), "{context}");
            let mut resumed = Setup::new(cached(&["Read"]));
            resumed.cache = Some(shared);
            let agent = resumed.build(turns(), &runs, stores.clone());
            let run_id = recover(&agent, &stores, run_id).await;

            let record = stores.runs.load(NS, &run_id).await.unwrap();
            assert_eq!(record.status, RunStatus::Completed, "{context}");
            let events = stores.runs.events(NS, &run_id, 0).await.unwrap();
            let per_call = |id: &str| {
                (events.iter())
                    .filter(
                        |e| matches!(&e.event, Event::ToolCallEnd(end) if end.tool_use_id() == id),
                    )
                    .count()
            };
            assert_eq!(
                (per_call("call_0"), per_call("call_1")),
                (1, 1),
                "{context}"
            );
            let counts = runs.lock().unwrap().clone();
            assert!(counts.values().all(|&n| n <= 1), "{context}: {counts:?}");
        }
    }
}

/// Continue whatever a crash left: resume, or run again if never created.
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
