//! Foreground subagents: a durable child run with `parent_run_id`, a fresh
//! session, filtered tools and its own system prompt; its events wrapped
//! in the parent's log; its answer as the tool result; no spawning, no
//! asking; and parallel runs only for read-only types.

mod common;

use std::sync::Arc;

use common::subagents::*;
use common::*;
use linch_core::{
    AgentBuilder, ContentBlock, Envelope, Event, JsonObject, PermissionMode, RunId, RunOptions,
    RunStatus, RunStore, SubagentDefinition, ToolRegistry, ToolScope,
};
use serde_json::json;
use tokio::sync::Barrier;

fn tools(runs: &Runs) -> Arc<ToolRegistry> {
    registry(vec![
        Echo::new("Read", ToolScope::Read, runs),
        Echo::new("Write", ToolScope::Write, runs),
    ])
}

fn scout() -> SubagentDefinition {
    let mut scout = SubagentDefinition::new("scout", "Reads around.", "Scout the code.");
    scout.tools = Some(vec!["Read".into()]);
    scout
}

/// Run the parent of `family` to its end in `mode`, with the tools of
/// `tools` and the definitions `scout` and `verification`.
async fn run(
    family: Arc<Family>,
    tools: Arc<ToolRegistry>,
    mode: PermissionMode,
) -> (Vec<Envelope>, Arc<Stores>, RunId) {
    let stores = Arc::new(Stores::default());
    let agent = AgentBuilder::new(spec())
        .provider(family)
        .tools(tools)
        .permissions(policy(mode))
        .run_store(stores.clone())
        .session_store(stores.clone())
        .namespace(NS)
        .subagents(vec![scout()])
        .build()
        .unwrap();
    let stream = agent.run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    (finish(stream).await, stores, run_id)
}

/// The events of each child, unwrapped from the parent's events, by child
/// run id in order of first sight.
fn wrapped(events: &[Envelope]) -> Vec<(RunId, Vec<Event>)> {
    let mut children: Vec<(RunId, Vec<Event>)> = Vec::new();
    for envelope in events {
        if let Event::Subagent(sub) = &envelope.event {
            match children.iter_mut().find(|(id, _)| *id == sub.child_run_id) {
                Some((_, events)) => events.push((*sub.event).clone()),
                None => children.push((sub.child_run_id.clone(), vec![(*sub.event).clone()])),
            }
        }
    }
    children
}

/// As Python's: the parent's `total_usage` and last `usage` cumulative
/// count its own calls only; a child's usage reaches it wrapped.
#[tokio::test]
async fn the_parents_total_usage_counts_its_own_calls_only() {
    let parent = vec![spawn_turn(None, &["look"]), text_turn("done")];
    let child = linch_core::provider::scripted::TurnBuilder::new(MODEL)
        .text("seen")
        .usage(usage(100, 50))
        .build();
    let family = Arc::new(Family::new(parent, vec![child]));
    let runs = Runs::default();
    let (envelopes, _, _) = run(family, tools(&runs), PermissionMode::SkipDangerous).await;

    let Event::Result(result) = &envelopes.last().unwrap().event else {
        panic!("no result")
    };
    let own = (
        result.total_usage.input_tokens,
        result.total_usage.output_tokens,
    );
    assert_eq!(own, (20, 10), "two parent calls of 10 in, 5 out");
    let child_usage: Vec<(u64, u64)> = wrapped(&envelopes)
        .into_iter()
        .flat_map(|(_, events)| events)
        .filter_map(|event| match event {
            Event::Usage(usage) => Some((usage.usage.input_tokens, usage.usage.output_tokens)),
            _ => None,
        })
        .collect();
    assert_eq!(child_usage, [(100, 50)], "the child's, wrapped");
}

#[tokio::test]
async fn a_subagent_is_a_durable_child_run_of_its_parent() {
    let parent = vec![
        spawn_turn(Some("verification"), &["check x"]),
        text_turn("done"),
    ];
    let child = vec![tool_turn(0, 1, "Read"), text_turn("x holds")];
    let family = Arc::new(Family::new(parent, child));
    let runs = Runs::default();
    let (envelopes, stores, run_id) =
        run(family.clone(), tools(&runs), PermissionMode::SkipDangerous).await;

    let log = messages(&*stores, "s1").await;
    assert_eq!(
        result_texts(&log[2]),
        ["x holds"],
        "the answer is the result"
    );
    let child_id = RunId::new(format!("{run_id}:sub_0"));
    let record = stores.runs.load(NS, &child_id).await.unwrap();
    assert_eq!(record.parent_run_id, Some(run_id.clone()));
    assert_eq!(record.session_id.as_str(), child_id.as_str());
    assert_eq!(record.status, RunStatus::Completed);
    let child_log = messages(&*stores, child_id.as_str()).await;
    assert_eq!(child_log[0], linch_core::Message::user_text("check x"));
    assert_eq!(child_log.len(), 4, "prompt, call, result, answer");

    let request = &family.requests_of("check x")[0];
    let names: Vec<&str> = request.tools.iter().map(|t| t.name.as_str()).collect();
    assert_eq!(names, ["Read"], "verification's tools that the parent has");
    assert_eq!(request.system.len(), 2);
    assert_eq!(request.system[0].text, "You are a test agent.");
    assert!(request.system[1].cacheable);
    let body = linch_core::subagents::verification().body;
    assert_eq!(
        request.system[1].text,
        format!("User-provided instructions:\n\n{body}")
    );
    let parent_names: Vec<String> = (family.requests_of("parent")[0].tools.iter())
        .map(|t| t.name.clone())
        .collect();
    assert_eq!(parent_names, ["Read", "Write", "Subagent"]);

    // Every durable event of the child, wrapped, in the parent's log
    // between the call's start and end.
    let durable = stores.runs.events(NS, &run_id, 0).await.unwrap();
    let children = wrapped(&durable);
    assert_eq!(children.len(), 1);
    let (id, events) = &children[0];
    assert_eq!(*id, child_id);
    let own: Vec<String> = (stores.runs.events(NS, &child_id, 0).await.unwrap().iter())
        .filter(|e| e.event.persistence() == linch_core::Persistence::Both)
        .map(|e| kind(&e.event))
        .collect();
    assert_eq!(events.iter().map(kind).collect::<Vec<_>>(), own);
    let Event::Init(init) = &events[0] else {
        panic!("the child starts with init")
    };
    assert_eq!(init.parent_run_id, Some(run_id.clone()));
    let at = |name: &str| durable.iter().position(|e| kind(&e.event) == name).unwrap();
    let first = durable
        .iter()
        .position(|e| kind(&e.event) == "subagent")
        .unwrap();
    let last = durable
        .iter()
        .rposition(|e| kind(&e.event) == "subagent")
        .unwrap();
    assert!(at("tool_call_start") < first && last < at("tool_call_end"));
    let seqs: Vec<u64> = durable.iter().map(|e| e.seq).collect();
    assert_eq!(seqs, (1..=durable.len() as u64).collect::<Vec<_>>());
    // The caller saw them too, and a child's deltas at once.
    assert!(envelopes.iter().any(|e| matches!(&e.event,
        Event::Subagent(s) if kind(&s.event) == "partial_assistant")));
    assert_eq!(runs.lock().unwrap()["call_0_0"], 1);
}

#[tokio::test]
async fn a_general_purpose_child_has_every_tool_but_subagent_and_cannot_ask() {
    let parent = vec![spawn_turn(None, &["edit y"]), text_turn("done")];
    let child = vec![tool_turn(0, 1, "Write"), text_turn("could not edit")];
    let family = Arc::new(Family::new(parent, child));
    let runs = Runs::default();
    // Default mode: the parent's Subagent call (exec) would ask, so allow
    // it by rule; the child's Write asks and is denied instead.
    let mut policy = linch_core::PermissionPolicy::new(PermissionMode::Default, "/");
    let rule = linch_core::permissions::Rule::Tool(linch_core::permissions::ToolRule::new(
        "Subagent",
        linch_core::permissions::RuleDecision::Allow,
    ));
    policy
        .layers
        .push(linch_core::PermissionLayer::new("host", [rule]));
    let stores = Arc::new(Stores::default());
    let agent = AgentBuilder::new(spec())
        .provider(family.clone())
        .tools(tools(&runs))
        .permissions(linch_core::PermissionEngine::new(policy).unwrap())
        .run_store(stores.clone())
        .session_store(stores.clone())
        .namespace(NS)
        .subagents(Vec::new())
        .build()
        .unwrap();
    let stream = agent.run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    finish(stream).await;

    let names: Vec<String> = (family.requests_of("edit y")[0].tools.iter())
        .map(|t| t.name.clone())
        .collect();
    assert_eq!(
        names,
        ["Read", "Write"],
        "no Subagent: a child cannot spawn"
    );
    let child_id = format!("{run_id}:sub_0");
    let child_log = messages(&*stores, &child_id).await;
    let denied = result_texts(&child_log[2]);
    assert!(denied[0].starts_with("Tool call denied: it needs approval, which a subagent"));
    assert!(runs.lock().unwrap().is_empty(), "the write never ran");
    let log = messages(&*stores, "s1").await;
    assert_eq!(result_texts(&log[2]), ["could not edit"]);
    let child_events = stores.runs.events(NS, &RunId::new(child_id), 0).await;
    let suspended = child_events
        .unwrap()
        .iter()
        .any(|e| kind(&e.event) == "suspended");
    assert!(!suspended);
}

#[tokio::test]
async fn an_unknown_type_is_an_error_result_and_starts_no_child() {
    let parent = vec![spawn_turn(Some("nope"), &["x"]), text_turn("done")];
    let family = Arc::new(Family::new(parent, Vec::new()));
    let (_, stores, run_id) = run(
        family,
        tools(&Runs::default()),
        PermissionMode::SkipDangerous,
    )
    .await;
    let log = messages(&*stores, "s1").await;
    let ContentBlock::ToolResult(result) = &log[2].content[0] else {
        panic!("a tool result")
    };
    assert!(result.is_error);
    assert_eq!(
        result_texts(&log[2]),
        [
            "Unknown subagent_type 'nope'. Available: scout, verification. Or omit \
          subagent_type to use the built-in default."
        ]
    );
    let child = RunId::new(format!("{run_id}:sub_0"));
    assert!(stores.runs.load(NS, &child).await.is_err());
}

#[tokio::test]
async fn read_only_subagents_run_in_parallel() {
    let parent = vec![spawn_turn(Some("scout"), &["a", "b"]), text_turn("done")];
    let child = vec![text_turn("seen")];
    let mut family = Family::new(parent, child);
    // Each child's first call waits for the other's: serial runs time out.
    family.meet = Some(Barrier::new(2));
    let family = Arc::new(family);
    let (_, stores, _) = run(
        family,
        tools(&Runs::default()),
        PermissionMode::SkipDangerous,
    )
    .await;
    let log = messages(&*stores, "s1").await;
    assert_eq!(result_texts(&log[2]), ["seen", "seen"]);
}

#[tokio::test]
async fn parallel_children_lose_no_event_while_a_flush_holds_the_journal() {
    let prompts = ["a", "b", "c", "d"];
    let parent = vec![spawn_turn(Some("scout"), &prompts), text_turn("done")];
    let family = Arc::new(Family::new(parent, vec![text_turn("seen")]));
    let stores = Arc::new(Stores::default());
    let slow = Arc::new(Slow(stores.clone()));
    let agent = AgentBuilder::new(spec())
        .provider(family)
        .tools(tools(&Runs::default()))
        .permissions(policy(PermissionMode::SkipDangerous))
        .run_store(slow.clone())
        .session_store(slow)
        .namespace(NS)
        .subagents(vec![scout()])
        .build()
        .unwrap();
    let stream = agent.run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    finish(stream).await;
    let durable = stores.runs.events(NS, &run_id, 0).await.unwrap();
    let children = wrapped(&durable);
    assert_eq!(children.len(), prompts.len());
    for (child_id, events) in children {
        let own = stores.runs.events(NS, &child_id, 0).await.unwrap();
        let own: Vec<String> = (own.iter())
            .filter(|e| e.event.persistence() == linch_core::Persistence::Both)
            .map(|e| kind(&e.event))
            .collect();
        assert_eq!(
            events.iter().map(kind).collect::<Vec<_>>(),
            own,
            "{child_id}"
        );
    }
}

fn input(value: serde_json::Value) -> JsonObject {
    value.as_object().unwrap().clone()
}

#[tokio::test]
async fn only_a_named_type_whose_tools_all_read_may_run_in_parallel() {
    let registry = tools(&Runs::default());
    let mut writer = SubagentDefinition::new("writer", "Writes.", "Write.");
    writer.tools = Some(vec!["Read".into(), "Write".into()]);
    let mut wild = SubagentDefinition::new("wild", "Anything.", "Go.");
    wild.tools = Some(vec!["*".into()]);
    let mut empty = SubagentDefinition::new("empty", "Nothing.", "Think.");
    empty.tools = Some(Vec::new());
    let mut ghost = SubagentDefinition::new("ghost", "Missing.", "Look.");
    ghost.tools = Some(vec!["Glob".into()]);
    let open = SubagentDefinition::new("open", "Inherits.", "Go.");
    let definitions = vec![scout(), writer, wild, empty, ghost, open];
    let stores = Arc::new(Stores::default());
    let agent = AgentBuilder::new(spec())
        .provider(Arc::new(Family::new(Vec::new(), Vec::new())))
        .tools(registry.clone())
        .permissions(policy(PermissionMode::Default))
        .run_store(stores.clone())
        .session_store(stores)
        .namespace(NS)
        .subagents(definitions)
        .build()
        .unwrap();
    let snapshot = registry.snapshot();
    let tool = snapshot.resolve("Subagent").unwrap().tool();
    assert_eq!(tool.scope(), ToolScope::Exec);
    let parallel = |kind: Option<&str>| {
        let mut call = json!({"description": "d", "prompt": "p"});
        if let Some(kind) = kind {
            call["subagent_type"] = json!(kind);
        }
        tool.parallel(&input(call))
    };
    assert!(parallel(Some("scout")) && parallel(Some(" SCOUT ")) && parallel(Some("empty")));
    for kind in [
        None,
        Some(""),
        Some("general-purpose"),
        Some("writer"),
        Some("wild"),
    ] {
        assert!(!parallel(kind), "{kind:?}");
    }
    for kind in [
        Some("ghost"),
        Some("open"),
        Some("nope"),
        Some("verification"),
    ] {
        assert!(
            !parallel(kind),
            "{kind:?}: Glob and Bash are not registered"
        );
    }
    assert!(
        tool.description()
            .contains("Available subagent types:\n- empty: Nothing.")
    );
    drop(agent);
    assert!(registry.snapshot().resolve("Subagent").is_none());
}

/// R3-core 4: a child run is keyed by its call's id, so a provider that
/// sends one id again in a later response gets a tool error for the later
/// call, never the earlier call's answer.
#[tokio::test]
async fn a_call_id_used_again_in_a_later_turn_is_refused() {
    let call = |prompt: &str| {
        let input = json!({"description": "look", "prompt": prompt});
        linch_core::provider::scripted::TurnBuilder::new(MODEL)
            .tool_call("same", "Subagent", &input)
            .usage(usage(10, 5))
            .build()
    };
    let parent = vec![call("a"), call("b"), text_turn("done")];
    let family = Arc::new(Family::new(parent, vec![text_turn("answered")]));
    let runs = Runs::default();
    let (_, stores, run_id) =
        run(family.clone(), tools(&runs), PermissionMode::SkipDangerous).await;

    let log = messages(&*stores, "s1").await;
    assert_eq!(result_texts(&log[2]), ["answered"]);
    let refused = &result_texts(&log[4])[0];
    assert!(refused.contains("'same' was already used"), "{refused}");
    let calls = family.calls.lock().unwrap().clone();
    assert_eq!(calls.get(&("a".into(), 0)), Some(&1));
    assert_eq!(calls.get(&("b".into(), 0)), None, "{calls:?}");
    let parent = stores.runs.load(NS, &run_id).await.unwrap();
    assert_eq!(parent.status, RunStatus::Completed);
}
