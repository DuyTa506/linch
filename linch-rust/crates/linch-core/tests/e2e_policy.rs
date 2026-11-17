//! The reliability policy: a prerequisite refuses a call until the tools
//! it requires succeeded, a completion gate sends a final answer back
//! until the run did what it must, and both survive a crash at every
//! write, because their state is read back from the durable log.

mod common;

use std::sync::Arc;

use async_trait::async_trait;
use common::hooks::hook;
use common::*;
use linch_core::events::kinds::PolicyAction;
use linch_core::hooks::{PreToolUse, PreToolUseResponse, Stop, StopResponse};
use linch_core::provider::scripted::TurnBuilder;
use linch_core::store::faulty::{FaultTiming, Faulty};
use linch_core::{
    Agent, AgentBuilder, AgentSpec, BuildError, ContentBlock, Envelope, Event, Hooks, JsonObject,
    LoopGuard, Message, OutputSchema, PermissionMode, ReliabilityPolicy, Role, RunError, RunId,
    RunOptions, RunStatus, RunStore, ScriptedTurn, SessionStore, Skill, Tool, ToolChoice,
    ToolContext, ToolOutput, ToolOutputError, ToolRegistry, ToolResult, ToolScope,
};
use serde_json::{Value, json};

fn with_policy(policy: Value) -> AgentSpec {
    let mut spec = spec();
    spec.policy = Some(serde_json::from_value(policy).unwrap());
    spec
}

/// `Update` needs `Lookup`.
fn ordered() -> AgentSpec {
    with_policy(json!({"prerequisites": [{"tool": "Update", "requires": ["Lookup"]}]}))
}

/// A final answer needs a successful `Test`.
fn tested() -> AgentSpec {
    with_policy(json!({"gates": [{"name": "tests", "succeeded": ["Test"]}]}))
}

/// `Lookup`: counts its runs, and fails for the input `bad`.
struct Picky {
    runs: Runs,
    schema: JsonObject,
}

#[async_trait]
impl Tool for Picky {
    fn name(&self) -> &str {
        "Lookup"
    }
    fn description(&self) -> &str {
        "Look a value up."
    }
    fn input_schema(&self) -> &JsonObject {
        &self.schema
    }
    fn scope(&self) -> ToolScope {
        ToolScope::Read
    }
    async fn execute(&self, input: JsonObject, ctx: ToolContext) -> ToolResult {
        *(self.runs.lock().unwrap().entry(ctx.tool_use_id.clone())).or_default() += 1;
        match input["value"] == "bad" {
            true => Err(ToolOutputError::new("no such value")),
            false => Ok(ToolOutput::new(input["value"].clone())),
        }
    }
}

fn registry_of(runs: &Runs) -> Arc<ToolRegistry> {
    let schema = json!({"type": "object", "properties": {"value": {"type": "string"}}});
    let lookup = Picky {
        runs: Arc::clone(runs),
        schema: object(schema),
    };
    let registry = registry(vec![
        Echo::new("Update", ToolScope::Write, runs),
        Echo::new("Test", ToolScope::Read, runs),
    ]);
    let _ = registry.register(Arc::new(lookup)).unwrap();
    registry
}

/// A response with one call of `tool` with the input value `value`.
fn call_with(turn: usize, tool: &str, value: &str) -> ScriptedTurn {
    TurnBuilder::new(MODEL)
        .text("working")
        .tool_call(&format!("call_{turn}_0"), tool, &json!({"value": value}))
        .usage(usage(10, 5))
        .build()
}

fn builder<S: RunStore + SessionStore + 'static>(
    spec: AgentSpec,
    provider: Arc<Replay>,
    mode: PermissionMode,
    hooks: Hooks,
    runs: &Runs,
    stores: Arc<S>,
) -> AgentBuilder {
    AgentBuilder::new(spec)
        .provider(provider)
        .tools(registry_of(runs))
        .permissions(policy(mode))
        .hooks(Arc::new(hooks))
        .run_store(stores.clone())
        .session_store(stores)
        .namespace(NS)
}

fn agent_on<S: RunStore + SessionStore + 'static>(
    spec: AgentSpec,
    provider: Arc<Replay>,
    runs: &Runs,
    stores: Arc<S>,
) -> Agent {
    agent_with(spec, provider, Hooks::default(), runs, stores)
}

fn agent_with<S: RunStore + SessionStore + 'static>(
    spec: AgentSpec,
    provider: Arc<Replay>,
    hooks: Hooks,
    runs: &Runs,
    stores: Arc<S>,
) -> Agent {
    let mode = PermissionMode::SkipDangerous;
    builder(spec, provider, mode, hooks, runs, stores)
        .build()
        .unwrap()
}

/// A `stop` hook that rewrites every final answer into `text`.
fn rewriting(text: &'static str) -> Hooks {
    let hooks = Hooks::default();
    let rewrite = hook::<Stop>("rewrite", move |_| {
        Ok(StopResponse::Rewrite { text: text.into() })
    });
    let _ = hooks.stop.register(rewrite.arc()).unwrap();
    hooks
}

/// A response with one call of `tool`, id `call_{turn}_0`.
fn call(turn: usize, tool: &str) -> ScriptedTurn {
    tool_turn(turn, 1, tool)
}

/// A response with calls of each of `tools`, ids `call_{turn}_{i}`.
fn batch(turn: usize, names: &[&str]) -> ScriptedTurn {
    let mut turn_builder = TurnBuilder::new(MODEL).text("working");
    for (at, tool) in names.iter().enumerate() {
        let input = json!({"value": format!("v{turn}.{at}")});
        turn_builder = turn_builder.tool_call(&format!("call_{turn}_{at}"), tool, &input);
    }
    turn_builder.usage(usage(10, 5)).build()
}

/// The wire form of every `policy` event.
fn policies(events: &[Envelope]) -> Vec<Value> {
    (events.iter())
        .filter(|envelope| matches!(envelope.event, Event::Policy(_)))
        .map(|envelope| serde_json::to_value(&envelope.event).unwrap())
        .collect()
}

fn actions(events: &[Envelope], action: PolicyAction) -> usize {
    (events.iter())
        .filter(|envelope| matches!(&envelope.event, Event::Policy(p) if p.action == action))
        .count()
}

fn errors(events: &[Envelope]) -> Vec<String> {
    (events.iter())
        .filter_map(|envelope| match &envelope.event {
            Event::Error(error) => Some(error.error["name"].as_str()?.to_owned()),
            _ => None,
        })
        .collect()
}

async fn ended(stores: &Stores, run_id: &RunId) -> (RunStatus, Vec<Envelope>) {
    let record = stores.runs.load(NS, run_id).await.unwrap();
    let events = stores.runs.events(NS, run_id, 0).await.unwrap();
    (record.status, events)
}

/// A run to its end on fresh stores.
async fn run_to_end(
    spec: AgentSpec,
    turns: Vec<ScriptedTurn>,
) -> (
    Arc<Stores>,
    Arc<Replay>,
    Runs,
    RunId,
    RunStatus,
    Vec<Envelope>,
) {
    let stores = Arc::new(Stores::default());
    let (provider, runs) = (Arc::new(Replay::new(turns)), Runs::default());
    let agent = agent_on(spec, provider.clone(), &runs, stores.clone());
    let stream = agent.run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    finish(stream).await;
    let (status, events) = ended(&stores, &run_id).await;
    (stores, provider, runs, run_id, status, events)
}

fn results_of(message: &Message) -> Vec<(String, String, bool)> {
    (message.content.iter())
        .filter_map(|block| match block {
            ContentBlock::ToolResult(result) => {
                let text = serde_json::to_value(&result.content).unwrap();
                let text = match text {
                    Value::String(text) => text,
                    Value::Array(parts) => (parts.iter())
                        .filter_map(|part| part["text"].as_str())
                        .collect(),
                    other => other.to_string(),
                };
                Some((result.tool_use_id.clone(), text, result.is_error))
            }
            _ => None,
        })
        .collect()
}

/// The session's results by call id.
fn answers(session: &[Message]) -> Vec<(String, String, bool)> {
    session.iter().flat_map(results_of).collect()
}

/// A prerequisite answers the call with an error naming what to run first,
/// the call does not run, and the model recovers by running the
/// prerequisite and calling again.
#[tokio::test]
async fn a_prerequisite_blocks_the_call_and_the_model_recovers() {
    let turns = vec![
        call(0, "Update"),
        call(1, "Lookup"),
        call(2, "Update"),
        text_turn("done"),
    ];
    let (stores, _, runs, _, status, events) = run_to_end(ordered(), turns).await;
    assert_eq!(status, RunStatus::Completed);
    let ran = runs.lock().unwrap().clone();
    assert!(
        !ran.contains_key("call_0_0"),
        "the refused call did not run"
    );
    assert_eq!(ran.get("call_1_0"), Some(&1));
    assert_eq!(ran.get("call_2_0"), Some(&1), "the retried call ran");

    let refused = policies(&events);
    assert_eq!(refused.len(), 1, "{refused:?}");
    assert_eq!(refused[0]["rule"], "prerequisite");
    assert_eq!(refused[0]["action"], "refuse");
    assert_eq!(refused[0]["tool"], "Update");
    assert_eq!(refused[0]["tool_use_id"], "call_0_0");
    assert_eq!(refused[0]["missing"], json!(["Lookup"]));
    assert!(
        refused[0]["detail"].as_str().unwrap().contains("'Lookup'"),
        "{refused:?}"
    );

    let session = messages(&*stores, "s1").await;
    let answered = answers(&session);
    assert_eq!(answered[0].0, "call_0_0");
    assert!(answered[0].2, "the refusal is an error result");
    assert!(answered[0].1.contains("'Lookup' must succeed first"));
    assert!(!answered[1].2 && !answered[2].2);
}

/// Calls of one batch do not unlock each other: a call and the call it
/// needs, asked together, are refused; the prerequisite itself runs.
#[tokio::test]
async fn calls_in_one_batch_do_not_unlock_each_other() {
    let turns = vec![
        batch(0, &["Lookup", "Update"]),
        call(1, "Update"),
        text_turn("done"),
    ];
    let (stores, _, runs, _, status, events) = run_to_end(ordered(), turns).await;
    assert_eq!(status, RunStatus::Completed);
    let ran = runs.lock().unwrap().clone();
    assert_eq!(ran.get("call_0_0"), Some(&1), "the prerequisite ran");
    assert!(!ran.contains_key("call_0_1"), "its dependant did not");
    assert_eq!(ran.get("call_1_0"), Some(&1), "the next batch's call ran");
    assert_eq!(actions(&events, PolicyAction::Refuse), 1);
    let session = messages(&*stores, "s1").await;
    let answered = answers(&session);
    assert_eq!(
        (answered[0].2, answered[1].2),
        (false, true),
        "{answered:?}"
    );
}

/// A prerequisite that ran and failed does not unlock.
#[tokio::test]
async fn a_failed_prerequisite_does_not_unlock() {
    let turns = vec![
        call_with(0, "Lookup", "bad"),
        call(1, "Update"),
        call_with(2, "Lookup", "good"),
        call(3, "Update"),
        text_turn("done"),
    ];
    // Three failed batches in a row would end the run: two are enough.
    let (_, _, runs, _, status, events) = run_to_end(ordered(), turns).await;
    assert_eq!(status, RunStatus::Completed);
    let ran = runs.lock().unwrap().clone();
    assert_eq!(ran.get("call_0_0"), Some(&1), "Lookup ran and failed");
    assert!(!ran.contains_key("call_1_0"), "so Update was refused");
    assert_eq!(ran.get("call_3_0"), Some(&1), "a later Lookup unlocked it");
    assert_eq!(actions(&events, PolicyAction::Refuse), 1);
}

/// A refused call asks nobody: no permission request, no `pre_tool_use`
/// hook, no approval to wait for.
#[tokio::test]
async fn a_refused_call_uses_neither_permission_nor_hooks() {
    let seen = Arc::new(std::sync::Mutex::new(Vec::<String>::new()));
    let log = seen.clone();
    let hooks = Hooks::default();
    let pre = hook::<PreToolUse>("watch", move |request| {
        log.lock().unwrap().push(request.tool_name.clone());
        Ok(PreToolUseResponse::Continue)
    });
    let _ = hooks.pre_tool_use.register(pre.arc()).unwrap();
    let stores = Arc::new(Stores::default());
    let (provider, runs) = (
        Arc::new(Replay::new(vec![call(0, "Update"), text_turn("done")])),
        Runs::default(),
    );
    // `Update` writes, which this mode would suspend to ask about.
    let agent = builder(
        ordered(),
        provider,
        PermissionMode::Default,
        hooks,
        &runs,
        stores.clone(),
    )
    .build()
    .unwrap();
    let stream = agent.run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    finish(stream).await;
    let (status, events) = ended(&stores, &run_id).await;
    assert_eq!(status, RunStatus::Completed);
    assert!(!kinds(&events).contains(&"permission_request".to_owned()));
    assert!(seen.lock().unwrap().is_empty(), "{:?}", seen.lock());
    assert!(runs.lock().unwrap().is_empty());
}

fn invoice() -> OutputSchema {
    OutputSchema {
        name: "invoice".into(),
        schema: object(json!({
            "type": "object",
            "properties": {"total": {"type": "number"}},
            "required": ["total"],
        })),
        strict: true,
        description: None,
    }
}

fn build_with(spec: AgentSpec) -> Result<Agent, BuildError> {
    let provider = Arc::new(Replay::new(vec![]).without_native_output_schema());
    let mode = PermissionMode::SkipDangerous;
    let stores = Arc::new(Stores::default());
    builder(
        spec,
        provider,
        mode,
        Hooks::default(),
        &Runs::default(),
        stores,
    )
    .build()
}

/// A tool choice that holds every turn cannot go with rules it keeps from
/// being met: a held-back tool or a `succeeded` gate never gets called.
/// The final tool is the way out of `required`.
#[test]
fn a_forced_tool_choice_cannot_go_with_a_policy_it_defeats() {
    let named = |name: &str| ToolChoice::Tool { name: name.into() };
    let check_only = json!({"gates": [{"name": "g", "check":
        {"type": "keywords", "keywords": ["done"]}}]});
    let mut cases = Vec::new();
    for (policy, why) in [(ordered(), "ordered"), (tested(), "tested")] {
        for choice in [
            named("Test"),
            ToolChoice::Required {},
            ToolChoice::Disabled {},
        ] {
            cases.push((policy.clone(), choice, why));
        }
    }
    for (policy, choice, why) in cases {
        let mut spec = policy;
        spec.tool_choice = Some(choice.clone());
        match build_with(spec) {
            Err(BuildError::Invalid(reason)) => {
                assert!(reason.contains("tool_choice"), "{why} {choice:?}: {reason}")
            }
            other => panic!("{why} {choice:?}: {:?}", other.map(|_| ())),
        }
    }
    // What does not defeat it: auto, any choice without rules, and a check
    // on the answer with no tool to call.
    let mut allowed = vec![(tested(), ToolChoice::Auto {})];
    allowed.push((with_policy(json!({})), named("Test")));
    allowed.push((with_policy(json!({})), ToolChoice::Required {}));
    allowed.push((with_policy(check_only), ToolChoice::Disabled {}));
    for (spec, choice) in allowed {
        let mut spec = spec;
        spec.tool_choice = Some(choice.clone());
        assert!(build_with(spec).is_ok(), "{choice:?}");
    }
    // With a final tool the run can end under `required`, and the model can
    // still call `Test` first; the final tool itself cannot be forced while
    // a gate needs `Test`.
    let mut spec = tested();
    spec.output_schema = Some(invoice());
    spec.tool_choice = Some(ToolChoice::Required {});
    assert!(build_with(spec.clone()).is_ok());
    spec.tool_choice = Some(named("invoice"));
    assert!(build_with(spec).is_err());
}

/// The same check at the start of a run that brings its own tool choice.
#[tokio::test]
async fn a_run_cannot_bring_a_tool_choice_its_policy_defeats() {
    let stores = Arc::new(Stores::default());
    let turns = (0..4).map(|_| text_turn("done")).collect();
    let provider = Arc::new(Replay::new(turns));
    let agent = agent_on(tested(), provider, &Runs::default(), stores.clone());
    let mut options = RunOptions::new("s1");
    options.tool_choice = Some(ToolChoice::Required {});
    let (envelopes, error) = drain(agent.run("go", options)).await;
    assert!(envelopes.is_empty());
    assert!(
        matches!(&error, Some(RunError::InvalidToolChoice { reason, .. })
            if reason.contains("policy")),
        "{error:?}"
    );
    let mut options = RunOptions::new("s1");
    options.tool_choice = Some(ToolChoice::Auto {});
    let (_, error) = drain(agent.run("go", options)).await;
    assert!(error.is_none(), "{error:?}");
}

/// `required` with the final tool: the model calls `Test` to meet the gate,
/// then ends the run through the final tool.
#[tokio::test]
async fn the_final_tool_ends_a_required_run_whose_gate_needs_another_tool() {
    let mut spec = tested();
    spec.output_schema = Some(invoice());
    spec.tool_choice = Some(ToolChoice::Required {});
    let submit = |id: &str| {
        TurnBuilder::new(MODEL)
            .tool_call(id, "invoice", &json!({"total": 1}))
            .usage(usage(10, 5))
            .build()
    };
    let turns = vec![submit("f1"), call(1, "Test"), submit("f2")];
    let stores = Arc::new(Stores::default());
    let provider = Arc::new(Replay::new(turns).without_native_output_schema());
    let agent = agent_on(spec, provider, &Runs::default(), stores.clone());
    let stream = agent.run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    finish(stream).await;
    let (status, events) = ended(&stores, &run_id).await;
    assert_eq!(status, RunStatus::Completed, "{:?}", policies(&events));
    assert_eq!(actions(&events, PolicyAction::Correct), 1);
}

/// A refusal tells the model about the tools it has: with `Lookup` hidden by
/// a skill's `allowed_tools`, the refusal of `Update` does not name it, and
/// the audit event still does.
#[tokio::test]
async fn a_refusal_does_not_name_a_tool_a_skill_hid() {
    let mut skill = Skill::new("narrow", "/skills/narrow", "Narrow.", "go");
    skill.allowed_tools = Some(vec!["Update".into()]);
    let load = TurnBuilder::new(MODEL)
        .tool_call("k1", "Skill", &json!({"skill": "narrow", "args": ""}))
        .usage(usage(10, 5))
        .build();
    let turns = vec![load, call(1, "Update"), text_turn("done")];
    let stores = Arc::new(Stores::default());
    let provider = Arc::new(Replay::new(turns));
    let mode = PermissionMode::SkipDangerous;
    let hooks = Hooks::default();
    let agent = builder(
        ordered(),
        provider,
        mode,
        hooks,
        &Runs::default(),
        stores.clone(),
    )
    .skills(vec![skill])
    .build()
    .unwrap();
    let stream = agent.run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    finish(stream).await;
    let (status, events) = ended(&stores, &run_id).await;
    assert_eq!(status, RunStatus::Completed);
    let session = messages(&*stores, "s1").await;
    let refusal = (answers(&session).into_iter())
        .find(|(id, ..)| id == "call_1_0")
        .unwrap();
    assert!(refusal.2 && refusal.1.contains("'Update'"), "{refusal:?}");
    assert!(!refusal.1.contains("Lookup"), "{refusal:?}");
    assert_eq!(policies(&events)[0]["missing"], json!(["Lookup"]));
}

/// A gate sends the answer back once, with the reminder, and the run
/// completes when the model did what it must.
#[tokio::test]
async fn a_gate_sends_one_correction_and_then_passes() {
    let turns = vec![text_turn("done"), call(1, "Test"), text_turn("really done")];
    let (stores, provider, runs, _, status, events) = run_to_end(tested(), turns).await;
    assert_eq!(status, RunStatus::Completed);
    assert_eq!(provider.calls().values().sum::<u32>(), 3);
    assert_eq!(runs.lock().unwrap().get("call_1_0"), Some(&1));
    let corrections = policies(&events);
    assert_eq!(corrections.len(), 1, "{corrections:?}");
    assert_eq!(corrections[0]["rule"], "completion");
    assert_eq!(corrections[0]["action"], "correct");
    assert_eq!(corrections[0]["gates"], json!(["tests"]));
    assert_eq!(corrections[0]["missing"], json!(["Test"]));
    assert_eq!(corrections[0]["attempt"], 1);
    let session = messages(&*stores, "s1").await;
    let reminder = serde_json::to_string(&session[2]).unwrap();
    assert!(reminder.contains("'Test'"), "{reminder}");
    assert!(reminder.contains("system-reminder"), "{reminder}");
    let Some(Event::Result(result)) = events.last().map(|e| &e.event) else {
        panic!("no result");
    };
    assert_eq!(result.final_text.as_deref(), Some("really done"));
}

/// A gate that never passes ends the run at the cap, with a named error
/// and a `stop` event, and the answer the model gave last.
#[tokio::test]
async fn a_gate_that_never_passes_ends_the_run_at_the_cap() {
    let turns: Vec<_> = (0..6).map(|_| text_turn("done")).collect();
    let (stores, provider, _, _, status, events) = run_to_end(tested(), turns).await;
    assert_eq!(status, RunStatus::Failed);
    assert_eq!(provider.calls().values().sum::<u32>(), 3, "two corrections");
    assert_eq!(actions(&events, PolicyAction::Correct), 2);
    assert_eq!(actions(&events, PolicyAction::Stop), 1);
    assert_eq!(errors(&events), ["PolicyGateError"]);
    let stop = policies(&events).pop().unwrap();
    assert_eq!(stop["action"], "stop");
    assert_eq!(stop["attempt"], 2);
    assert!(stop["detail"].as_str().unwrap().contains("2 correction(s)"));
    let Some(Event::Result(result)) = events.last().map(|e| &e.event) else {
        panic!("no result");
    };
    let result = serde_json::to_value(result).unwrap();
    assert_eq!(result["subtype"], "error");
    assert_eq!(result["stop_reason"], "error");
    assert_eq!(result["final_text"], "done");
    let session = messages(&*stores, "s1").await;
    assert_eq!(session.last().unwrap().role, Role::Assistant);
}

/// `max_corrections` 0 ends the run at the first failure; a larger cap
/// allows that many.
#[tokio::test]
async fn max_corrections_bounds_the_corrections() {
    let gate = json!({"name": "tests", "succeeded": ["Test"]});
    for (max, corrections) in [(0, 0), (1, 1), (4, 4)] {
        let spec = with_policy(json!({"gates": [gate], "max_corrections": max}));
        let turns: Vec<_> = (0..8).map(|_| text_turn("done")).collect();
        let (_, provider, _, _, status, events) = run_to_end(spec, turns).await;
        assert_eq!(status, RunStatus::Failed, "max {max}");
        assert_eq!(actions(&events, PolicyAction::Correct), corrections);
        assert_eq!(
            provider.calls().values().sum::<u32>(),
            corrections as u32 + 1
        );
    }
}

/// A gate may need a number of successful calls, and an output check, and
/// every failing gate is answered in the same correction.
#[tokio::test]
async fn gates_count_calls_check_the_answer_and_answer_together() {
    let spec = with_policy(json!({"gates": [
        {"name": "tests", "succeeded": ["Test"], "min_calls": 2},
        {"name": "cites", "check": {"type": "matches", "pattern": "\\[\\d+\\]"},
         "feedback": "Cite a source like [1]."},
    ]}));
    let turns = vec![
        text_turn("done"),
        call(1, "Test"),
        text_turn("done [1]"),
        batch(3, &["Test"]),
        text_turn("done [1]"),
    ];
    let (_, _, _, _, status, events) = run_to_end(spec, turns).await;
    assert_eq!(status, RunStatus::Completed);
    let corrections = policies(&events);
    assert_eq!(corrections.len(), 2, "{corrections:?}");
    assert_eq!(corrections[0]["gates"], json!(["tests", "cites"]));
    assert!(
        corrections[0]["detail"]
            .as_str()
            .unwrap()
            .contains("Cite a source")
    );
    assert_eq!(corrections[1]["gates"], json!(["tests"]));
    assert!(
        corrections[1]["detail"]
            .as_str()
            .unwrap()
            .contains("at least 2 times each")
    );
}

/// The gate also guards the final tool: a call that would end the run
/// goes back as its error result, and the session stays paired.
#[tokio::test]
async fn a_gate_guards_the_final_tool_too() {
    let mut spec = tested();
    spec.output_schema = Some(OutputSchema {
        name: "invoice".into(),
        schema: object(json!({
            "type": "object",
            "properties": {"total": {"type": "number"}},
            "required": ["total"],
        })),
        strict: true,
        description: None,
    });
    let submit = |id: &str| {
        TurnBuilder::new(MODEL)
            .tool_call(id, "invoice", &json!({"total": 1}))
            .usage(usage(10, 5))
            .build()
    };
    let stores = Arc::new(Stores::default());
    let turns = vec![submit("f1"), call(1, "Test"), submit("f2")];
    let provider = Arc::new(Replay::new(turns).without_native_output_schema());
    let runs = Runs::default();
    let agent = agent_on(spec.clone(), provider, &runs, stores.clone());
    let stream = agent.run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    finish(stream).await;
    let (status, events) = ended(&stores, &run_id).await;
    assert_eq!(status, RunStatus::Completed);
    assert_eq!(actions(&events, PolicyAction::Correct), 1);
    let session = messages(&*stores, "s1").await;
    let answered = answers(&session);
    assert_eq!(answered[0].0, "f1");
    assert!(answered[0].2, "the refused final call is an error result");
    assert!(answered[0].1.contains("'Test'"), "{answered:?}");
    assert!(!answered[2].2, "the last final call was taken");

    // Never passing: the run ends failed, every call answered.
    let never: Vec<_> = (0..5).map(|n| submit(&format!("f{n}"))).collect();
    let provider = Arc::new(Replay::new(never).without_native_output_schema());
    let stores = Arc::new(Stores::default());
    let agent = agent_on(spec, provider, &Runs::default(), stores.clone());
    let stream = agent.run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    finish(stream).await;
    let (status, events) = ended(&stores, &run_id).await;
    assert_eq!(status, RunStatus::Failed);
    assert_eq!(actions(&events, PolicyAction::Stop), 1);
    let session = messages(&*stores, "s1").await;
    for (at, message) in session.iter().enumerate() {
        for call in message.tool_uses() {
            let next = &session[at + 1];
            assert!(results_of(next).iter().any(|(id, ..)| *id == call.id));
        }
    }
}

/// A correction turn is a turn with a provider call and no tool batch: the
/// loop guard counts it as neither idle nor repeated, even with its
/// strictest settings, and a run that ignores its corrections ends on the
/// gate, not on the guard.
#[tokio::test]
async fn a_correction_is_not_an_idle_or_repeated_turn() {
    let strict = LoopGuard {
        max_identical_tool_calls: 2,
        max_consecutive_failures: 1,
        max_idle_turns: 1,
        ..LoopGuard::default()
    };
    let mut passing = tested();
    passing.loop_guard = strict.clone();
    let turns = vec![
        text_turn("done"),
        text_turn("done"),
        call(2, "Test"),
        text_turn("done"),
    ];
    let (_, _, _, _, status, events) = run_to_end(passing, turns).await;
    assert_eq!(status, RunStatus::Completed, "{:?}", kinds(&events));
    assert_eq!(actions(&events, PolicyAction::Correct), 2);
    assert!(!kinds(&events).contains(&"loop_guard".to_owned()));

    let mut ignoring = tested();
    ignoring.loop_guard = strict;
    let turns: Vec<_> = (0..5).map(|_| text_turn("done")).collect();
    let (_, _, _, _, status, events) = run_to_end(ignoring, turns).await;
    assert_eq!(status, RunStatus::Failed);
    assert_eq!(errors(&events), ["PolicyGateError"]);
    assert!(!kinds(&events).contains(&"loop_guard".to_owned()));
}

/// A model that keeps calling a tool it may not is a failing run like any
/// other: three refused batches in a row are the loop guard's.
#[tokio::test]
async fn refused_batches_in_a_row_are_a_failure_streak() {
    let turns: Vec<_> = (0..6).map(|n| call(n, "Update")).collect();
    let (_, provider, runs, _, status, events) = run_to_end(ordered(), turns).await;
    assert_eq!(status, RunStatus::Failed);
    assert_eq!(provider.calls().values().sum::<u32>(), 3);
    assert!(runs.lock().unwrap().is_empty());
    assert_eq!(actions(&events, PolicyAction::Refuse), 3);
    assert_eq!(errors(&events), ["LoopGuardError"]);
}

/// A policy that cannot run fails the build, naming why, rather than
/// being dropped.
#[test]
fn a_policy_that_cannot_run_fails_the_build() {
    let build = |policy: Value| {
        let stores = Arc::new(Stores::default());
        let provider = Arc::new(Replay::new(vec![]));
        let spec = with_policy(policy);
        let mode = PermissionMode::SkipDangerous;
        let builder = builder(
            spec,
            provider,
            mode,
            Hooks::default(),
            &Runs::default(),
            stores,
        );
        match builder.build() {
            Err(BuildError::Invalid(reason)) => reason,
            Err(other) => panic!("{other:?}"),
            Ok(_) => String::new(),
        }
    };
    let need = |tool: &str, requires: &[&str]| json!({"tool": tool, "requires": requires});
    let cases = [
        (
            json!({"prerequisites": [need("Nope", &["Lookup"])]}),
            "not a tool the agent has",
        ),
        (
            json!({"prerequisites": [need("Update", &["Nope"])]}),
            "not a tool the agent has",
        ),
        (
            json!({"prerequisites": [need("Update", &["Update"])]}),
            "cannot require itself",
        ),
        (
            json!({"prerequisites": [need("Update", &[])]}),
            "requires no tool",
        ),
        (
            json!({"prerequisites": [need("Update", &["Lookup", "Lookup"])]}),
            "twice",
        ),
        (
            json!({"prerequisites": [need("Update", &["Lookup"]), need("Update", &["Test"])]}),
            "two prerequisites name",
        ),
        (
            json!({"prerequisites": [
                need("Update", &["Lookup"]), need("Lookup", &["Test"]), need("Test", &["Update"]),
            ]}),
            "lead back to",
        ),
        (
            json!({"gates": [{"name": " ", "succeeded": ["Test"]}]}),
            "must not be blank",
        ),
        (
            json!({"gates": [{"name": "a", "succeeded": ["Test"]}, {"name": "a", "succeeded": ["Test"]}]}),
            "two gates are named",
        ),
        (json!({"gates": [{"name": "a"}]}), "asks for nothing"),
        (
            json!({"gates": [{"name": "a", "succeeded": ["Test"], "min_calls": 0}]}),
            "min_calls",
        ),
        (
            json!({"gates": [{"name": "a", "succeeded": ["Nope"]}]}),
            "not a tool the agent has",
        ),
        (
            json!({"gates": [{"name": "a", "check": {"type": "matches", "pattern": "("}}]}),
            "gate \"a\"",
        ),
    ];
    for (policy, why) in cases {
        let reason = build(policy.clone());
        assert!(reason.contains(why), "{policy}: {reason:?}");
    }
    assert_eq!(build(json!({})), "", "an empty policy is allowed");
}

/// A policy naming the final tool is refused: its call never runs, so it
/// could never be a prerequisite or evidence.
#[test]
fn the_final_tool_cannot_be_named() {
    let mut spec = tested();
    spec.final_tool_name = Some("Lookup".into());
    let provider = Arc::new(Replay::new(vec![]));
    let runs = Runs::default();
    let mut gate = spec.clone();
    gate.policy = Some(
        serde_json::from_value(json!({"gates": [{"name": "g", "succeeded": ["Lookup"]}]})).unwrap(),
    );
    let mut prerequisite = spec;
    prerequisite.policy = Some(
        serde_json::from_value(
            json!({"prerequisites": [{"tool": "Update", "requires": ["Lookup"]}]}),
        )
        .unwrap(),
    );
    for spec in [gate, prerequisite] {
        let stores = Arc::new(Stores::default());
        let mode = PermissionMode::SkipDangerous;
        let built = builder(
            spec,
            provider.clone(),
            mode,
            Hooks::default(),
            &runs,
            stores,
        )
        .build();
        assert!(
            matches!(&built, Err(BuildError::Invalid(why)) if why.contains("final tool")),
            "{:?}",
            built.err()
        );
    }
}

/// An agent with no policy keeps the contract its runs were created with
/// (the digest `e2e_tool_choice` pins); one with a policy has its own, and
/// no two policies share one.
#[test]
fn contracts_without_a_policy_are_unchanged() {
    let contract = |spec: AgentSpec| {
        let stores = Arc::new(Stores::default());
        let provider = Arc::new(Replay::new(vec![]));
        let mode = PermissionMode::SkipDangerous;
        let builder = builder(
            spec,
            provider,
            mode,
            Hooks::default(),
            &Runs::default(),
            stores,
        );
        builder.build().unwrap().contract().to_string()
    };
    let plain = {
        let stores = Arc::new(Stores::default());
        let tools = registry(vec![Echo::new("Read", ToolScope::Read, &Runs::default())]);
        let built = AgentBuilder::new(spec())
            .provider(Arc::new(Replay::new(vec![])))
            .tools(tools)
            .permissions(policy(PermissionMode::SkipDangerous))
            .run_store(stores.clone())
            .session_store(stores)
            .namespace(NS)
            .build();
        built.unwrap().contract().to_string()
    };
    assert_eq!(
        plain,
        "b2853525e856d78c5977856318e7b5563dbc291c1d1e9e8bedf0500577b3011e"
    );
    let unset = contract(spec());
    let ordering = contract(ordered());
    let gating = contract(tested());
    assert_ne!(unset, ordering);
    assert_ne!(ordering, gating);
    let empty = contract(with_policy(json!({})));
    assert!(![&unset, &ordering, &gating].contains(&&empty), "{empty}");
}

/// The policy as a typed value reads and writes the same wire form.
#[test]
fn a_policy_reads_strictly() {
    let policy: ReliabilityPolicy = serde_json::from_value(json!({
        "prerequisites": [{"tool": "Update", "requires": ["Lookup"]}],
        "gates": [{"name": "g", "succeeded": ["Test"], "min_calls": 2}],
        "max_corrections": 3,
    }))
    .unwrap();
    let wire = serde_json::to_value(&policy).unwrap();
    assert_eq!(
        serde_json::from_value::<ReliabilityPolicy>(wire).unwrap(),
        policy
    );
    for typo in [
        json!({"prerequisite": []}),
        json!({"gates": [{"name": "g", "required": ["Test"]}]}),
        json!({"prerequisites": [{"tool": "a", "needs": ["b"]}]}),
    ] {
        assert!(serde_json::from_value::<ReliabilityPolicy>(typo).is_err());
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

/// What one crash left, once recovered.
struct Crashed {
    context: String,
    provider: Arc<Replay>,
    runs: Runs,
    status: RunStatus,
    events: Vec<Envelope>,
    session: Vec<Message>,
    /// A crash left the placeholder of an interrupted call.
    interrupted: bool,
}

/// Run `turns` under `spec` on stores that crash at write `n`, before or
/// after it lands, for every write a clean run makes, and recover each.
async fn crash_everywhere(
    spec: AgentSpec,
    turns: impl Fn() -> Vec<ScriptedTurn>,
    native: bool,
) -> (Vec<Crashed>, Crashed) {
    crash_everywhere_with(spec, turns, native, Hooks::default).await
}

/// [`crash_everywhere`] on an agent with `hooks`.
async fn crash_everywhere_with(
    spec: AgentSpec,
    turns: impl Fn() -> Vec<ScriptedTurn>,
    native: bool,
    hooks: impl Fn() -> Hooks,
) -> (Vec<Crashed>, Crashed) {
    let provider = || {
        let replay = Replay::new(turns());
        Arc::new(match native {
            true => replay,
            false => replay.without_native_output_schema(),
        })
    };
    let clean_stores = Arc::new(Stores::default());
    let runs = Runs::default();
    let agent = agent_with(
        spec.clone(),
        provider(),
        hooks(),
        &runs,
        clean_stores.clone(),
    );
    let stream = agent.run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    finish(stream).await;
    let writes = clean_stores.log.lock().unwrap().len() as u64;
    let (status, events) = ended(&clean_stores, &run_id).await;
    let session = messages(&*clean_stores, "s1").await;
    let clean = Crashed {
        context: "clean".into(),
        provider: provider(),
        runs,
        status,
        events,
        session,
        interrupted: false,
    };
    let mut crashed = Vec::new();
    for n in 1..=writes {
        for timing in [FaultTiming::Before, FaultTiming::After] {
            let context = format!("write {n} failing {timing:?}");
            let (stores, runs, provider) =
                (Arc::new(Stores::default()), Runs::default(), provider());
            let faulty = Arc::new(Faulty::new(stores.clone(), n, timing));
            let doomed = agent_with(spec.clone(), provider.clone(), hooks(), &runs, faulty);
            let stream = doomed.run("go", RunOptions::new("s1"));
            let run_id = stream.run_id().clone();
            assert!(drain(stream).await.1.is_some(), "{context}");
            let agent = agent_with(
                spec.clone(),
                provider.clone(),
                hooks(),
                &runs,
                stores.clone(),
            );
            let run_id = recover(&agent, &stores, run_id).await;
            let (status, events) = ended(&stores, &run_id).await;
            let session = messages(&*stores, "s1").await;
            let interrupted = (session.iter()).any(|message| {
                result_texts(message) == [linch_core::journal::INTERRUPTED_TOOL_TEXT]
            });
            crashed.push(Crashed {
                context,
                provider,
                runs,
                status,
                events,
                session,
                interrupted,
            });
        }
    }
    (crashed, clean)
}

/// No refusal is reported twice, whatever re-plan a crash caused.
fn assert_refused_once(run: &Crashed) {
    let mut ids: Vec<String> = (policies(&run.events).iter())
        .filter(|event| event["action"] == "refuse")
        .map(|event| event["tool_use_id"].as_str().unwrap().to_owned())
        .collect();
    let all = ids.len();
    ids.sort();
    ids.dedup();
    assert_eq!(ids.len(), all, "{}: {ids:?}", run.context);
}

/// The ordering holds at every crash point: the call that needs a
/// prerequisite never runs before the prerequisite succeeded, no call runs
/// twice, and no refusal is reported twice. A crash that left no
/// interrupted placeholder resumes to the clean run's session and
/// refusals; one that did (the prerequisite's own result) leaves the run
/// without the success, so the dependant is refused again.
#[tokio::test]
async fn a_crash_at_any_write_keeps_the_prerequisites() {
    let turns = || {
        vec![
            call(0, "Update"),
            call(1, "Lookup"),
            call(2, "Update"),
            text_turn("done"),
        ]
    };
    let (crashed, clean) = crash_everywhere(ordered(), turns, true).await;
    assert_eq!(clean.status, RunStatus::Completed);
    assert!(!crashed.is_empty());
    for run in &crashed {
        let context = &run.context;
        assert_refused_once(run);
        let ran = run.runs.lock().unwrap().clone();
        assert!(ran.values().all(|&n| n == 1), "{context}: {ran:?}");
        assert!(
            !ran.contains_key("call_0_0"),
            "{context}: ran before Lookup"
        );
        let calls = run.provider.calls();
        assert!(calls.values().all(|&n| n <= 2), "{context}: {calls:?}");
        let refused = actions(&run.events, PolicyAction::Refuse);
        match run.interrupted {
            false => {
                assert_eq!(run.status, RunStatus::Completed, "{context}");
                assert_eq!(run.session, clean.session, "{context}");
                assert_eq!(refused, 1, "{context}");
                assert_eq!(ran.get("call_2_0"), Some(&1), "{context}");
            }
            true => {
                let lookup = (answers(&run.session).iter())
                    .any(|(id, _, failed)| id == "call_1_0" && !failed);
                let update = ran.contains_key("call_2_0");
                assert!(!update || lookup, "{context}: Update without Lookup");
                if lookup {
                    // The crash interrupted Update itself: it is not run
                    // again, and nothing else is refused.
                    assert_eq!(refused, 1, "{context}: {ran:?}");
                } else {
                    // A Lookup that did not finish did not unlock Update,
                    // so the run saw three failed batches in a row: the
                    // guard's.
                    assert_eq!(refused, 2, "{context}: {ran:?}");
                    assert_eq!(run.status, RunStatus::Failed, "{context}");
                    assert_eq!(errors(&run.events), ["LoopGuardError"], "{context}");
                }
            }
        }
        let session = answers(&run.session);
        assert!(
            session.iter().all(|(id, ..)| run
                .session
                .iter()
                .any(|m| m.tool_uses().any(|c| &c.id == id))),
            "{context}: paired"
        );
    }
}

/// A gate cannot be skipped by a crash: whatever write fails, the run
/// ends `completed` only with a successful `Test` behind it, and a crash
/// that left no placeholder resumes to the clean run's one correction.
#[tokio::test]
async fn a_crash_at_any_write_cannot_skip_a_gate() {
    let turns = || {
        vec![
            text_turn("done"),
            call(1, "Test"),
            text_turn("done"),
            call(3, "Test"),
            text_turn("done"),
        ]
    };
    let (crashed, clean) = crash_everywhere(tested(), turns, true).await;
    assert_eq!(clean.status, RunStatus::Completed);
    assert_eq!(actions(&clean.events, PolicyAction::Correct), 1);
    for run in &crashed {
        let context = &run.context;
        assert_eq!(run.status, RunStatus::Completed, "{context}");
        let tested = answers(&run.session)
            .iter()
            .any(|(id, _, failed)| id.starts_with("call_") && !failed);
        assert!(tested, "{context}: completed without a successful Test");
        let corrections = actions(&run.events, PolicyAction::Correct);
        let want = if run.interrupted { 2 } else { 1 };
        assert_eq!(corrections, want, "{context}");
        assert_eq!(actions(&run.events, PolicyAction::Stop), 0, "{context}");
        let attempts: Vec<u64> = (policies(&run.events).iter())
            .map(|event| event["attempt"].as_u64().unwrap())
            .collect();
        assert_eq!(attempts, (1..=want as u64).collect::<Vec<_>>(), "{context}");
        let ran = run.runs.lock().unwrap().clone();
        assert!(ran.values().all(|&n| n == 1), "{context}: {ran:?}");
        if !run.interrupted {
            assert_eq!(run.session, clean.session, "{context}");
        }
    }
}

/// The same for a gate that never passes: no crash point lets the run
/// complete, none spends a third correction, and each ends with one stop.
#[tokio::test]
async fn a_crash_at_any_write_still_stops_a_gate_that_never_passes() {
    let turns = || (0..6).map(|_| text_turn("done")).collect();
    let (crashed, clean) = crash_everywhere(tested(), turns, true).await;
    assert_eq!(clean.status, RunStatus::Failed);
    for run in &crashed {
        let context = &run.context;
        assert_eq!(run.status, RunStatus::Failed, "{context}");
        assert_eq!(actions(&run.events, PolicyAction::Correct), 2, "{context}");
        assert_eq!(actions(&run.events, PolicyAction::Stop), 1, "{context}");
        assert_eq!(errors(&run.events), ["PolicyGateError"], "{context}");
        let calls = run.provider.calls();
        assert!(calls.keys().all(|&turn| turn <= 2), "{context}: {calls:?}");
        assert_eq!(run.session, clean.session, "{context}");
    }
}

/// And through the final tool: every call stays answered at every crash
/// point, and a gate never lets the answer through.
#[tokio::test]
async fn a_crash_at_any_write_still_stops_a_final_tool_gate() {
    let mut spec = tested();
    spec.output_schema = Some(OutputSchema {
        name: "invoice".into(),
        schema: object(json!({"type": "object", "properties": {"total": {"type": "number"}}})),
        strict: true,
        description: None,
    });
    let turns = || {
        (0..6)
            .map(|n| {
                TurnBuilder::new(MODEL)
                    .tool_call(&format!("f{n}"), "invoice", &json!({"total": n}))
                    .usage(usage(10, 5))
                    .build()
            })
            .collect()
    };
    let (crashed, clean) = crash_everywhere(spec, turns, false).await;
    assert_eq!(clean.status, RunStatus::Failed);
    for run in &crashed {
        let context = &run.context;
        assert_eq!(run.status, RunStatus::Failed, "{context}");
        assert_eq!(actions(&run.events, PolicyAction::Correct), 2, "{context}");
        assert_eq!(actions(&run.events, PolicyAction::Stop), 1, "{context}");
        for (at, message) in run.session.iter().enumerate() {
            for call in message.tool_uses() {
                let next = run.session.get(at + 1);
                let paired =
                    next.is_some_and(|m| results_of(m).iter().any(|(id, ..)| *id == call.id));
                assert!(paired, "{context}: {} unanswered", call.id);
            }
        }
        assert_eq!(run.session, clean.session, "{context}");
    }
}

fn noted() -> OutputSchema {
    OutputSchema {
        name: "report".into(),
        schema: object(json!({
            "type": "object",
            "properties": {"note": {"type": "string"}},
            "required": ["note"],
        })),
        strict: true,
        description: None,
    }
}

fn report(id: &str, note: &str) -> ScriptedTurn {
    TurnBuilder::new(MODEL)
        .tool_call(id, "report", &json!({"note": note}))
        .usage(usage(10, 5))
        .build()
}

/// A final tool's input is judged as the strings it holds, not only as its
/// JSON text, where a quote or a newline is escaped: a pattern that names
/// either still catches them.
#[tokio::test]
async fn a_forbidden_pattern_is_not_dodged_by_json_escaping() {
    for (pattern, note) in [("say \"hi\"", "please say \"hi\" now"), ("a\\nb", "a\nb")] {
        let mut spec = with_policy(json!({"gates": [{"name": "clean",
            "check": {"type": "forbids", "pattern": pattern}}]}));
        spec.output_schema = Some(noted());
        let turns = vec![report("f1", note), report("f2", "fine")];
        let stores = Arc::new(Stores::default());
        let provider = Arc::new(Replay::new(turns).without_native_output_schema());
        let agent = agent_on(spec, provider, &Runs::default(), stores.clone());
        let stream = agent.run("go", RunOptions::new("s1"));
        let run_id = stream.run_id().clone();
        finish(stream).await;
        let (status, events) = ended(&stores, &run_id).await;
        assert_eq!(status, RunStatus::Completed, "{pattern:?}");
        assert_eq!(actions(&events, PolicyAction::Correct), 1, "{pattern:?}");
        let session = messages(&*stores, "s1").await;
        let answered = answers(&session);
        assert!(answered[0].2, "the first answer went back: {answered:?}");
    }
    // A required text may be in a string only: its JSON text escapes it.
    let mut spec = with_policy(json!({"gates": [{"name": "lines",
        "check": {"type": "matches", "pattern": "x\ny"}}]}));
    spec.output_schema = Some(noted());
    let stores = Arc::new(Stores::default());
    let turns = vec![report("f1", "x\ny")];
    let provider = Arc::new(Replay::new(turns).without_native_output_schema());
    let agent = agent_on(spec, provider, &Runs::default(), stores.clone());
    let stream = agent.run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    finish(stream).await;
    let (status, events) = ended(&stores, &run_id).await;
    assert_eq!(status, RunStatus::Completed);
    assert_eq!(actions(&events, PolicyAction::Correct), 0);
}

/// A `stop` hook that rewrites the answer into one a gate forbids does not
/// get it through: the run ends failed with one `stop` event and no
/// correction, on the text route and on the final tool's.
#[tokio::test]
async fn a_stop_rewrite_is_gated_again() {
    let gates = json!({"gates": [{"name": "clean", "check":
        {"type": "forbids", "pattern": "SECRET"}}]});
    let stores = Arc::new(Stores::default());
    let provider = Arc::new(Replay::new(vec![text_turn("all good")]));
    let spec = with_policy(gates.clone());
    let hooks = rewriting("SECRET");
    let agent = agent_with(spec, provider, hooks, &Runs::default(), stores.clone());
    let stream = agent.run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    finish(stream).await;
    let (status, events) = ended(&stores, &run_id).await;
    assert_eq!(status, RunStatus::Failed);
    assert_eq!(actions(&events, PolicyAction::Correct), 0);
    assert_eq!(actions(&events, PolicyAction::Stop), 1);
    assert_eq!(errors(&events), ["PolicyGateError"]);
    let detail = policies(&events)[0]["detail"].as_str().unwrap().to_owned();
    assert!(detail.contains("rewrote"), "{detail}");

    let mut spec = with_policy(gates);
    spec.output_schema = Some(noted());
    let stores = Arc::new(Stores::default());
    let turns = vec![report("f1", "all good")];
    let provider = Arc::new(Replay::new(turns).without_native_output_schema());
    let hooks = rewriting("{\"note\": \"SECRET\"}");
    let agent = agent_with(spec, provider, hooks, &Runs::default(), stores.clone());
    let stream = agent.run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    finish(stream).await;
    let (status, events) = ended(&stores, &run_id).await;
    assert_eq!(status, RunStatus::Failed);
    assert_eq!(actions(&events, PolicyAction::Stop), 1);
    let session = messages(&*stores, "s1").await;
    for (at, message) in session.iter().enumerate() {
        for call in message.tool_uses() {
            let next = &session[at + 1];
            assert!(results_of(next).iter().any(|(id, ..)| *id == call.id));
        }
    }
}

/// A crash at any write cannot let a rewritten answer past the gate.
#[tokio::test]
async fn a_crash_at_any_write_still_gates_a_rewritten_answer() {
    let spec = with_policy(json!({"gates": [{"name": "clean", "check":
        {"type": "forbids", "pattern": "SECRET"}}]}));
    let turns = || vec![text_turn("all good")];
    let hooks = || rewriting("SECRET");
    let (crashed, clean) = crash_everywhere_with(spec, turns, true, hooks).await;
    assert_eq!(clean.status, RunStatus::Failed);
    for run in &crashed {
        let context = &run.context;
        assert_eq!(run.status, RunStatus::Failed, "{context}");
        assert_eq!(actions(&run.events, PolicyAction::Stop), 1, "{context}");
        assert_eq!(actions(&run.events, PolicyAction::Correct), 0, "{context}");
        assert_eq!(errors(&run.events), ["PolicyGateError"], "{context}");
    }
}
