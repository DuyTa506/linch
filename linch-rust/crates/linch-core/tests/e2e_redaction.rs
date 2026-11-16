//! Redaction: what the model writes and what tools return is scrubbed before
//! it is logged, streamed, or shown to a hook, so the raw text never reaches
//! a store, and a crash at any write keeps it that way.

mod common;

use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use common::hooks::hook;
use common::*;
use linch_core::events::Envelope;
use linch_core::hooks::{
    AfterProviderCall, AfterProviderCallResponse, PostToolUse, PostToolUseResponse, Stop,
    StopResponse,
};
use linch_core::provider::scripted::TurnBuilder;
use linch_core::redaction::{RedactionRule, RedactionSpec};
use linch_core::store::faulty::{FaultTiming, Faulty};
use linch_core::verification::VerificationSpec;
use linch_core::{
    Agent, AgentBuilder, AgentSpec, BuildError, Commit, ContentBlock, Event, Hooks, JsonObject,
    Lease, LogEntry, Message, NewRun, PermissionMode, Provider, RunId, RunOptions, RunRecord,
    RunStatus, RunStore, ScriptedProvider, ScriptedTurn, SessionId, SessionStore, StoreError, Tool,
    ToolContext, ToolOutput, ToolRegistry, ToolResult, ToolScope,
};
use linch_core::{InMemoryMailbox, Mailbox, MailboxMessage};
use serde_json::json;

const SECRET: &str = "sk-live0123456789";
const MASK: &str = "[KEY]";
const INTERRUPTED: &str = "Tool execution was interrupted before a result was recorded; not \
                           re-running automatically on resume.";

fn redaction() -> RedactionSpec {
    RedactionSpec {
        rules: vec![RedactionRule {
            pattern: r"sk-[A-Za-z0-9]{8,}".into(),
            replacement: MASK.into(),
        }],
        response: true,
        tool_results: true,
        prompt: false,
    }
}

fn redacting() -> AgentSpec {
    let mut spec = spec();
    spec.redaction = Some(redaction());
    spec
}

/// A read tool whose result, metadata and progress hold the secret,
/// though its input does not.
struct Leak;

#[async_trait]
impl Tool for Leak {
    fn name(&self) -> &str {
        "Leak"
    }
    fn description(&self) -> &str {
        "Find the key."
    }
    fn input_schema(&self) -> &JsonObject {
        static SCHEMA: std::sync::OnceLock<JsonObject> = std::sync::OnceLock::new();
        SCHEMA.get_or_init(|| object(json!({"type": "object"})))
    }
    fn scope(&self) -> ToolScope {
        ToolScope::Read
    }
    async fn execute(&self, _input: JsonObject, ctx: ToolContext) -> ToolResult {
        ctx.report_progress(format!("found {SECRET}"), None);
        let mut output = ToolOutput::new(json!({"text": format!("the key is {SECRET}")}));
        output.metadata.insert("raw".into(), json!(SECRET));
        Ok(output)
    }
}

fn tools() -> Arc<ToolRegistry> {
    let registry = ToolRegistry::new();
    let _ = registry.register(Arc::new(Leak)).unwrap();
    Arc::new(registry)
}

/// The model says the secret, calls `Leak`, then answers with it.
fn turns() -> Vec<ScriptedTurn> {
    vec![
        TurnBuilder::new(MODEL)
            .text(format!("looking for {SECRET}"))
            .tool_call("c1", "Leak", &json!({}))
            .usage(usage(10, 5))
            .build(),
        text_turn(&format!("the key is {SECRET}")),
    ]
}

fn build<S: RunStore + SessionStore + 'static>(
    spec: AgentSpec,
    provider: Arc<dyn Provider>,
    hooks: Hooks,
    stores: Arc<S>,
) -> Agent {
    AgentBuilder::new(spec)
        .provider(provider)
        .tools(tools())
        .permissions(policy(PermissionMode::SkipDangerous))
        .hooks(Arc::new(hooks))
        .run_store(stores.clone())
        .session_store(stores)
        .namespace(NS)
        .build()
        .unwrap()
}

fn leaks(value: impl serde::Serialize) -> bool {
    serde_json::to_string(&value).unwrap().contains(SECRET)
}

fn final_text(envelopes: &[Envelope]) -> Option<String> {
    match envelopes.last().map(|e| &e.event) {
        Some(Event::Result(result)) => result.final_text.clone(),
        other => panic!("no result: {other:?}"),
    }
}

/// The logged session, the model's requests, every event the caller saw,
/// and the result hold the masked text only; hooks see it masked too.
#[tokio::test]
async fn the_raw_text_reaches_no_log_request_event_or_hook() {
    let hooks = Hooks::default();
    let seen = Arc::new(Mutex::new(Vec::new()));
    let saw = Arc::clone(&seen);
    let after = hook::<AfterProviderCall>("after", move |request| {
        saw.lock().unwrap().push(request.assembly.message.text());
        Ok(AfterProviderCallResponse::Continue)
    });
    let _ = hooks.after_provider_call.register(after.arc()).unwrap();
    let saw = Arc::clone(&seen);
    let post = hook::<PostToolUse>("post", move |request| {
        saw.lock().unwrap().push(json!(request.result).to_string());
        Ok(PostToolUseResponse::Continue)
    });
    let _ = hooks.post_tool_use.register(post.arc()).unwrap();
    let stores = Arc::new(Stores::default());
    let provider = Arc::new(ScriptedProvider::new(turns()));
    let agent = build(redacting(), provider.clone(), hooks, stores.clone());
    let envelopes = finish(agent.run("go", RunOptions::new("s1"))).await;

    let log = messages(&*stores, "s1").await;
    assert_eq!(log.len(), 4, "{log:?}");
    assert_eq!(log[1].text(), format!("looking for {MASK}"));
    assert_eq!(
        result_texts(&log[2]),
        [json!({"text": format!("the key is {MASK}")}).to_string()]
    );
    assert_eq!(
        log[3],
        Message::assistant_text(format!("the key is {MASK}"))
    );
    assert!(!leaks(&log), "{log:?}");
    assert!(!leaks(provider.requests()), "the model never reads it back");
    assert!(!leaks(&envelopes), "{envelopes:#?}");
    let kinds = kinds(&envelopes);
    assert!(
        !kinds.contains(&"partial_assistant".to_owned()),
        "{kinds:?}"
    );
    assert!(!kinds.contains(&"tool_progress".to_owned()), "{kinds:?}");
    assert_eq!(final_text(&envelopes), Some(format!("the key is {MASK}")));
    let seen = seen.lock().unwrap();
    assert_eq!(seen.len(), 3, "{seen:?}");
    assert!(!seen.iter().any(|text| text.contains(SECRET)), "{seen:?}");
}

/// A hook's rewrite cannot bring the raw text back: an
/// `after_provider_call` rewrite and a `stop` rewrite are masked again.
#[tokio::test]
async fn a_hook_rewrite_is_masked_again() {
    let hooks = Hooks::default();
    let after = hook::<AfterProviderCall>("after", |request| {
        let mut assembly = request.assembly.clone();
        assembly.message.content.push(ContentBlock::text(SECRET));
        Ok(AfterProviderCallResponse::Rewrite {
            assembly: Box::new(assembly),
        })
    });
    let _ = hooks.after_provider_call.register(after.arc()).unwrap();
    let stop = hook::<Stop>("stop", |_| {
        Ok(StopResponse::Rewrite {
            text: format!("final {SECRET}"),
        })
    });
    let _ = hooks.stop.register(stop.arc()).unwrap();
    let stores = Arc::new(Stores::default());
    let provider = Arc::new(ScriptedProvider::new(vec![text_turn("hi")]));
    let agent = build(redacting(), provider, hooks, stores.clone());
    let envelopes = finish(agent.run("go", RunOptions::new("s1"))).await;

    let log = messages(&*stores, "s1").await;
    assert_eq!(log[1].content[1], ContentBlock::text(MASK));
    assert_eq!(final_text(&envelopes), Some(format!("final {MASK}")));
    assert!(!leaks(&envelopes));
}

/// The prompt is the host's input: it is kept unless `prompt` is on.
#[tokio::test]
async fn the_prompt_is_masked_only_when_asked() {
    for prompt in [false, true] {
        let mut spec = redacting();
        if let Some(redaction) = &mut spec.redaction {
            redaction.prompt = prompt;
        }
        let stores = Arc::new(Stores::default());
        let provider = Arc::new(ScriptedProvider::new(vec![text_turn("ok")]));
        let agent = build(spec, provider, Hooks::default(), stores.clone());
        finish(agent.run(format!("use {SECRET}"), RunOptions::new("s1"))).await;
        let want = if prompt { MASK } else { SECRET };
        let log = messages(&*stores, "s1").await;
        assert_eq!(
            log[0],
            Message::user_text(format!("use {want}")),
            "{prompt}"
        );
    }
}

/// Only the surfaces turned on are masked.
#[tokio::test]
async fn each_surface_has_its_own_switch() {
    let mut spec = redacting();
    if let Some(redaction) = &mut spec.redaction {
        redaction.tool_results = false;
    }
    let stores = Arc::new(Stores::default());
    let provider = Arc::new(ScriptedProvider::new(turns()));
    let agent = build(spec, provider, Hooks::default(), stores.clone());
    let envelopes = finish(agent.run("go", RunOptions::new("s1"))).await;
    let log = messages(&*stores, "s1").await;
    assert_eq!(log[1].text(), format!("looking for {MASK}"));
    assert!(leaks(&log[2]), "tool results are kept");
    assert!(kinds(&envelopes).contains(&"tool_progress".to_owned()));
    assert!(!kinds(&envelopes).contains(&"partial_assistant".to_owned()));
}

/// A rule that cannot run is refused when the agent is built.
#[test]
fn a_bad_rule_is_refused_at_build() {
    let mut spec = spec();
    spec.redaction = Some(RedactionSpec {
        rules: vec![RedactionRule::new("(unclosed")],
        response: true,
        tool_results: true,
        prompt: false,
    });
    let built = AgentBuilder::new(spec)
        .provider(Arc::new(ScriptedProvider::new(vec![])))
        .permissions(policy(PermissionMode::Default))
        .run_store(Arc::new(Stores::default()))
        .session_store(Arc::new(Stores::default()))
        .namespace(NS)
        .build();
    assert!(
        matches!(built, Err(BuildError::Invalid(ref why)) if why.contains("redaction rule 0")),
        "{:?}",
        built.err()
    );
}

/// Every write the stores take, serialized, so a test can search them.
#[derive(Default)]
struct Recorded {
    stores: Stores,
    writes: Mutex<Vec<String>>,
}

impl Recorded {
    fn record(&self, value: impl std::fmt::Debug) {
        let text = format!("{value:?}");
        self.writes.lock().unwrap().push(text);
    }

    fn leaked(&self) -> bool {
        self.writes
            .lock()
            .unwrap()
            .iter()
            .any(|w| w.contains(SECRET))
    }
}

#[async_trait]
impl RunStore for Recorded {
    async fn create(&self, run: NewRun) -> Result<RunRecord, StoreError> {
        self.record(&run.checkpoint);
        RunStore::create(&self.stores, run).await
    }
    async fn claim(&self, ns: &str, run_id: &RunId, owner: &str) -> Result<Lease, StoreError> {
        self.stores.claim(ns, run_id, owner).await
    }
    async fn load(&self, ns: &str, run_id: &RunId) -> Result<RunRecord, StoreError> {
        RunStore::load(&self.stores, ns, run_id).await
    }
    async fn commit(&self, lease: &Lease, commit: Commit) -> Result<(), StoreError> {
        self.record(&commit.events);
        self.record(&commit.checkpoint);
        self.stores.commit(lease, commit).await
    }
    async fn events(
        &self,
        ns: &str,
        run_id: &RunId,
        after: u64,
    ) -> Result<Vec<Envelope>, StoreError> {
        self.stores.events(ns, run_id, after).await
    }
}

#[async_trait]
impl SessionStore for Recorded {
    async fn create(&self, ns: &str, session: &SessionId) -> Result<(), StoreError> {
        SessionStore::create(&self.stores, ns, session).await
    }
    async fn load(&self, ns: &str, session: &SessionId) -> Result<Vec<LogEntry>, StoreError> {
        SessionStore::load(&self.stores, ns, session).await
    }
    async fn append(
        &self,
        ns: &str,
        session: &SessionId,
        expected: u64,
        entries: Vec<LogEntry>,
    ) -> Result<u64, StoreError> {
        self.record(&entries);
        self.stores.append(ns, session, expected, entries).await
    }
}

/// A crash at any write, before or after it lands, then a resume (or a new
/// run when the first never started): no write ever carries the raw text,
/// and the session ends as a clean run leaves it, but for a tool the crash
/// interrupted. With an `after_provider_call` hook attached the paid
/// receipt is written before the hook runs, so that path is crashed too.
#[tokio::test]
async fn a_crash_at_any_write_never_stores_the_raw_text() {
    for hooks in [Hooks::default as fn() -> Hooks, passing] {
        crash_everywhere(hooks).await;
    }
}

/// A pass-through `after_provider_call` hook.
fn passing() -> Hooks {
    let hooks = Hooks::default();
    let pass = hook::<AfterProviderCall>("pass", |_| Ok(AfterProviderCallResponse::Continue));
    let _ = hooks.after_provider_call.register(pass.arc()).unwrap();
    hooks
}

async fn crash_everywhere(hooks: fn() -> Hooks) {
    let replay = || Arc::new(Replay::new(turns()));
    let clean = Arc::new(Recorded::default());
    let agent = build(redacting(), replay(), hooks(), clean.clone());
    finish(agent.run("go", RunOptions::new("s1"))).await;
    assert!(!clean.leaked());
    let want = messages(&clean.stores, "s1").await;
    let writes = clean.stores.log.lock().unwrap().len() as u64;
    for n in 1..=writes {
        for timing in [FaultTiming::Before, FaultTiming::After] {
            let context = format!("write {n} failing {timing:?}");
            let stores = Arc::new(Recorded::default());
            let faulty = Arc::new(Faulty::new(stores.clone(), n, timing));
            let stream =
                build(redacting(), replay(), hooks(), faulty).run("go", RunOptions::new("s1"));
            let run_id = stream.run_id().clone();
            assert!(drain(stream).await.1.is_some(), "{context}");
            let agent = build(redacting(), replay(), hooks(), stores.clone());
            let run_id = match stores.stores.runs.load(NS, &run_id).await {
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
            };
            let record = stores.stores.runs.load(NS, &run_id).await.unwrap();
            assert_eq!(record.status, RunStatus::Completed, "{context}");
            let log = messages(&stores.stores, "s1").await;
            assert_eq!(log.len(), want.len(), "{context}");
            // A crash while the tool ran leaves it interrupted, never re-run.
            if log[2] != want[2] {
                assert_eq!(result_texts(&log[2]), [INTERRUPTED], "{context}");
            }
            assert_eq!(
                (&log[..2], &log[3..]),
                (&want[..2], &want[3..]),
                "{context}"
            );
            assert!(!stores.leaked(), "{context}: a write held the raw text");
        }
    }
}

/// A subagent's runs reach the same logs, so they take the agent's
/// redaction; they answer the parent, so not its verifiers.
#[tokio::test]
async fn a_subagent_takes_the_redaction_but_not_the_verifiers() {
    let mut spec = redacting();
    spec.verification = Some(VerificationSpec {
        max_retries: 2,
        verifiers: Vec::new(),
    });
    let answer = text_turn(&format!("child says {SECRET}"));
    let provider = Arc::new(ScriptedProvider::new(vec![answer]));
    let stores = Arc::new(Stores::default());
    let parent = AgentBuilder::new(spec)
        .provider(provider)
        .permissions(policy(PermissionMode::SkipDangerous))
        .run_store(stores.clone())
        .session_store(stores.clone())
        .namespace(NS)
        .subagents(Vec::new())
        .build()
        .unwrap();
    let child = parent.subagent(None).unwrap();
    assert_eq!(child.spec().redaction, Some(redaction()));
    assert_eq!(child.spec().verification, None);
    let stream = child.run_child("go", RunOptions::new("child"), RunId::new("parent"));
    let envelopes = finish(stream).await;
    assert!(!leaks(&envelopes));
    let log = messages(&*stores, "child").await;
    assert_eq!(
        log[1],
        Message::assistant_text(format!("child says {MASK}"))
    );
}

/// A peer message was written by another agent, from its tool call's
/// input, so it is scrubbed like a tool result before the prompt it joins
/// is logged.
#[tokio::test]
async fn a_peer_message_is_scrubbed_like_a_tool_result() {
    let mailbox = Arc::new(InMemoryMailbox::default());
    let note = MailboxMessage::new("bob", "s1", format!("use {SECRET}"));
    mailbox.send(note).await.unwrap();
    let provider = Arc::new(ScriptedProvider::new(vec![text_turn("ok")]));
    let stores = Arc::new(Stores::default());
    let agent = AgentBuilder::new(redacting())
        .provider(provider.clone())
        .permissions(policy(PermissionMode::SkipDangerous))
        .run_store(stores.clone())
        .session_store(stores.clone())
        .namespace(NS)
        .mailbox(mailbox)
        .build()
        .unwrap();
    let envelopes = finish(agent.run("go", RunOptions::new("s1"))).await;
    let log = messages(&*stores, "s1").await;
    assert!(log[0].text().contains(MASK), "{:?}", log[0]);
    assert!(!leaks(&log) && !leaks(&envelopes) && !leaks(provider.requests()));
}
