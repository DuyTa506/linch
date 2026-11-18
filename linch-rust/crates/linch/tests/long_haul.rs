//! The building blocks of a long-running agent, composed: a goal contract
//! (a system section) that compaction never touches, a task ledger shared by
//! every session and shown with each request, a compaction that fires every
//! turn, a run cut off and resumed, and sessions that each start with an empty
//! conversation. Assertions are on what is durable (tasks, events, the session
//! log) and on what the provider was sent.

use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use futures::StreamExt;
use linch::provider::scripted::TurnBuilder;
use linch::provider::{ProviderError, ProviderRequest, ProviderStream};
use linch::tools::{TaskLedgerContext, task_tools_in};
use linch::{
    Agent, AgentBuilder, AgentSpec, CompactionSpec, Event, Hooks, LogEntry, MemoryRunStore,
    MemorySessionStore, PermissionEngine, PermissionMode, PermissionPolicy, Provider, Role,
    RunOptions, RunStore, ScriptedTurn, SessionId, SessionStore, SystemSection, TaskStore,
    ToolChoice, ToolRegistry,
};
use serde_json::{Value, json};
use tokio_util::sync::CancellationToken;

const MODEL: &str = "scripted-model";
const NS: &str = "t";
const LIST: &str = "ledger";
const GOAL: &str = "GOAL CONTRACT: the report is done when report.md has one section per file.";

/// Answers from the request alone, as a model that keeps nothing in its head
/// would: what to do next comes from the task ledger the request carries and
/// from the last tool result, so a resumed, compacted or fresh session gets
/// the answer an uninterrupted one would. Keeps every request.
struct Scripts {
    requests: Mutex<Vec<ProviderRequest>>,
}

fn ledger_counts(request: &ProviderRequest) -> (usize, usize, Option<usize>) {
    let text: String = request.messages.iter().map(|m| m.text()).collect();
    let open = text.matches("] Summarise").count();
    let done = text
        .rsplit_once('(')
        .and_then(|(_, tail)| tail.split_once(" completed)"))
        .and_then(|(n, _)| n.parse::<usize>().ok())
        .unwrap_or(0);
    let first_open = text
        .split_once("\n#")
        .and_then(|(_, tail)| tail.split_once(' '))
        .and_then(|(id, _)| id.parse().ok());
    (open, done, first_open)
}

#[async_trait]
impl Provider for Scripts {
    fn id(&self) -> &str {
        "scripts"
    }

    async fn stream(
        &self,
        request: ProviderRequest,
        _cancel: CancellationToken,
    ) -> Result<ProviderStream, ProviderError> {
        self.requests.lock().unwrap().push(request.clone());
        let turn = if request.tool_choice == Some(ToolChoice::Disabled {}) {
            TurnBuilder::new(MODEL).text("summary").build()
        } else {
            let (open, done, first_open) = ledger_counts(&request);
            let last = request.messages.last().unwrap();
            let result = match &last.content[0] {
                linch::ContentBlock::ToolResult(result) => Some(result.tool_use_id.clone()),
                _ => None,
            };
            let total = open + done;
            match result.as_deref() {
                _ if total < 3 => {
                    create(total, &format!("Summarise {}.txt", ["a", "b", "c"][total]))
                }
                Some(id) if id.starts_with("create") => say("planned"),
                Some(_) => say("piece done"),
                None => complete(&first_open.unwrap().to_string()),
            }
        };
        Ok(stream(&turn))
    }
}

fn stream(turn: &ScriptedTurn) -> ProviderStream {
    let events: Vec<_> = turn.events.iter().cloned().map(Ok).collect();
    futures::stream::iter(events).boxed()
}

fn call(id: &str, tool: &str, input: Value) -> ScriptedTurn {
    TurnBuilder::new(MODEL).tool_call(id, tool, &input).build()
}

fn say(text: &str) -> ScriptedTurn {
    TurnBuilder::new(MODEL).text(text).build()
}

fn create(n: usize, subject: &str) -> ScriptedTurn {
    let input = json!({"subject": subject, "description": "d"});
    call(&format!("create_{n}"), "TaskCreate", input)
}

fn complete(id: &str) -> ScriptedTurn {
    let input = json!({"id": id, "status": "completed"});
    call("update_0", "TaskUpdate", input)
}

/// An agent over `stores`, with the goal as a section, the shared ledger,
/// and a compaction that fires before every turn after the first.
fn agent(
    provider: &Arc<Scripts>,
    runs: &Arc<MemoryRunStore>,
    sessions: &Arc<MemorySessionStore>,
) -> Agent {
    let mut spec = AgentSpec::new(MODEL);
    spec.system.sections = vec![SystemSection::new("goal", GOAL)];
    spec.compaction = Some(CompactionSpec {
        keep_recent_turns: 1,
        ..CompactionSpec::new(1)
    });
    let list = SessionId::from(LIST);
    let tools = ToolRegistry::new();
    let store: Arc<dyn TaskStore> = sessions.clone();
    for tool in task_tools_in(store.clone(), runs.clone(), NS, list.clone()) {
        let _ = tools.register(tool).unwrap();
    }
    let hooks = Hooks::default();
    let ledger = TaskLedgerContext::new(store, NS, list, 4000);
    let _ = hooks.context_build.register(Arc::new(ledger)).unwrap();
    let policy = PermissionPolicy::new(PermissionMode::SkipDangerous, "/");
    AgentBuilder::new(spec)
        .provider(provider.clone() as Arc<dyn Provider>)
        .tools(Arc::new(tools))
        .hooks(Arc::new(hooks))
        .permissions(PermissionEngine::new(policy).unwrap())
        .run_store(runs.clone())
        .session_store(sessions.clone())
        .namespace(NS)
        .build()
        .unwrap()
}

fn text_of(request: &ProviderRequest) -> String {
    request.messages.last().unwrap().text()
}

#[tokio::test]
async fn the_contract_and_the_ledger_outlive_compaction_a_cut_run_and_new_sessions() {
    let runs = Arc::new(MemoryRunStore::new());
    let sessions = Arc::new(MemorySessionStore::new());
    sessions.create(NS, &SessionId::from(LIST)).await.unwrap();
    let scripts = Arc::new(Scripts {
        requests: Mutex::default(),
    });

    // The initializer is cut off after its second task, and resumed.
    let first = agent(&scripts, &runs, &sessions);
    let mut stream = first.run("INIT", RunOptions::new("init"));
    let run_id = stream.run_id().clone();
    let mut tool_ends = 0;
    while let Some(item) = stream.next().await {
        if matches!(item.unwrap().event, Event::ToolCallEnd(_)) {
            tool_ends += 1;
            if tool_ends == 2 {
                break;
            }
        }
    }
    drop(stream);
    let second = agent(&scripts, &runs, &sessions);
    let mut resumed = second.resume(run_id.clone(), RunOptions::new("init"));
    while let Some(item) = resumed.next().await {
        item.unwrap();
    }

    // Two more pieces, each in a session whose conversation starts empty.
    for (prompt, session) in [("PIECE 1", "w1"), ("PIECE 2", "w2")] {
        let mut stream = second.run(prompt, RunOptions::new(session));
        while let Some(item) = stream.next().await {
            item.unwrap();
        }
    }

    // Durable facts: the ledger, the journal, the log.
    let tasks = sessions
        .list_tasks(NS, &SessionId::from(LIST))
        .await
        .unwrap();
    let state: Vec<(&str, &str)> = (tasks.iter())
        .map(|t| (t.subject.as_str(), t.status.as_str()))
        .collect();
    assert_eq!(
        state,
        [
            ("Summarise a.txt", "completed"),
            ("Summarise b.txt", "completed"),
            ("Summarise c.txt", "pending"),
        ]
    );
    let events = runs.events(NS, &run_id, 0).await.unwrap();
    let creates = (events.iter())
        .filter(|e| matches!(&e.event, Event::ToolCallStart(s) if s.tool_name == "TaskCreate"))
        .count();
    assert_eq!(creates, 3, "no task call ran twice across the cut");
    let log = sessions.load(NS, &SessionId::from("init")).await.unwrap();
    let projections = log
        .iter()
        .filter(|e| matches!(e, LogEntry::Projection(_)))
        .count();
    assert!(projections >= 1, "the run compacted");
    let messages: String = (log.iter())
        .filter_map(|entry| match entry {
            LogEntry::Message(message) => Some(format!("{message:?}")),
            _ => None,
        })
        .collect();
    assert!(
        !messages.contains("Task ledger"),
        "the ledger is never logged"
    );
    let prefixes = format!(
        "{:?}",
        log.iter().find(|e| matches!(e, LogEntry::Prefix(_)))
    );
    assert!(
        prefixes.contains("GOAL CONTRACT"),
        "the goal is in the frozen prefix"
    );

    // What the provider was sent.
    let requests = scripts.requests.lock().unwrap().clone();
    let primary: Vec<_> = (requests.iter())
        .filter(|r| r.tool_choice != Some(ToolChoice::Disabled {}))
        .collect();
    assert!(primary.len() >= 8, "{} primary requests", primary.len());
    for request in &primary {
        let goal = request.system.iter().filter(|b| b.text == GOAL).count();
        assert_eq!(goal, 1, "every request carries the goal, once");
        assert!(text_of(request).contains("Task ledger"), "and the ledger");
    }
    // A fresh session reads where the work stands in its first request.
    let first_of = |marker: &str| {
        primary
            .iter()
            .find(|r| r.messages.len() == 1 && r.messages[0].text().starts_with(marker))
    };
    let w1 = first_of("PIECE 1").expect("the first request of session w1");
    let text = text_of(w1);
    assert!(text.contains("#1 [pending] Summarise a.txt"), "{text}");
    assert!(text.contains("#3 [pending] Summarise c.txt"), "{text}");
    let w2 = first_of("PIECE 2").expect("the first request of session w2");
    let text = text_of(w2);
    assert!(
        !text.contains("Summarise a.txt"),
        "a finished task is counted, not listed: {text}"
    );
    assert!(text.contains("#2 [pending] Summarise b.txt"), "{text}");
    assert!(text.contains("(1 completed)"), "{text}");
    // Compaction left the conversation short, and the contract whole.
    let compacted = primary
        .iter()
        .filter(|r| r.messages.iter().any(|m| m.text().starts_with("<summary")))
        .count();
    assert!(compacted >= 1, "a request carried a summary");
    assert!(
        primary
            .iter()
            .all(|r| r.messages.first().map(|m| m.role) == Some(Role::User))
    );
}
