//! A task list shared by every session (`task_tools_in`), and the hook that
//! shows its open tasks with each request (`TaskLedgerContext`).

mod common;

use std::sync::Arc;

use common::{ctx, input};
use linch_core::hooks::ContextBuildRequest;
use linch_core::{
    Hook, MemoryRunStore, MemorySessionStore, RunId, SessionId, SessionStore, TaskStore, Tool,
};
use linch_tools::{DEFAULT_LEDGER_CHARS, LEDGER_POLICY_ID, TaskLedgerContext, task_tools_in};
use serde_json::{Value, json};

const LIST: &str = "ledger-1";

struct Ledger {
    store: Arc<MemorySessionStore>,
    tools: Vec<Arc<dyn Tool>>,
}

impl Ledger {
    async fn new() -> Self {
        let store = Arc::new(MemorySessionStore::new());
        store.create("ns", &SessionId::from(LIST)).await.unwrap();
        let runs = Arc::new(MemoryRunStore::new());
        let tools = task_tools_in(store.clone(), runs, "ns", SessionId::from(LIST));
        Self { store, tools }
    }

    /// Run `name` as a call of session `session`.
    async fn call(&self, session: &str, name: &str, value: Value) -> String {
        let tool = self.tools.iter().find(|tool| tool.name() == name).unwrap();
        let input = tool.validate(input(value)).unwrap();
        let output = tool.execute(input, ctx(session)).await.unwrap();
        output.value.as_str().unwrap().to_owned()
    }

    fn hook(&self, max_chars: usize) -> TaskLedgerContext {
        let store: Arc<dyn TaskStore> = self.store.clone();
        TaskLedgerContext::new(store, "ns", SessionId::from(LIST), max_chars)
    }
}

async fn shown(hook: &TaskLedgerContext, session: &str) -> String {
    let request = ContextBuildRequest {
        session_id: SessionId::from(session),
        run_id: RunId::from("r"),
        messages: Arc::default(),
        principal: None,
    };
    let response = hook.call(&request).await.unwrap();
    assert_eq!(response.blocks.len(), 1);
    response.blocks[0].text.clone()
}

#[tokio::test]
async fn every_session_shares_the_list() {
    let ledger = Ledger::new().await;
    let made = ledger
        .call(
            "a",
            "TaskCreate",
            json!({"subject": "Read a.txt", "description": "x"}),
        )
        .await;
    assert_eq!(made, "Task #1 created: Read a.txt");
    // Another session, another run: the same list.
    let listed = ledger.call("b", "TaskList", json!({})).await;
    assert_eq!(listed, "#1 [pending] Read a.txt");
    ledger
        .call("b", "TaskUpdate", json!({"id": "1", "status": "completed"}))
        .await;
    let listed = ledger.call("a", "TaskList", json!({})).await;
    assert_eq!(listed, "#1 [completed] Read a.txt");
}

#[tokio::test]
async fn the_hook_shows_open_tasks_and_counts_the_done() {
    let ledger = Ledger::new().await;
    let hook = ledger.hook(DEFAULT_LEDGER_CHARS);
    assert_eq!(hook.policy_id(), LEDGER_POLICY_ID);
    assert!(shown(&hook, "s").await.ends_with("The ledger is empty."));
    for subject in ["one", "two", "three"] {
        let value = json!({"subject": subject, "description": "d"});
        ledger.call("s", "TaskCreate", value).await;
    }
    ledger
        .call("s", "TaskUpdate", json!({"id": "1", "status": "completed"}))
        .await;
    ledger
        .call(
            "s",
            "TaskUpdate",
            json!({"id": "2", "status": "in_progress"}),
        )
        .await;
    let text = shown(&hook, "other-session").await;
    assert!(
        text.contains("\n#2 [in_progress] two\n#3 [pending] three"),
        "{text}"
    );
    assert!(
        !text.contains("#1 "),
        "a finished task is only counted: {text}"
    );
    assert!(text.ends_with("\n(1 completed)"), "{text}");
}

#[tokio::test]
async fn the_hook_stops_at_its_budget_and_counts_what_it_left_out() {
    let ledger = Ledger::new().await;
    for n in 0..10 {
        let value = json!({"subject": format!("task number {n}"), "description": "d"});
        ledger.call("s", "TaskCreate", value).await;
    }
    let header = shown(&ledger.hook(10_000), "s").await;
    let first = header.find("\n#1 ").unwrap();
    // Room for the header and three lines.
    let hook = ledger.hook(first + 3 * "\n#1 [pending] task number 0".len());
    let text = shown(&hook, "s").await;
    assert!(text.contains("task number 2"), "{text}");
    assert!(!text.contains("task number 3"), "{text}");
    assert!(
        text.contains("[7 more open tasks not shown; call TaskList.]"),
        "{text}"
    );
}
