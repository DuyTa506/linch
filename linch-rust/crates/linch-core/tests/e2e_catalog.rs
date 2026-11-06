//! Catalog changes mid-session: the request prefix a session froze stays
//! byte-identical, and every change goes at the conversation's tail.

mod common;

use std::collections::HashMap;
use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use common::*;
use linch_core::store::faulty::{FaultTiming, Faulty};
use linch_core::{
    Agent, CatalogChangeBlock, ContentBlock, JsonObject, LogEntry, Message, PermissionMode,
    ProviderRequest, Registration, RunOptions, RunStore, ScriptedProvider, ScriptedTurn, SessionId,
    SessionStore, Tool, ToolContext, ToolOutput, ToolRegistry, ToolResult, ToolScope,
};
use serde_json::json;

/// The registrations a test may undo, by tool name.
type Book = Arc<Mutex<HashMap<String, Registration>>>;

type Action = Box<dyn Fn(&ToolRegistry, &mut HashMap<String, Registration>) + Send + Sync>;

/// A tool that changes the catalog when it runs.
struct Mutate {
    schema: JsonObject,
    registry: Arc<ToolRegistry>,
    book: Book,
    action: Action,
}

#[async_trait]
impl Tool for Mutate {
    fn name(&self) -> &str {
        "Mutate"
    }
    fn description(&self) -> &str {
        "Change the catalog."
    }
    fn input_schema(&self) -> &JsonObject {
        &self.schema
    }
    fn scope(&self) -> ToolScope {
        ToolScope::Write
    }
    async fn execute(&self, _input: JsonObject, _ctx: ToolContext) -> ToolResult {
        (self.action)(&self.registry, &mut self.book.lock().unwrap());
        Ok(ToolOutput::new("changed"))
    }
}

/// A registry with `Echo` (its handle in the book) and a `Mutate` that
/// runs `action`.
fn mutating(
    action: impl Fn(&ToolRegistry, &mut HashMap<String, Registration>) + Send + Sync + 'static,
) -> (Arc<ToolRegistry>, Book) {
    let tools = Arc::new(ToolRegistry::new());
    let book: Book = Arc::default();
    let echo = Echo::new("Echo", ToolScope::Read, &Runs::default());
    let handle = tools.register(Arc::new(echo)).unwrap();
    book.lock().unwrap().insert("Echo".into(), handle);
    let mutate = Mutate {
        schema: object(json!({"type": "object"})),
        registry: Arc::clone(&tools),
        book: Arc::clone(&book),
        action: Box::new(action),
    };
    let _ = tools.register(Arc::new(mutate)).unwrap();
    (tools, book)
}

/// A tool with a name, a description and one string field, standing in
/// for a new version of a tool.
struct Spec {
    name: &'static str,
    description: &'static str,
    schema: JsonObject,
}

impl Spec {
    fn new(name: &'static str, description: &'static str, field: &str) -> Arc<Self> {
        let schema = json!({"type": "object", "properties": {field: {"type": "string"}}});
        let schema = object(schema);
        Arc::new(Self {
            name,
            description,
            schema,
        })
    }
}

#[async_trait]
impl Tool for Spec {
    fn name(&self) -> &str {
        self.name
    }
    fn description(&self) -> &str {
        self.description
    }
    fn input_schema(&self) -> &JsonObject {
        &self.schema
    }
    fn scope(&self) -> ToolScope {
        ToolScope::Read
    }
    async fn execute(&self, _input: JsonObject, _ctx: ToolContext) -> ToolResult {
        Ok(ToolOutput::new("spec"))
    }
}

fn add_late(registry: &ToolRegistry, book: &mut HashMap<String, Registration>) {
    let tool = Echo::new("Late", ToolScope::Read, &Runs::default());
    let handle = registry.replace(Arc::new(tool)).unwrap();
    book.insert("Late".into(), handle);
}

/// An agent on `tools` and `stores` answering `turns`.
fn scripted_on(
    tools: Arc<ToolRegistry>,
    turns: Vec<ScriptedTurn>,
    stores: Arc<Stores>,
) -> (Agent, Arc<ScriptedProvider>) {
    let provider = Arc::new(ScriptedProvider::new(turns));
    let permissions = policy(PermissionMode::SkipDangerous);
    let agent = agent(provider.clone(), tools, permissions, stores);
    (agent, provider)
}

fn scripted(
    tools: Arc<ToolRegistry>,
    turns: Vec<ScriptedTurn>,
) -> (Agent, Arc<ScriptedProvider>, Arc<Stores>) {
    let stores = Arc::new(Stores::default());
    let (agent, provider) = scripted_on(tools, turns, stores.clone());
    (agent, provider, stores)
}

/// The wire bytes of a request's system blocks and of its first `n` tools.
fn prefix_bytes(request: &ProviderRequest, n: usize) -> (String, String) {
    let system = serde_json::to_string(&request.system).unwrap();
    let tools = serde_json::to_string(&request.tools[..n]).unwrap();
    (system, tools)
}

fn names(request: &ProviderRequest) -> Vec<&str> {
    request.tools.iter().map(|t| t.name.as_str()).collect()
}

/// The catalog notices of `message`.
fn notices(message: &Message) -> Vec<&CatalogChangeBlock> {
    (message.content.iter())
        .filter_map(|block| match block {
            ContentBlock::CatalogChange(change) => Some(change),
            _ => None,
        })
        .collect()
}

/// Every notice in the messages of `request`.
fn all_notices(request: &ProviderRequest) -> Vec<&CatalogChangeBlock> {
    request.messages.iter().flat_map(notices).collect()
}

/// Python Linch's notice text for `lines`.
fn reminder(lines: &str) -> String {
    let header = "The available catalog changed during this session.";
    format!("<system-reminder>\n{header}\n{lines}\n</system-reminder>")
}

#[tokio::test]
async fn a_tool_registered_mid_session_leaves_the_prefix_bytes_alone() {
    let (tools, _book) = mutating(add_late);
    let turns = vec![
        tool_turn(0, 1, "Mutate"),
        text_turn("done"),
        text_turn("again"),
        text_turn("fresh"),
    ];
    let (agent, provider, stores) = scripted(tools, turns);
    finish(agent.run("go", RunOptions::new("s1"))).await;
    finish(agent.run("more", RunOptions::new("s1"))).await;
    finish(agent.run("hi", RunOptions::new("s2"))).await;
    let requests = provider.requests();
    assert_eq!(requests.len(), 4);
    let first = prefix_bytes(&requests[0], 2);
    assert_eq!(names(&requests[0]), ["Echo", "Mutate"]);
    for later in &requests[1..3] {
        assert_eq!(prefix_bytes(later, 2), first, "the frozen prefix moved");
        // The added tool is appended after the frozen rows.
        assert_eq!(names(later), ["Echo", "Mutate", "Late"]);
    }
    // The notice rides the tool results of the turn that changed it.
    let results = requests[1].messages.last().unwrap();
    let [notice] = notices(results)[..] else {
        panic!("one notice: {results:?}")
    };
    assert_eq!(notice.text, reminder("Added tools: Late"));
    assert_eq!(notice.added_tools, ["Late"]);
    assert_eq!(notice.tool_definitions, requests[1].tools[2..]);
    assert_eq!(all_notices(&requests[2]), [notice], "told once");
    // A new session takes the live catalog into its own prefix.
    assert_eq!(names(&requests[3]), ["Echo", "Mutate", "Late"]);
    assert!(all_notices(&requests[3]).is_empty());

    // The prefix rides the first prompt's write and is logged once; the
    // notice is a logged part of the tool results.
    let entries = SessionStore::load(&*stores, NS, &SessionId::new("s1"))
        .await
        .unwrap();
    let prefixes = (entries.iter())
        .filter(|e| matches!(e, LogEntry::Prefix(_)))
        .count();
    assert!(matches!(entries[0], LogEntry::Prefix(_)), "{entries:?}");
    assert_eq!(prefixes, 1);
    let logged = messages(&*stores, "s1").await;
    assert_eq!(notices(&logged[2]), [notice]);
}

#[tokio::test]
async fn a_removed_tool_keeps_its_row_and_a_call_to_it_is_refused() {
    let (tools, _book) = mutating(|_, book| book.remove("Echo").unwrap().dispose());
    let turns = vec![
        tool_turn(0, 1, "Mutate"),
        tool_turn(1, 1, "Echo"),
        text_turn("done"),
    ];
    let (agent, provider, _) = scripted(tools, turns);
    finish(agent.run("go", RunOptions::new("s1"))).await;
    let requests = provider.requests();
    let first = prefix_bytes(&requests[0], 2);
    for later in &requests[1..] {
        assert_eq!(prefix_bytes(later, 2), first);
        assert_eq!(names(later), ["Echo", "Mutate"]);
    }
    let [notice] = all_notices(&requests[1])[..] else {
        panic!("one notice")
    };
    assert_eq!(notice.text, reminder("Removed tools: Echo"));
    assert_eq!(notice.removed_tools, ["Echo"]);
    let refused = result_texts(requests[2].messages.last().unwrap());
    assert_eq!(refused, ["Tool \"Echo\" is not registered"]);
}

#[tokio::test]
async fn a_new_description_is_told_and_keeps_the_cached_row() {
    let (tools, _book) = mutating(|registry, book| {
        let handle = registry.replace(Spec::new("Echo", "Echo, louder.", "value"));
        book.insert("Echo2".into(), handle.unwrap());
    });
    let turns = vec![tool_turn(0, 1, "Mutate"), text_turn("done")];
    let (agent, provider, _) = scripted(tools, turns);
    finish(agent.run("go", RunOptions::new("s1"))).await;
    let requests = provider.requests();
    let all = |r: &ProviderRequest| serde_json::to_string(&r.tools).unwrap();
    assert_eq!(all(&requests[1]), all(&requests[0]), "the old row stays");
    let [notice] = all_notices(&requests[1])[..] else {
        panic!("one notice")
    };
    assert_eq!(notice.text, reminder("Updated tools: Echo: Echo, louder."));
    assert!(notice.added_tools.is_empty() && notice.removed_tools.is_empty());
}

#[tokio::test]
async fn a_new_input_schema_replaces_its_row_in_place() {
    let (tools, _book) = mutating(|registry, book| {
        let handle = registry.replace(Spec::new("Echo", "Echo the value.", "text"));
        book.insert("Echo2".into(), handle.unwrap());
    });
    let turns = vec![tool_turn(0, 1, "Mutate"), text_turn("done")];
    let (agent, provider, _) = scripted(tools, turns);
    finish(agent.run("go", RunOptions::new("s1"))).await;
    let requests = provider.requests();
    assert_eq!(names(&requests[1]), ["Echo", "Mutate"]);
    let schema = &requests[1].tools[0].input_schema;
    assert!(schema["properties"].get("text").is_some(), "{schema:?}");
    assert_eq!(requests[1].tools[1], requests[0].tools[1]);
    let [notice] = all_notices(&requests[1])[..] else {
        panic!("one notice")
    };
    assert_eq!(
        notice.text,
        reminder("Updated tools: Echo: Echo the value.")
    );
}

#[tokio::test]
async fn a_registration_that_changes_nothing_adds_no_notice() {
    let (tools, _book) = mutating(|registry, _| {
        let tool = Echo::new("Brief", ToolScope::Read, &Runs::default());
        registry.register(Arc::new(tool)).unwrap().dispose();
    });
    let turns = vec![tool_turn(0, 1, "Mutate"), text_turn("done")];
    let (agent, provider, stores) = scripted(tools, turns);
    finish(agent.run("go", RunOptions::new("s1"))).await;
    let requests = provider.requests();
    assert!(all_notices(&requests[1]).is_empty());
    let first = serde_json::to_string(&requests[0].tools).unwrap();
    assert_eq!(serde_json::to_string(&requests[1].tools).unwrap(), first);
    // One tool turn and the answer: 3 + 2 run commits and the create's
    // final one, 3 session appends.
    assert_eq!(stores.runs.writes().commit, (3 + 2) + 3);
    assert_eq!(stores.sessions.writes().append, 4);
}

#[tokio::test]
async fn a_change_between_runs_is_told_in_the_next_prompt() {
    let (tools, book) = mutating(|_, _| {});
    let turns = vec![text_turn("first"), text_turn("second")];
    let (agent, provider, _) = scripted(Arc::clone(&tools), turns);
    finish(agent.run("go", RunOptions::new("s1"))).await;
    add_late(&tools, &mut book.lock().unwrap());
    finish(agent.run("more", RunOptions::new("s1"))).await;
    let requests = provider.requests();
    assert_eq!(prefix_bytes(&requests[1], 2), prefix_bytes(&requests[0], 2));
    assert_eq!(names(&requests[1]), ["Echo", "Mutate", "Late"]);
    let prompt = requests[1].messages.last().unwrap();
    assert_eq!(prompt.text(), "more");
    let [notice] = notices(prompt)[..] else {
        panic!("one notice: {prompt:?}")
    };
    assert_eq!(notice.text, reminder("Added tools: Late"));
}

#[tokio::test]
async fn a_crash_at_any_write_tells_a_change_once() {
    let turns = || vec![tool_turn(0, 1, "Mutate"), text_turn("done")];
    let baseline = Arc::new(Stores::default());
    let (tools, _book) = mutating(add_late);
    let (clean, _) = scripted_on(tools, turns(), baseline.clone());
    finish(clean.run("go", RunOptions::new("s1"))).await;
    let writes = baseline.log.lock().unwrap().len();
    for n in 1..=writes as u64 {
        for timing in [FaultTiming::Before, FaultTiming::After] {
            let context = format!("write {n} failing {timing:?}");
            let stores = Arc::new(Stores::default());
            let faulty = Arc::new(Faulty::new(stores.clone(), n, timing));
            let (tools, _book) = mutating(add_late);
            let provider = Arc::new(Replay::new(turns()));
            let permissions = || policy(PermissionMode::SkipDangerous);
            let crashing = agent(provider.clone(), Arc::clone(&tools), permissions(), faulty);
            // Built before the change, so its contract is the run's.
            let clean = agent(provider, Arc::clone(&tools), permissions(), stores.clone());
            let stream = crashing.run("go", RunOptions::new("s1"));
            let run_id = stream.run_id().clone();
            let (_, error) = drain(stream).await;
            assert!(error.is_some(), "{context}: the run must stop");
            match stores.runs.load(NS, &run_id).await {
                Ok(record) if record.status.is_terminal() => {}
                Ok(_) => drop(finish(clean.resume(run_id, RunOptions::new("s1"))).await),
                Err(_) => drop(finish(clean.run("go", RunOptions::new("s1"))).await),
            }
            let logged = messages(&*stores, "s1").await;
            let told: Vec<&CatalogChangeBlock> = logged.iter().flat_map(notices).collect();
            let texts: Vec<&str> = told.iter().map(|n| n.text.as_str()).collect();
            // A crash right after Mutate's start leaves it interrupted, so
            // it never ran and nothing changed.
            let ran = tools.snapshot().resolve("Late").is_some();
            let want = if ran {
                vec![reminder("Added tools: Late")]
            } else {
                Vec::new()
            };
            assert_eq!(texts, want, "{context}");
        }
    }
}

#[tokio::test]
async fn a_compaction_keeps_the_prefix_and_tells_the_folded_changes_once() {
    let (tools, _book) = mutating(add_late);
    let summary = linch_core::provider::scripted::TurnBuilder::new(MODEL)
        .text("summary")
        .usage(usage(3, 2))
        .build();
    let turns = vec![
        tool_turn(0, 1, "Mutate"),
        tool_turn(1, 1, "Echo"),
        summary,
        text_turn("done"),
    ];
    let mut spec = spec();
    spec.compaction = Some(linch_core::CompactionSpec {
        keep_recent_turns: 1,
        ..linch_core::CompactionSpec::new(1)
    });
    let provider = Arc::new(ScriptedProvider::new(turns));
    let stores = Arc::new(Stores::default());
    let permissions = policy(PermissionMode::SkipDangerous);
    let agent = agent_with(spec, provider.clone(), tools, permissions, stores);
    finish(agent.run("go", RunOptions::new("s1"))).await;
    let requests = provider.requests();
    assert_eq!(requests.len(), 4);
    let (before, after) = (&requests[1], &requests[3]);
    let all = |r: &ProviderRequest| prefix_bytes(r, r.tools.len());
    assert_eq!(all(after), all(before), "compaction moved the prefix");
    assert_eq!(names(after), ["Echo", "Mutate", "Late"]);
    // Every request after the change names the added row, the summary
    // call's too, though the compaction folds the notice that added it.
    assert!(requests[0].appended_tools.is_empty());
    for request in &requests[1..] {
        assert_eq!(request.appended_tools, ["Late"]);
    }
    // The notice the summary folded away comes back once, in the summary.
    let [restated] = all_notices(after)[..] else {
        panic!("one notice: {:?}", after.messages)
    };
    assert!(notices(&after.messages[1]).contains(&restated));
    assert_eq!(restated.text, reminder("Added tools: Late"));
    assert_eq!(restated.tool_definitions, after.tools[2..]);
}
