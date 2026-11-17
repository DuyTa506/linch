//! `tool_choice` and the final tool: the choice on the agent and per run,
//! the final tool a provider without native structured output is offered,
//! its call ending the run with the checked object, retries, and a crash
//! at every write.

mod common;

use std::sync::{Arc, Mutex};

use common::hooks::hook;
use common::*;
use linch_core::events::kinds::{ResultEvent, ResultSubtype, VerificationAction};
use linch_core::hooks::{PreToolUse, PreToolUseResponse, Stop, StopResponse};
use linch_core::provider::scripted::TurnBuilder;
use linch_core::store::faulty::{FaultTiming, Faulty};
use linch_core::{
    Agent, AgentBuilder, AgentSpec, BuildError, ContentBlock, Envelope, Event, FailMode, Hooks,
    JsonObject, Message, OutputSchema, PermissionMode, Provider, Role, RunError, RunOptions,
    RunStatus, RunStore, ScriptedProvider, ScriptedTurn, SessionStore, ToolChoice, ToolScope,
};
use serde_json::{Value, json};

fn schema() -> OutputSchema {
    OutputSchema {
        name: "invoice".into(),
        schema: object(json!({
            "type": "object",
            "properties": {"total": {"type": "number"}},
            "required": ["total"],
            "additionalProperties": false,
        })),
        strict: true,
        description: None,
    }
}

fn shaped(retries: u32) -> AgentSpec {
    let mut spec = spec();
    spec.output_schema = Some(schema());
    spec.structured_output_retries = retries;
    spec
}

fn builder<S: RunStore + SessionStore + 'static>(
    spec: AgentSpec,
    provider: Arc<dyn Provider>,
    tools: Vec<Echo>,
    hooks: Hooks,
    stores: Arc<S>,
) -> AgentBuilder {
    AgentBuilder::new(spec)
        .provider(provider)
        .tools(registry(tools))
        .permissions(policy(PermissionMode::SkipDangerous))
        .hooks(Arc::new(hooks))
        .run_store(stores.clone())
        .session_store(stores)
        .namespace(NS)
}

fn read(runs: &Runs) -> Vec<Echo> {
    vec![Echo::new("Read", ToolScope::Read, runs)]
}

fn build(spec: AgentSpec, provider: Arc<dyn Provider>) -> Agent {
    let stores = Arc::new(Stores::default());
    let tools = read(&Runs::default());
    (builder(spec, provider, tools, Hooks::default(), stores).build()).unwrap()
}

/// An agent on `stores` whose provider has no native structured output.
fn final_agent<S: RunStore + SessionStore + 'static>(
    spec: AgentSpec,
    provider: Arc<dyn Provider>,
    runs: &Runs,
    hooks: Hooks,
    stores: Arc<S>,
) -> Agent {
    (builder(spec, provider, read(runs), hooks, stores).build()).unwrap()
}

fn scripted(turns: impl IntoIterator<Item = ScriptedTurn>) -> Arc<ScriptedProvider> {
    Arc::new(ScriptedProvider::new(turns).without_native_output_schema())
}

/// A response that calls `tool` with `input`, as call `id`.
fn call(id: &str, tool: &str, input: Value) -> ScriptedTurn {
    TurnBuilder::new(MODEL)
        .tool_call(id, tool, &input)
        .usage(usage(10, 5))
        .build()
}

/// A response that calls each `(id, tool, input)`.
fn calls(list: &[(&str, &str, Value)]) -> ScriptedTurn {
    let mut builder = TurnBuilder::new(MODEL);
    for (id, tool, input) in list {
        builder = builder.tool_call(id, tool, input);
    }
    builder.usage(usage(10, 5)).build()
}

fn result(envelopes: &[Envelope]) -> ResultEvent {
    match &envelopes.last().unwrap().event {
        Event::Result(result) => result.clone(),
        other => panic!("ended with {other:?}"),
    }
}

fn verifications(envelopes: &[Envelope]) -> Vec<(VerificationAction, u64)> {
    (envelopes.iter())
        .filter_map(|envelope| match &envelope.event {
            Event::Verification(event) => Some((event.action, event.attempt)),
            _ => None,
        })
        .collect()
}

fn tool_events(envelopes: &[Envelope]) -> usize {
    (kinds(envelopes).iter())
        .filter(|kind| kind.starts_with("tool_call"))
        .count()
}

fn total(value: u64) -> JsonObject {
    object(json!({"total": value}))
}

/// The results of `message`: `(tool_use_id, text, is_error)`.
fn results(message: &Message) -> Vec<(String, String, bool)> {
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

/// Every response's tool calls are answered by the message after it.
fn assert_paired(messages: &[Message], context: &str) {
    for (at, message) in messages.iter().enumerate() {
        let ids: Vec<_> = message.tool_uses().map(|call| call.id.clone()).collect();
        if ids.is_empty() {
            continue;
        }
        let next = messages.get(at + 1).unwrap_or_else(|| {
            panic!("{context}: the calls of message {at} have no results");
        });
        assert_eq!(next.role, Role::User, "{context}");
        let answered: Vec<_> = results(next).into_iter().map(|(id, ..)| id).collect();
        assert_eq!(answered, ids, "{context}: message {at}");
    }
}

/// Digests taken before `tool_choice` and the final tool existed: an agent
/// that sets neither, on a provider with native structured output, keeps
/// the contract its runs were created with.
#[test]
fn contracts_without_tool_choice_are_unchanged() {
    let provider = || Arc::new(ScriptedProvider::new([]));
    let plain = build(spec(), provider());
    assert_eq!(
        plain.contract().to_string(),
        "b2853525e856d78c5977856318e7b5563dbc291c1d1e9e8bedf0500577b3011e"
    );
    let native = build(shaped(2), provider());
    assert_eq!(
        native.contract().to_string(),
        "20375b704ee604b451865db65a907649a24e2cd6c4d0cdacc7e0c408071687c4"
    );
    // The final tool, a tool choice and a named final tool each change it.
    let final_tool = build(shaped(2), scripted([]));
    assert_ne!(final_tool.contract(), native.contract());
    let mut choosing = shaped(2);
    choosing.tool_choice = Some(ToolChoice::Required {});
    assert_ne!(build(choosing, provider()).contract(), native.contract());
    let mut named = shaped(2);
    named.final_tool_name = Some("Read".into());
    assert_ne!(build(named, provider()).contract(), native.contract());
}

#[tokio::test]
async fn the_agents_tool_choice_goes_with_every_request_a_run_may_replace_it() {
    let provider = Arc::new(ScriptedProvider::new([
        tool_turn(0, 1, "Read"),
        text_turn("done"),
        text_turn("again"),
    ]));
    let mut spec = spec();
    spec.tool_choice = Some(ToolChoice::Auto {});
    let agent = build(spec, provider.clone());
    finish(agent.run("go", RunOptions::new("s1"))).await;
    let mut options = RunOptions::new("s2");
    let read = ToolChoice::Tool {
        name: "Read".into(),
    };
    options.tool_choice = Some(read.clone());
    let envelopes = finish(agent.run("go", options)).await;
    let choices: Vec<_> = (provider.requests().into_iter())
        .map(|request| request.tool_choice)
        .collect();
    let auto = Some(ToolChoice::Auto {});
    assert_eq!(choices, [auto.clone(), auto, Some(read.clone())]);
    let Event::Init(init) = &envelopes[0].event else {
        panic!("a run starts with init");
    };
    assert_eq!(init.tool_choice, Some(read));
    assert_eq!(init.final_tool, None);
}

#[tokio::test]
async fn a_turn_with_no_tools_sends_no_tool_choice_and_none_can_be_required() {
    let toolless = |choice: ToolChoice, provider: Arc<ScriptedProvider>| {
        let mut spec = spec();
        spec.tool_choice = Some(choice);
        let stores = Arc::new(Stores::default());
        AgentBuilder::new(spec)
            .provider(provider)
            .tools(registry(vec![]))
            .permissions(policy(PermissionMode::SkipDangerous))
            .run_store(stores.clone())
            .session_store(stores)
            .namespace(NS)
            .build()
    };
    let provider = Arc::new(ScriptedProvider::new([text_turn("done")]));
    let agent = toolless(ToolChoice::Auto {}, provider.clone()).unwrap();
    finish(agent.run("go", RunOptions::new("s1"))).await;
    assert_eq!(provider.requests()[0].tool_choice, None);
    let required = toolless(ToolChoice::Required {}, provider);
    assert!(
        matches!(&required, Err(BuildError::Invalid(reason)) if reason.contains("offers none")),
        "a forced choice is never dropped silently"
    );
}

/// Records each request's tool choice, and answers as [`Replay`] does.
struct Recording {
    replay: Replay,
    choices: Mutex<Vec<Option<ToolChoice>>>,
}

#[async_trait::async_trait]
impl Provider for Recording {
    fn id(&self) -> &str {
        "recording"
    }

    async fn stream(
        &self,
        request: linch_core::ProviderRequest,
        cancel: tokio_util::sync::CancellationToken,
    ) -> Result<linch_core::ProviderStream, linch_core::ProviderError> {
        let choice = request.tool_choice.clone();
        self.choices.lock().unwrap().push(choice);
        self.replay.stream(request, cancel).await
    }
}

/// A run's own tool choice survives a crash at any write: every request,
/// a resumed run's included, carries it, not the agent's.
#[tokio::test]
async fn a_resumed_run_keeps_its_own_tool_choice() {
    let turns = || vec![tool_turn(0, 1, "Read"), text_turn("done")];
    let options = || {
        let mut options = RunOptions::new("s1");
        options.tool_choice = Some(ToolChoice::Auto {});
        options
    };
    let mut spec = spec();
    spec.tool_choice = Some(ToolChoice::Tool {
        name: "Read".into(),
    });
    fn agent_on<S: RunStore + SessionStore + 'static>(
        spec: &AgentSpec,
        provider: Arc<dyn Provider>,
        stores: Arc<S>,
    ) -> Agent {
        let tools = read(&Runs::default());
        (builder(spec.clone(), provider, tools, Hooks::default(), stores).build()).unwrap()
    }
    let clean = Arc::new(Stores::default());
    let agent = agent_on(&spec, Arc::new(Replay::new(turns())), clean.clone());
    finish(agent.run("go", options())).await;
    let writes = clean.log.lock().unwrap().len() as u64;
    let mut resumed_calls = 0;
    for n in 1..=writes {
        let stores = Arc::new(Stores::default());
        let provider = Arc::new(Recording {
            replay: Replay::new(turns()),
            choices: Mutex::new(Vec::new()),
        });
        let faulty = Arc::new(Faulty::new(stores.clone(), n, FaultTiming::After));
        let stream = agent_on(&spec, provider.clone(), faulty).run("go", options());
        let run_id = stream.run_id().clone();
        drain(stream).await;
        let before = provider.choices.lock().unwrap().len();
        if let Ok(record) = stores.runs.load(NS, &run_id).await
            && !record.status.is_terminal()
        {
            let agent = agent_on(&spec, provider.clone(), stores.clone());
            finish(agent.resume(run_id, RunOptions::new("s1"))).await;
        }
        let choices = provider.choices.lock().unwrap().clone();
        resumed_calls += choices.len() - before;
        let auto = Some(ToolChoice::Auto {});
        assert!(choices.iter().all(|c| *c == auto), "write {n}: {choices:?}");
    }
    assert!(resumed_calls > 0, "some resume called the provider");
}

/// A provider that refuses a forced choice, as DeepSeek does while it
/// thinks.
struct Refusing;

#[async_trait::async_trait]
impl Provider for Refusing {
    fn id(&self) -> &str {
        "refusing"
    }

    fn tool_choice_refusal(
        &self,
        choice: &ToolChoice,
        _thinking: Option<&linch_core::Thinking>,
    ) -> Option<String> {
        matches!(choice, ToolChoice::Required {}).then(|| "no forced choice".into())
    }

    async fn stream(
        &self,
        _request: linch_core::ProviderRequest,
        _cancel: tokio_util::sync::CancellationToken,
    ) -> Result<linch_core::ProviderStream, linch_core::ProviderError> {
        unreachable!("no run calls it")
    }
}

#[tokio::test]
async fn an_unusable_tool_choice_does_not_build_and_a_run_of_one_writes_nothing() {
    let invalid = |spec: AgentSpec, provider: Arc<dyn Provider>| {
        let stores = Arc::new(Stores::default());
        let tools = read(&Runs::default());
        match builder(spec, provider, tools, Hooks::default(), stores).build() {
            Err(BuildError::Invalid(reason)) => reason,
            Err(other) => panic!("{other:?}"),
            Ok(_) => panic!("built"),
        }
    };
    let mut unknown = spec();
    unknown.tool_choice = Some(ToolChoice::Tool {
        name: "Write".into(),
    });
    let reason = invalid(unknown, Arc::new(ScriptedProvider::new([])));
    assert!(reason.contains("\"Write\""), "{reason}");
    let mut required = spec();
    required.tool_choice = Some(ToolChoice::Required {});
    let reason = invalid(required, Arc::new(Refusing));
    assert!(reason.contains("refusing refuses"), "{reason}");
    assert!(reason.contains("no forced choice"), "{reason}");
    let mut final_tool = shaped(0);
    final_tool.final_tool_name = Some("Submit".into());
    let reason = invalid(final_tool, Arc::new(ScriptedProvider::new([])));
    assert!(reason.contains("not a registered tool"), "{reason}");
    let mut taken = shaped(0);
    taken.output_schema.as_mut().unwrap().name = "Read".into();
    let reason = invalid(taken.clone(), scripted([]));
    assert!(reason.contains("registered tool's"), "{reason}");
    // A provider that takes the schema natively makes no tool of it.
    build(taken, Arc::new(ScriptedProvider::new([])));
    let mut spaced = shaped(0);
    spaced.output_schema.as_mut().unwrap().name = "my invoice".into();
    let reason = invalid(spaced, scripted([]));
    assert!(reason.contains("cannot name a tool"), "{reason}");
    // The final tool may be the named choice.
    let mut forced = shaped(0);
    forced.tool_choice = Some(ToolChoice::Tool {
        name: "invoice".into(),
    });
    build(forced, scripted([]));

    let stores = Arc::new(Stores::default());
    let tools = read(&Runs::default());
    let provider = Arc::new(Refusing);
    let agent =
        (builder(spec(), provider, tools, Hooks::default(), stores.clone()).build()).unwrap();
    let mut options = RunOptions::new("s1");
    options.tool_choice = Some(ToolChoice::Required {});
    let (envelopes, error) = drain(agent.run("go", options)).await;
    assert!(envelopes.is_empty());
    assert!(
        matches!(&error, Some(RunError::InvalidToolChoice { reason, .. })
            if reason.contains("no forced choice")),
        "{error:?}"
    );
    assert!(stores.log.lock().unwrap().is_empty(), "nothing was written");
}

#[tokio::test]
async fn the_final_tool_call_ends_the_run_with_its_input() {
    let provider = scripted([
        tool_turn(0, 1, "Read"),
        call("f1", "invoice", json!({"total": 4})),
        text_turn("thanks"),
    ]);
    let (stores, runs) = (Arc::new(Stores::default()), Runs::default());
    let agent = final_agent(
        shaped(0),
        provider.clone(),
        &runs,
        Hooks::default(),
        stores.clone(),
    );
    let envelopes = finish(agent.run("bill me", RunOptions::new("s1"))).await;
    let result = result(&envelopes);
    assert_eq!(result.subtype, ResultSubtype::Success);
    assert_eq!(result.structured_output, Some(total(4)));
    assert_eq!(result.final_text.as_deref(), Some("{\"total\":4}"));
    assert_eq!(tool_events(&envelopes), 2, "only Read ran");
    assert!(verifications(&envelopes).is_empty());
    let Event::Init(init) = &envelopes[0].event else {
        panic!("a run starts with init");
    };
    assert_eq!(init.final_tool.as_deref(), Some("invoice"));
    assert_eq!(
        init.tools,
        ["Read", "invoice"],
        "init lists what is offered"
    );
    for request in provider.requests() {
        assert_eq!(request.output_schema, None, "the provider cannot take it");
        let names: Vec<_> = request.tools.iter().map(|t| t.name.as_str()).collect();
        assert_eq!(names, ["Read", "invoice"], "the final tool goes last");
        assert_eq!(request.tool_choice, None, "not the only tool");
        assert_eq!(request.tools[1].input_schema, schema().schema);
    }
    let log = messages(&*stores, "s1").await;
    assert_paired(&log, "after the answer");
    let ack = results(log.last().unwrap());
    let taken = (
        "f1".into(),
        "Final answer captured by the host.".into(),
        false,
    );
    assert_eq!(ack, [taken]);
    // The session takes another run.
    let envelopes = finish(agent.run("and now?", RunOptions::new("s1"))).await;
    let next = self::result(&envelopes);
    assert_eq!(
        next.subtype,
        ResultSubtype::Error,
        "a text answer is checked"
    );
    assert_eq!(runs.lock().unwrap().len(), 1);
}

#[tokio::test]
async fn the_only_tool_is_forced_and_a_text_answer_still_counts() {
    let provider = scripted([text_turn("{\"total\": 5}")]);
    let stores = Arc::new(Stores::default());
    let agent = (builder(
        shaped(0),
        provider.clone(),
        vec![],
        Hooks::default(),
        stores,
    )
    .build())
    .unwrap();
    let envelopes = finish(agent.run("bill me", RunOptions::new("s1"))).await;
    assert_eq!(result(&envelopes).structured_output, Some(total(5)));
    let request = &provider.requests()[0];
    let forced = ToolChoice::Tool {
        name: "invoice".into(),
    };
    assert_eq!(request.tool_choice, Some(forced));
}

#[tokio::test]
async fn a_bad_input_goes_back_as_the_calls_error_and_the_next_one_counts() {
    let provider = scripted([
        call("f1", "invoice", json!({"sum": 1})),
        call("f2", "invoice", json!({"total": 2})),
    ]);
    let stores = Arc::new(Stores::default());
    let runs = Runs::default();
    let agent = final_agent(shaped(1), provider, &runs, Hooks::default(), stores.clone());
    let envelopes = finish(agent.run("bill me", RunOptions::new("s1"))).await;
    assert_eq!(result(&envelopes).structured_output, Some(total(2)));
    assert_eq!(verifications(&envelopes), [(VerificationAction::Retry, 1)]);
    let log = messages(&*stores, "s1").await;
    assert_paired(&log, "after a retry");
    let (id, text, error) = &results(&log[2])[0];
    assert_eq!((id.as_str(), *error), ("f1", true));
    assert!(text.starts_with("Your previous response failed"), "{text}");
    assert!(text.contains("Call the invoice tool again"), "{text}");
}

#[tokio::test]
async fn past_the_retries_the_run_fails_and_the_session_stays_paired() {
    let provider = scripted([call("f1", "invoice", json!({"sum": 1})), text_turn("fine")]);
    let stores = Arc::new(Stores::default());
    let runs = Runs::default();
    let agent = final_agent(shaped(0), provider, &runs, Hooks::default(), stores.clone());
    let envelopes = finish(agent.run("bill me", RunOptions::new("s1"))).await;
    let result = result(&envelopes);
    assert_eq!(result.subtype, ResultSubtype::Error);
    assert!(result.structured_error.is_some());
    assert_eq!(result.final_text.as_deref(), Some("{\"sum\":1}"));
    assert_eq!(verifications(&envelopes), [(VerificationAction::Stop, 0)]);
    let log = messages(&*stores, "s1").await;
    assert_paired(&log, "after the failure");
    assert!(results(log.last().unwrap())[0].2, "an error result");
    let mut spec = shaped(0);
    spec.output_schema = None;
    let plain = (builder(
        spec,
        scripted([text_turn("ok")]),
        vec![],
        Hooks::default(),
        stores,
    )
    .build())
    .unwrap();
    let envelopes = finish(plain.run("hello", RunOptions::new("s1"))).await;
    assert_eq!(self::result(&envelopes).subtype, ResultSubtype::Success);
}

#[tokio::test]
async fn a_final_call_beside_another_call_runs_neither() {
    let provider = scripted([
        calls(&[
            ("r1", "Read", json!({"value": "x"})),
            ("f1", "invoice", json!({"total": 1})),
        ]),
        call("f2", "invoice", json!({"total": 3})),
    ]);
    let stores = Arc::new(Stores::default());
    let runs = Runs::default();
    let agent = final_agent(shaped(1), provider, &runs, Hooks::default(), stores.clone());
    let envelopes = finish(agent.run("bill me", RunOptions::new("s1"))).await;
    assert_eq!(result(&envelopes).structured_output, Some(total(3)));
    assert_eq!(tool_events(&envelopes), 0);
    assert!(runs.lock().unwrap().is_empty(), "Read never ran");
    let log = messages(&*stores, "s1").await;
    assert_paired(&log, "after a mixed batch");
    let paired = results(&log[2]);
    assert!(paired[0].1.starts_with("Tool not executed"), "{paired:?}");
    assert!(
        paired[1].1.contains("must be the only tool call"),
        "{paired:?}"
    );
}

#[tokio::test]
async fn a_named_final_tool_ends_the_run_without_running() {
    let provider = Arc::new(ScriptedProvider::new([call(
        "f1",
        "Read",
        json!({"value": "done"}),
    )]));
    let mut spec = spec();
    spec.final_tool_name = Some("Read".into());
    let stores = Arc::new(Stores::default());
    let runs = Runs::default();
    let agent = final_agent(spec, provider.clone(), &runs, Hooks::default(), stores);
    let envelopes = finish(agent.run("go", RunOptions::new("s1"))).await;
    let result = result(&envelopes);
    assert_eq!(
        result.structured_output,
        Some(object(json!({"value": "done"})))
    );
    assert!(runs.lock().unwrap().is_empty());
    let request = &provider.requests()[0];
    assert_eq!(request.tools.len(), 1, "nothing is made");
    let Event::Init(init) = &envelopes[0].event else {
        panic!("a run starts with init");
    };
    assert_eq!(
        init.tools,
        ["Read"],
        "a registered final tool is listed once"
    );
}

#[tokio::test]
async fn a_stop_hook_sees_the_input_and_its_retry_pairs_the_call() {
    let provider = scripted([
        call("f1", "invoice", json!({"total": 1})),
        call("f2", "invoice", json!({"total": 2})),
    ]);
    let seen = Arc::new(Mutex::new(Vec::new()));
    let hooks = Hooks::default();
    let judged = Arc::clone(&seen);
    let judge = hook::<Stop>("judge", move |request| {
        let mut seen = judged.lock().unwrap();
        seen.push(request.structured_output.clone());
        Ok(match seen.len() {
            1 => StopResponse::Retry {
                feedback: "bigger".into(),
            },
            _ => StopResponse::Allow,
        })
    });
    let _ = hooks.stop.register(judge.arc()).unwrap();
    let stores = Arc::new(Stores::default());
    let agent = final_agent(shaped(0), provider, &Runs::default(), hooks, stores.clone());
    let envelopes = finish(agent.run("bill me", RunOptions::new("s1"))).await;
    assert_eq!(result(&envelopes).structured_output, Some(total(2)));
    assert_eq!(*seen.lock().unwrap(), [Some(total(1)), Some(total(2))]);
    let log = messages(&*stores, "s1").await;
    assert_paired(&log, "after a hook retry");
    assert_eq!(results(&log[2])[0].1, "bigger");
}

#[tokio::test]
async fn a_hook_that_ends_the_run_first_still_pairs_the_call() {
    let provider = scripted([call("f1", "invoice", json!({"total": 1}))]);
    let hooks = Hooks::default();
    let down = hook::<Stop>("judge", |_| Err("judge down".into())).fail_mode(FailMode::Closed);
    let _ = hooks.stop.register(down.arc()).unwrap();
    let stores = Arc::new(Stores::default());
    let agent = final_agent(shaped(0), provider, &Runs::default(), hooks, stores.clone());
    let envelopes = finish(agent.run("bill me", RunOptions::new("s1"))).await;
    assert_eq!(result(&envelopes).subtype, ResultSubtype::Error);
    let log = messages(&*stores, "s1").await;
    assert_paired(&log, "after a hook failure");
    let (_, text, error) = &results(log.last().unwrap())[0];
    assert!(*error, "not taken");
    assert!(text.starts_with("Final answer not taken"), "{text}");
}

/// The turns of one crash scenario, made afresh for each run.
type Script = fn() -> Vec<ScriptedTurn>;

/// A named tool choice is sent only on a turn whose request offers that
/// tool: a skill that narrows the turn to other tools would otherwise make
/// every provider call of the turn a 400. The turn goes out as `auto`, and
/// the `context_build` event of that turn says what was dropped.
#[tokio::test]
async fn a_named_choice_the_turn_does_not_offer_falls_back_to_auto() {
    let mut review = linch_core::Skill::new("review", "/skills/review", "Review.", "go");
    review.allowed_tools = Some(vec!["Read".into()]);
    let turns = [
        call("k1", "Skill", json!({"skill": "review", "args": ""})),
        call("r1", "Read", json!({"value": "x"})),
        text_turn("done"),
    ];
    let provider = Arc::new(ScriptedProvider::new(turns));
    let stores = Arc::new(Stores::default());
    let runs = Runs::default();
    let tools = vec![
        Echo::new("Read", ToolScope::Read, &runs),
        Echo::new("Write", ToolScope::Write, &runs),
    ];
    let mut spec = spec();
    spec.tool_choice = Some(ToolChoice::Tool {
        name: "Write".into(),
    });
    let agent = (builder(spec, provider.clone(), tools, Hooks::default(), stores)
        .skills(vec![review])
        .build())
    .unwrap();
    let envelopes = finish(agent.run("go", RunOptions::new("s1"))).await;
    let sent: Vec<_> = (provider.requests().into_iter())
        .map(|request| {
            let names: Vec<_> = request.tools.iter().map(|t| t.name.clone()).collect();
            (names, request.tool_choice)
        })
        .collect();
    let write = Some(ToolChoice::Tool {
        name: "Write".into(),
    });
    assert_eq!(sent[0].1, write, "the first turn offers Write");
    assert!(!sent[1].0.contains(&"Write".to_owned()), "{sent:?}");
    assert_eq!(sent[1].1, Some(ToolChoice::Auto {}), "{sent:?}");
    assert_eq!(sent[2].1, write, "the skill's turn is over");
    let dropped: Vec<_> = (envelopes.iter())
        .filter_map(|envelope| match &envelope.event {
            Event::ContextBuild(build) => Some(build.metadata.get("tool_choice_dropped").cloned()),
            _ => None,
        })
        .collect();
    assert_eq!(dropped, [None, Some(json!("Write")), None]);
}

/// `required` needs a tool to call: a `before_provider_call` hook that
/// leaves the request with none would make every provider answer 400, so
/// the turn goes out with no choice, and its `context_build` event says
/// `required` was dropped.
#[tokio::test]
async fn a_required_choice_on_a_request_with_no_tool_is_dropped() {
    let hooks = Hooks::default();
    let strip = hook::<linch_core::hooks::BeforeProviderCall>("strip", |request| {
        let mut request = request.request.clone();
        request.tools.clear();
        Ok(linch_core::hooks::BeforeProviderCallResponse::Rewrite {
            request: Box::new(request),
        })
    });
    let _ = hooks.before_provider_call.register(strip.arc()).unwrap();
    let provider = Arc::new(ScriptedProvider::new([text_turn("done")]));
    let mut spec = spec();
    spec.tool_choice = Some(ToolChoice::Required {});
    let stores = Arc::new(Stores::default());
    let agent = final_agent(spec, provider.clone(), &Runs::default(), hooks, stores);
    let envelopes = finish(agent.run("go", RunOptions::new("s1"))).await;
    let sent = provider.requests();
    assert!(sent[0].tools.is_empty(), "the hook took the tools off");
    assert_eq!(sent[0].tool_choice, None, "{:?}", sent[0].tool_choice);
    let dropped: Vec<_> = (envelopes.iter())
        .filter_map(|envelope| match &envelope.event {
            Event::ContextBuild(build) => Some(build.metadata.get("tool_choice_dropped").cloned()),
            _ => None,
        })
        .collect();
    assert_eq!(dropped, [Some(json!("required"))]);
}

/// A `pre_tool_use` hook that blocks one `total`.
fn veto_total(hooks: &Hooks, total: u64) {
    let veto = hook::<PreToolUse>("veto", move |request| {
        Ok(match request.input.get("total") == Some(&json!(total)) {
            true => PreToolUseResponse::Block {
                reason: "not that one".into(),
            },
            false => PreToolUseResponse::Continue,
        })
    });
    let _ = hooks.pre_tool_use.register(veto.arc()).unwrap();
}

/// The final call is decided as any call is: a `pre_tool_use` veto goes
/// back as its result, the model calls again, and the hook's record shows.
#[tokio::test]
async fn a_pre_tool_use_veto_goes_back_as_the_final_calls_result() {
    let provider = scripted([
        call("f1", "invoice", json!({"total": 7})),
        call("f2", "invoice", json!({"total": 2})),
    ]);
    let hooks = Hooks::default();
    veto_total(&hooks, 7);
    let stores = Arc::new(Stores::default());
    let agent = final_agent(shaped(1), provider, &Runs::default(), hooks, stores.clone());
    let envelopes = finish(agent.run("bill me", RunOptions::new("s1"))).await;
    assert_eq!(result(&envelopes).structured_output, Some(total(2)));
    let log = messages(&*stores, "s1").await;
    assert_paired(&log, "after a veto");
    let (_, text, error) = &results(&log[2])[0];
    assert!(*error, "the veto is an error result");
    assert!(text.contains("not that one"), "{text}");
    let hook_events = (envelopes.iter())
        .filter(|envelope| matches!(envelope.event, Event::Hook(_)))
        .count();
    assert_eq!(hook_events, 2, "the hook was asked of both calls");
    assert_eq!(tool_events(&envelopes), 0, "the call still never runs");
}

/// A hook that rewrites the final call's input changes the answer: the
/// result holds the rewritten object.
#[tokio::test]
async fn a_pre_tool_use_rewrite_is_the_answer() {
    let provider = scripted([call("f1", "invoice", json!({"total": 1}))]);
    let hooks = Hooks::default();
    let rewrite = hook::<PreToolUse>("rewrite", |_| {
        Ok(PreToolUseResponse::Rewrite { input: total(9) })
    });
    let _ = hooks.pre_tool_use.register(rewrite.arc()).unwrap();
    let agent = final_agent(
        shaped(0),
        provider,
        &Runs::default(),
        hooks,
        Arc::new(Stores::default()),
    );
    let envelopes = finish(agent.run("bill me", RunOptions::new("s1"))).await;
    let result = result(&envelopes);
    assert_eq!(result.structured_output, Some(total(9)));
    assert_eq!(result.final_text.as_deref(), Some(r#"{"total":9}"#));
}

/// A deny rule on the final tool, or a veto that never lifts, cannot keep a
/// run going: they draw on `structured_output_retries`, so under a forced
/// choice with no `max_turns` the run still ends failed, session paired.
#[tokio::test]
async fn a_refusal_that_never_lifts_ends_the_run_failed() {
    let deny = || {
        let mut strict = linch_core::PermissionPolicy::new(PermissionMode::SkipDangerous, "/");
        let rule = linch_core::permissions::ToolRule::new(
            "invoice",
            linch_core::permissions::RuleDecision::Deny,
        );
        let layer = [linch_core::permissions::Rule::Tool(rule)];
        strict.layers = vec![linch_core::PermissionLayer::new("runtime", layer)];
        linch_core::PermissionEngine::new(strict).unwrap()
    };
    for denied in [true, false] {
        let turns: Vec<_> = (1..=6)
            .map(|n| call(&format!("f{n}"), "invoice", json!({"total": 7})))
            .collect();
        let provider = scripted(turns);
        let hooks = Hooks::default();
        if !denied {
            veto_total(&hooks, 7);
        }
        let stores = Arc::new(Stores::default());
        let mut spec = shaped(2);
        spec.tool_choice = Some(ToolChoice::Required {});
        assert_eq!(spec.max_turns, None);
        let mut builder = builder(spec, provider.clone(), vec![], hooks, stores.clone());
        if denied {
            builder = builder.permissions(deny());
        }
        let agent = builder.build().unwrap();
        let envelopes = finish(agent.run("bill me", RunOptions::new("s1"))).await;
        let context = format!("denied: {denied}");
        assert_eq!(
            result(&envelopes).subtype,
            ResultSubtype::Error,
            "{context}"
        );
        assert_eq!(
            provider.requests().len(),
            3,
            "{context}: 2 retries, then it ends"
        );
        let log = messages(&*stores, "s1").await;
        assert_paired(&log, &context);
        let (_, text, _) = &results(&log[2])[0];
        let reason = if denied { "denied" } else { "not that one" };
        assert!(text.contains(reason), "{context}: {text}");
    }
}

/// A named final tool's own input schema and `Tool::validate` are checked,
/// with no output schema to do it: the invalid input goes back as the
/// call's error, and the valid one is the answer.
#[tokio::test]
async fn a_named_final_tools_input_is_checked_with_the_tool() {
    let provider = Arc::new(ScriptedProvider::new([
        call("f1", "Read", json!({"value": 3})),
        call("f2", "Read", json!({"value": "ok"})),
    ]));
    let mut spec = spec();
    spec.final_tool_name = Some("Read".into());
    spec.structured_output_retries = 1;
    let stores = Arc::new(Stores::default());
    let agent = final_agent(
        spec,
        provider,
        &Runs::default(),
        Hooks::default(),
        stores.clone(),
    );
    let envelopes = finish(agent.run("go", RunOptions::new("s1"))).await;
    let got = result(&envelopes);
    assert_eq!(got.structured_output, Some(object(json!({"value": "ok"}))));
    let log = messages(&*stores, "s1").await;
    assert_paired(&log, "after an invalid input");
    let (_, text, error) = &results(&log[2])[0];
    assert!(*error && text.contains("value"), "{text}");
}

/// A `stop` hook's rewrite of a final tool's answer is parsed and checked
/// again, so `structured_output` is the object `final_text` holds; one that
/// is not an object the tool accepts ends the run failed.
#[tokio::test]
async fn a_stop_rewrite_of_a_final_answer_is_checked_again() {
    for (rewritten, want) in [
        (r#"{"value": "changed"}"#, Some(json!({"value": "changed"}))),
        (r#"{"value": 4}"#, None),
        ("not json", None),
    ] {
        let provider = Arc::new(ScriptedProvider::new([call(
            "f1",
            "Read",
            json!({"value": "first"}),
        )]));
        let mut spec = spec();
        spec.final_tool_name = Some("Read".into());
        let hooks = Hooks::default();
        let rewrite = hook::<Stop>("rewrite", move |_| {
            Ok(StopResponse::Rewrite {
                text: rewritten.into(),
            })
        });
        let _ = hooks.stop.register(rewrite.arc()).unwrap();
        let stores = Arc::new(Stores::default());
        let agent = final_agent(spec, provider, &Runs::default(), hooks, stores.clone());
        let envelopes = finish(agent.run("go", RunOptions::new("s1"))).await;
        let got = result(&envelopes);
        let context = format!("rewritten to {rewritten}");
        match want {
            Some(want) => {
                assert_eq!(got.subtype, ResultSubtype::Success, "{context}");
                assert_eq!(got.final_text.as_deref(), Some(rewritten), "{context}");
                assert_eq!(got.structured_output, Some(object(want)), "{context}");
            }
            None => {
                assert_eq!(got.subtype, ResultSubtype::Error, "{context}");
                assert!(got.structured_error.is_some(), "{context}");
            }
        }
        assert_paired(&messages(&*stores, "s1").await, &context);
    }
}

/// A crash at any write of a final tool run ends where the clean run
/// ended: the same session, paired, the same `verification` events, no
/// paid response bought again; and the session takes another run.
#[tokio::test]
async fn a_crash_at_any_write_ends_where_the_clean_run_did() {
    let scripts: [(&str, Script, RunStatus); 4] = [
        (
            "good",
            || vec![call("f1", "invoice", json!({"total": 1}))],
            RunStatus::Completed,
        ),
        (
            "bad, good",
            || {
                vec![
                    call("f1", "invoice", json!({"sum": 1})),
                    call("f2", "invoice", json!({"total": 1})),
                ]
            },
            RunStatus::Completed,
        ),
        (
            "bad, bad",
            || {
                vec![
                    call("f1", "invoice", json!({"sum": 1})),
                    call("f2", "invoice", json!({"sum": 2})),
                    call("f3", "invoice", json!({"total": 1})),
                ]
            },
            RunStatus::Failed,
        ),
        (
            "vetoed, good",
            || {
                vec![
                    call("f1", "invoice", json!({"total": 7})),
                    call("f2", "invoice", json!({"total": 1})),
                ]
            },
            RunStatus::Completed,
        ),
    ];
    // A hook that vetoes the one input the "vetoed" script starts with.
    let vetoing = || {
        let hooks = Hooks::default();
        let veto = hook::<PreToolUse>("veto-seven", |request| {
            Ok(match request.input.get("total") == Some(&json!(7)) {
                true => PreToolUseResponse::Block {
                    reason: "seven is unlucky".into(),
                },
                false => PreToolUseResponse::Continue,
            })
        });
        let _ = hooks.pre_tool_use.register(veto.arc()).unwrap();
        hooks
    };
    let make =
        |turns: Vec<ScriptedTurn>| Arc::new(Replay::new(turns).without_native_output_schema());
    for (name, turns, status) in scripts {
        let clean = Arc::new(Stores::default());
        let runs = Runs::default();
        let agent = final_agent(shaped(1), make(turns()), &runs, vetoing(), clean.clone());
        let stream = agent.run("bill me", RunOptions::new("s1"));
        let clean_id = stream.run_id().clone();
        finish(stream).await;
        let want = messages(&*clean, "s1").await;
        assert_paired(&want, name);
        let clean_events = clean.runs.events(NS, &clean_id, 0).await.unwrap();
        let want_verifications = verifications(&clean_events);
        let want_result = result(&clean_events);
        let writes = clean.log.lock().unwrap().len() as u64;
        for n in 1..=writes {
            for timing in [FaultTiming::Before, FaultTiming::After] {
                let context = format!("{name}: write {n} failing {timing:?}");
                let stores = Arc::new(Stores::default());
                let provider = make(turns());
                let faulty = Arc::new(Faulty::new(stores.clone(), n, timing));
                let crashing = final_agent(shaped(1), provider.clone(), &runs, vetoing(), faulty);
                let stream = crashing.run("bill me", RunOptions::new("s1"));
                let run_id = stream.run_id().clone();
                assert!(drain(stream).await.1.is_some(), "{context}");
                let agent = final_agent(
                    shaped(1),
                    provider.clone(),
                    &runs,
                    vetoing(),
                    stores.clone(),
                );
                let run_id = match stores.runs.load(NS, &run_id).await {
                    Ok(record) if record.status.is_terminal() => run_id,
                    Ok(_) => {
                        finish(agent.resume(run_id.clone(), RunOptions::new("s1"))).await;
                        run_id
                    }
                    Err(_) => {
                        let stream = agent.run("bill me", RunOptions::new("s1"));
                        let run_id = stream.run_id().clone();
                        finish(stream).await;
                        run_id
                    }
                };
                let record = stores.runs.load(NS, &run_id).await.unwrap();
                assert_eq!(record.status, status, "{context}");
                let log = messages(&*stores, "s1").await;
                assert_eq!(log, want, "{context}");
                let events = stores.runs.events(NS, &run_id, 0).await.unwrap();
                assert_eq!(verifications(&events), want_verifications, "{context}");
                for envelope in &events {
                    if let Event::Init(init) = &envelope.event {
                        let tools = ["Read", "invoice"];
                        assert_eq!(init.tools, tools, "{context}: init, resumed or not");
                    }
                }
                let got = result(&events);
                assert_eq!(got.structured_output, want_result.structured_output);
                assert_eq!(got.structured_error, want_result.structured_error);
                let calls = provider.calls();
                assert!(calls.values().all(|&n| n <= 2), "{context}: {calls:?}");
                // The session takes another run.
                let mut spec = shaped(1);
                spec.output_schema = None;
                let next = (builder(
                    spec,
                    Arc::new(ScriptedProvider::new([text_turn("ok")])),
                    vec![],
                    Hooks::default(),
                    stores.clone(),
                )
                .build())
                .unwrap();
                let envelopes = finish(next.run("hello", RunOptions::new("s1"))).await;
                let next = self::result(&envelopes);
                assert_eq!(next.subtype, ResultSubtype::Success, "{context}");
            }
        }
    }
}
