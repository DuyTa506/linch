//! `TaskCreate`, `TaskList`, `TaskGet` and `TaskUpdate` over the memory
//! session store: Python's schemas, scopes and model-facing texts.

mod common;

use std::sync::Arc;

use common::{ctx, input};
use linch_core::{
    MemoryRunStore, MemorySessionStore, SessionId, SessionStore, TaskStatus, TaskStore, Tool,
    ToolRegistry, ToolResult, ToolScope,
};
use linch_tools::{TASK_TOOL_NAMES, task_tools};
use serde_json::{Value, json};

struct Board {
    store: Arc<MemorySessionStore>,
    tools: Vec<Arc<dyn Tool>>,
}

impl Board {
    /// The four tools over a store holding session `s1` of namespace `ns`.
    async fn new() -> Self {
        let store = Arc::new(MemorySessionStore::new());
        store.create("ns", &SessionId::from("s1")).await.unwrap();
        let tools = task_tools(store.clone(), Arc::new(MemoryRunStore::new()), "ns");
        Self { store, tools }
    }

    fn tool(&self, name: &str) -> &dyn Tool {
        let tool = self.tools.iter().find(|tool| tool.name() == name);
        tool.expect("a task tool").as_ref()
    }

    /// Validate and run, as the registry does, in session `session`.
    async fn call_in(&self, session: &str, name: &str, value: Value) -> ToolResult {
        let tool = self.tool(name);
        let input = tool.validate(input(value))?;
        tool.execute(input, ctx(session)).await
    }

    async fn call(&self, name: &str, value: Value) -> ToolResult {
        self.call_in("s1", name, value).await
    }

    async fn text(&self, name: &str, value: Value) -> String {
        let output = self.call(name, value).await.expect("the call succeeds");
        output.value.as_str().expect("text output").to_owned()
    }

    async fn error(&self, name: &str, value: Value) -> (String, String) {
        let error = self.call(name, value).await.expect_err("the call fails");
        (error.message, error.code.unwrap_or_default())
    }
}

#[tokio::test]
async fn names_scopes_descriptions_and_schemas_match_python() {
    let board = Board::new().await;
    let names: Vec<&str> = board.tools.iter().map(|tool| tool.name()).collect();
    assert_eq!(names, TASK_TOOL_NAMES);
    assert_eq!(names, ["TaskCreate", "TaskList", "TaskGet", "TaskUpdate"]);
    let expected = [
        (
            "TaskCreate",
            ToolScope::Write,
            "Create a new persistent task in the current session.",
        ),
        (
            "TaskList",
            ToolScope::Read,
            "List all tasks for the current session.",
        ),
        ("TaskGet", ToolScope::Read, "Get one task by id."),
        ("TaskUpdate", ToolScope::Write, "Patch one task."),
    ];
    for (name, scope, description) in expected {
        let tool = board.tool(name);
        assert_eq!(
            (tool.scope(), tool.description()),
            (scope, description),
            "{name}"
        );
        let parallel = tool.parallel(&input(json!({"id": "1"})));
        assert_eq!(
            parallel,
            scope == ToolScope::Read,
            "only reads run in parallel"
        );
    }
    let schema = |name| Value::Object(board.tool(name).input_schema().clone());
    let create = json!({
        "type": "object",
        "properties": {
            "subject": {"type": "string", "description": "Brief task title."},
            "description": {"type": "string", "description": "What needs to be done."},
            "active_form": {
                "type": "string",
                "description": "Present-continuous label shown in spinner.",
            },
            "metadata": {"type": "object", "description": "Arbitrary key-value metadata."},
        },
        "required": ["subject", "description"],
    });
    assert_eq!(schema("TaskCreate"), create);
    assert_eq!(
        schema("TaskList"),
        json!({"type": "object", "properties": {}})
    );
    let get =
        json!({"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]});
    assert_eq!(schema("TaskGet"), get);
    let ids = json!({"type": "array", "items": {"type": "string"}});
    let update = json!({
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "subject": {"type": "string"},
            "description": {"type": "string"},
            "active_form": {"type": "string"},
            "status": {
                "type": "string",
                "enum": ["pending", "in_progress", "completed", "deleted"],
            },
            "owner": {"type": "string"},
            "add_blocks": ids,
            "add_blocked_by": ids,
            "remove_blocks": ids,
            "remove_blocked_by": ids,
            "metadata": {"type": "object"},
        },
        "required": ["id"],
    });
    assert_eq!(schema("TaskUpdate"), update);
    let registry = ToolRegistry::new();
    for tool in &board.tools {
        let registered = registry.register(Arc::clone(tool));
        let _ = registered.expect("the schema compiles");
    }
}

#[tokio::test]
async fn the_tools_report_with_pythons_texts() {
    let board = Board::new().await;
    assert_eq!(board.text("TaskList", json!({})).await, "No tasks found.");
    let first = json!({
        "subject": "  Write docs ",
        "description": "All of them",
        "active_form": "Writing docs",
        "metadata": {"k": 1, "gone": true},
    });
    let created = board.text("TaskCreate", first).await;
    assert_eq!(
        created, "Task #1 created: Write docs",
        "the subject is trimmed"
    );
    let second = json!({"subject": "Ship", "description": "Tag it"});
    assert_eq!(
        board.text("TaskCreate", second).await,
        "Task #2 created: Ship"
    );
    let listed = board.text("TaskList", json!({})).await;
    assert_eq!(listed, "#1 [pending] Write docs\n#2 [pending] Ship");
    let got = board.text("TaskGet", json!({"id": "1"})).await;
    assert_eq!(got, "#1 [pending] Write docs\nAll of them");
    let missing = ("Task not found.".to_owned(), "not_found".to_owned());
    assert_eq!(board.error("TaskGet", json!({"id": "9"})).await, missing);

    let patch = json!({
        "id": "1",
        "status": "in_progress",
        "owner": "me",
        "add_blocks": ["2"],
        "metadata": {"gone": null},
    });
    let updated = board.text("TaskUpdate", patch).await;
    assert_eq!(updated, "Task #1 updated: [in_progress] Write docs");
    let (ns, sid) = ("ns", &SessionId::from("s1"));
    let stored = board.store.get_task(ns, sid, "1").await.unwrap().unwrap();
    assert_eq!(stored.active_form.as_deref(), Some("Writing docs"));
    assert_eq!(
        (stored.status, stored.owner.as_deref()),
        (TaskStatus::InProgress, Some("me"))
    );
    assert_eq!(Value::Object(stored.metadata), json!({"k": 1}));
    assert_eq!(stored.blocks, ["2"]);
    let blocked = board.store.get_task(ns, sid, "2").await.unwrap().unwrap();
    assert_eq!(blocked.blocked_by, ["1"]);
    let not_there = json!({"id": "9", "subject": "x"});
    assert_eq!(board.error("TaskUpdate", not_there).await, missing);

    let delete = json!({"id": "2", "status": "deleted", "subject": "ignored"});
    assert_eq!(board.text("TaskUpdate", delete).await, "Task #2 deleted.");
    let left = board.text("TaskList", json!({})).await;
    assert_eq!(left, "#1 [in_progress] Write docs");
    let stored = board.store.get_task(ns, sid, "1").await.unwrap().unwrap();
    assert!(
        stored.blocks.is_empty(),
        "the deleted task's edges went too"
    );
    let again = json!({"id": "9", "status": "deleted"});
    assert_eq!(
        board.text("TaskUpdate", again).await,
        "Task #9 deleted.",
        "as Python"
    );
}

#[tokio::test]
async fn validation_matches_python() {
    let board = Board::new().await;
    let invalid = |message: &str| (message.to_owned(), "invalid_input".to_owned());
    let cases = [
        (
            "TaskCreate",
            json!({"subject": "", "description": "d"}),
            "subject must be a non-empty string",
        ),
        (
            "TaskCreate",
            json!({"description": "d"}),
            "subject must be a non-empty string",
        ),
        (
            "TaskCreate",
            json!({"subject": "s", "description": ""}),
            "description must be a non-empty string",
        ),
        (
            "TaskCreate",
            json!({"subject": "s", "description": "d", "active_form": 5}),
            "active_form must be a string",
        ),
        (
            "TaskCreate",
            json!({"subject": "s", "description": "d", "metadata": []}),
            "metadata must be an object",
        ),
        (
            "TaskGet",
            json!({"id": ""}),
            "id must be a non-empty string",
        ),
        ("TaskUpdate", json!({}), "id must be a non-empty string"),
        (
            "TaskUpdate",
            json!({"id": "1", "owner": 3}),
            "owner must be a string",
        ),
        (
            "TaskUpdate",
            json!({"id": "1", "add_blocks": [1]}),
            "add_blocks must be a list of strings",
        ),
        (
            "TaskUpdate",
            json!({"id": "1", "remove_blocked_by": "2"}),
            "remove_blocked_by must be a list of strings",
        ),
        (
            "TaskUpdate",
            json!({"id": "1", "metadata": "x"}),
            "metadata must be an object",
        ),
        (
            "TaskUpdate",
            json!({"id": "1", "status": "done"}),
            "status must be one of pending, in_progress, completed, deleted",
        ),
    ];
    for (name, value, message) in cases {
        let text = value.to_string();
        assert_eq!(
            board.error(name, value).await,
            invalid(message),
            "{name} {text}"
        );
    }
    let first_wins = json!({"id": "1", "subject": 1, "status": "done"});
    let subject = invalid("subject must be a string");
    assert_eq!(
        board.error("TaskUpdate", first_wins).await,
        subject,
        "Python's order"
    );
    let bad_delete = json!({"id": "1", "status": "deleted", "add_blocks": [2]});
    let lists = invalid("add_blocks must be a list of strings");
    assert_eq!(
        board.error("TaskUpdate", bad_delete).await,
        lists,
        "checked on delete too"
    );
    let blank = json!({"subject": "   ", "description": "d"});
    assert_eq!(
        board.text("TaskCreate", blank).await,
        "Task #1 created: ",
        "as Python"
    );
    let nulls = json!({"id": "1", "subject": null, "add_blocks": null, "metadata": null});
    assert_eq!(
        board.text("TaskUpdate", nulls).await,
        "Task #1 updated: [pending] "
    );
}

#[tokio::test]
async fn summaries_and_sessions() {
    let board = Board::new().await;
    let summary = |name: &str, value: Value| board.tool(name).summarize(&input(value));
    assert_eq!(
        summary("TaskCreate", json!({"subject": "Ship"})),
        "Create task: Ship"
    );
    assert_eq!(summary("TaskList", json!({})), "List tasks");
    assert_eq!(summary("TaskGet", json!({"id": "3"})), "Get task 3");
    assert_eq!(summary("TaskUpdate", json!({"id": "3"})), "Update task 3");

    let first = json!({"subject": "a", "description": "d"});
    board.call("TaskCreate", first.clone()).await.unwrap();
    board
        .store
        .create("ns", &SessionId::from("s2"))
        .await
        .unwrap();
    let other = board.call_in("s2", "TaskList", json!({})).await.unwrap();
    assert_eq!(other.value, "No tasks found.", "tasks are per session");
    let nowhere = board.call_in("s9", "TaskCreate", first).await.unwrap_err();
    assert_eq!(
        nowhere.code.as_deref(),
        Some("store"),
        "{}",
        nowhere.message
    );
}
