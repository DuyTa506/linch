//! The session task tools `TaskCreate`, `TaskList`, `TaskGet` and
//! `TaskUpdate`, over a [`TaskStore`] and the namespace of the sessions.
//!
//! Names, input schemas, scopes and the texts the model sees are Python's.
//! Tasks are session data: only the calls and their results enter the
//! session log. They belong to the session of the root run of the agent
//! tree, so a subagent's child session shares its parent's tasks.

use std::sync::Arc;

use async_trait::async_trait;
use linch_core::{
    JsonObject, JsonValue, NewTask, RunStore, SessionId, StoreError, Task, TaskPatch, TaskStatus,
    TaskStore, Tool, ToolContext, ToolOutput, ToolOutputError, ToolResult, ToolScope,
};
use serde_json::json;

use crate::input::{coded, invalid, optional_string, schema, string};

/// The tool names, in [`task_tools`] order.
pub const TASK_TOOL_NAMES: [&str; 4] = ["TaskCreate", "TaskList", "TaskGet", "TaskUpdate"];

/// `TaskCreate`, `TaskList`, `TaskGet` and `TaskUpdate` over `store`, for
/// the sessions of `namespace`. `runs` finds the root run of a call's tree.
pub fn task_tools(
    store: Arc<dyn TaskStore>,
    runs: Arc<dyn RunStore>,
    namespace: &str,
) -> Vec<Arc<dyn Tool>> {
    vec![
        Arc::new(TaskCreate::new(
            Arc::clone(&store),
            Arc::clone(&runs),
            namespace,
        )),
        Arc::new(TaskList::new(
            Arc::clone(&store),
            Arc::clone(&runs),
            namespace,
        )),
        Arc::new(TaskGet::new(
            Arc::clone(&store),
            Arc::clone(&runs),
            namespace,
        )),
        Arc::new(TaskUpdate::new(store, runs, namespace)),
    ]
}

/// A store and the namespace its sessions are in.
struct Tasks {
    store: Arc<dyn TaskStore>,
    runs: Arc<dyn RunStore>,
    namespace: String,
}

impl Tasks {
    fn new(store: Arc<dyn TaskStore>, runs: Arc<dyn RunStore>, namespace: &str) -> Self {
        let namespace = namespace.to_owned();
        Self {
            store,
            runs,
            namespace,
        }
    }

    /// The session that owns the tasks of the call in `ctx`: the session of
    /// the root run of its tree. A call whose run the store does not know
    /// uses its own session.
    async fn session(&self, ctx: &ToolContext) -> Result<SessionId, ToolOutputError> {
        let mut session = ctx.session_id.clone();
        let mut run = ctx.run_id.clone();
        loop {
            let record = match self.runs.load(&self.namespace, &run).await {
                Ok(record) => record,
                Err(StoreError::NotFound { .. }) => return Ok(session),
                Err(error) => return Err(store_error(error)),
            };
            session = record.session_id;
            match record.parent_run_id {
                Some(parent) => run = parent,
                None => return Ok(session),
            }
        }
    }
}

fn store_error(error: StoreError) -> ToolOutputError {
    coded(error.to_string(), "store")
}

fn not_found() -> ToolOutputError {
    coded("Task not found.", "not_found")
}

fn text(text: String) -> ToolResult {
    Ok(ToolOutput::new(text))
}

/// A required string that is not empty.
fn required<'a>(input: &'a JsonObject, key: &str) -> Result<&'a str, ToolOutputError> {
    match input.get(key) {
        Some(JsonValue::String(text)) if !text.is_empty() => Ok(text),
        _ => Err(invalid(format!("{key} must be a non-empty string"))),
    }
}

fn optional_object(input: &JsonObject, key: &str) -> Result<JsonObject, ToolOutputError> {
    match input.get(key) {
        None | Some(JsonValue::Null) => Ok(JsonObject::new()),
        Some(JsonValue::Object(object)) => Ok(object.clone()),
        Some(_) => Err(invalid(format!("{key} must be an object"))),
    }
}

fn optional_ids(input: &JsonObject, key: &str) -> Result<Vec<String>, ToolOutputError> {
    let ids = match input.get(key) {
        None | Some(JsonValue::Null) => return Ok(Vec::new()),
        Some(JsonValue::Array(ids)) => ids,
        Some(_) => return Err(invalid(format!("{key} must be a list of strings"))),
    };
    let id = |value: &JsonValue| value.as_str().map(str::to_owned);
    let ids: Option<Vec<String>> = ids.iter().map(id).collect();
    ids.ok_or_else(|| invalid(format!("{key} must be a list of strings")))
}

fn owned(input: &JsonObject, key: &str) -> Result<Option<String>, ToolOutputError> {
    Ok(optional_string(input, key)?.map(str::to_owned))
}

/// One line per task: `#id [status] subject`.
fn line(task: &Task) -> String {
    format!("#{} [{}] {}", task.id, task.status.as_str(), task.subject)
}

fn field<'a>(input: &'a JsonObject, key: &str) -> &'a str {
    input.get(key).and_then(JsonValue::as_str).unwrap_or("")
}

/// Create a pending task in the current session. Scope `write`.
pub struct TaskCreate {
    tasks: Tasks,
    schema: JsonObject,
}

impl TaskCreate {
    /// A `TaskCreate` over `store`, for the sessions of `namespace`.
    pub fn new(store: Arc<dyn TaskStore>, runs: Arc<dyn RunStore>, namespace: &str) -> Self {
        let schema = schema(json!({
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
        }));
        let tasks = Tasks::new(store, runs, namespace);
        Self { tasks, schema }
    }
}

fn new_task(input: &JsonObject) -> Result<NewTask, ToolOutputError> {
    Ok(NewTask {
        subject: string(input, "subject")?.to_owned(),
        description: string(input, "description")?.to_owned(),
        active_form: owned(input, "active_form")?,
        metadata: optional_object(input, "metadata")?,
    })
}

#[async_trait]
impl Tool for TaskCreate {
    fn name(&self) -> &str {
        TASK_TOOL_NAMES[0]
    }

    fn description(&self) -> &str {
        "Create a new persistent task in the current session."
    }

    fn input_schema(&self) -> &JsonObject {
        &self.schema
    }

    fn scope(&self) -> ToolScope {
        ToolScope::Write
    }

    /// Checks the fields and trims the subject, which may leave it empty.
    fn validate(&self, mut input: JsonObject) -> Result<JsonObject, ToolOutputError> {
        required(&input, "subject")?;
        required(&input, "description")?;
        let subject = new_task(&input)?.subject.trim().to_owned();
        input.insert("subject".into(), subject.into());
        Ok(input)
    }

    fn summarize(&self, input: &JsonObject) -> String {
        format!("Create task: {}", field(input, "subject"))
    }

    async fn execute(&self, input: JsonObject, ctx: ToolContext) -> ToolResult {
        let Tasks {
            store, namespace, ..
        } = &self.tasks;
        let session = self.tasks.session(&ctx).await?;
        let new = new_task(&input)?;
        let task = store.create_task(namespace, &session, new).await;
        let task = task.map_err(store_error)?;
        text(format!("Task #{} created: {}", task.id, task.subject))
    }
}

/// List the current session's tasks. Scope `read`.
pub struct TaskList {
    tasks: Tasks,
    schema: JsonObject,
}

impl TaskList {
    /// A `TaskList` over `store`, for the sessions of `namespace`.
    pub fn new(store: Arc<dyn TaskStore>, runs: Arc<dyn RunStore>, namespace: &str) -> Self {
        let schema = schema(json!({"type": "object", "properties": {}}));
        let tasks = Tasks::new(store, runs, namespace);
        Self { tasks, schema }
    }
}

#[async_trait]
impl Tool for TaskList {
    fn name(&self) -> &str {
        TASK_TOOL_NAMES[1]
    }

    fn description(&self) -> &str {
        "List all tasks for the current session."
    }

    fn input_schema(&self) -> &JsonObject {
        &self.schema
    }

    fn scope(&self) -> ToolScope {
        ToolScope::Read
    }

    fn summarize(&self, _input: &JsonObject) -> String {
        "List tasks".to_owned()
    }

    async fn execute(&self, _input: JsonObject, ctx: ToolContext) -> ToolResult {
        let Tasks {
            store, namespace, ..
        } = &self.tasks;
        let session = self.tasks.session(&ctx).await?;
        let tasks = store.list_tasks(namespace, &session).await;
        let tasks = tasks.map_err(store_error)?;
        if tasks.is_empty() {
            return text("No tasks found.".to_owned());
        }
        text(tasks.iter().map(line).collect::<Vec<_>>().join("\n"))
    }
}

/// Show one task of the current session. Scope `read`.
pub struct TaskGet {
    tasks: Tasks,
    schema: JsonObject,
}

impl TaskGet {
    /// A `TaskGet` over `store`, for the sessions of `namespace`.
    pub fn new(store: Arc<dyn TaskStore>, runs: Arc<dyn RunStore>, namespace: &str) -> Self {
        let schema = schema(json!({
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        }));
        let tasks = Tasks::new(store, runs, namespace);
        Self { tasks, schema }
    }
}

#[async_trait]
impl Tool for TaskGet {
    fn name(&self) -> &str {
        TASK_TOOL_NAMES[2]
    }

    fn description(&self) -> &str {
        "Get one task by id."
    }

    fn input_schema(&self) -> &JsonObject {
        &self.schema
    }

    fn scope(&self) -> ToolScope {
        ToolScope::Read
    }

    fn validate(&self, input: JsonObject) -> Result<JsonObject, ToolOutputError> {
        required(&input, "id")?;
        Ok(input)
    }

    fn summarize(&self, input: &JsonObject) -> String {
        format!("Get task {}", field(input, "id"))
    }

    async fn execute(&self, input: JsonObject, ctx: ToolContext) -> ToolResult {
        let Tasks {
            store, namespace, ..
        } = &self.tasks;
        let session = self.tasks.session(&ctx).await?;
        let id = required(&input, "id")?;
        let task = store.get_task(namespace, &session, id).await;
        let task = task.map_err(store_error)?.ok_or_else(not_found)?;
        text(format!("{}\n{}", line(&task), task.description))
    }
}

/// Change, or with status `deleted` delete, one task of the current
/// session. Scope `write`.
pub struct TaskUpdate {
    tasks: Tasks,
    schema: JsonObject,
}

impl TaskUpdate {
    /// A `TaskUpdate` over `store`, for the sessions of `namespace`.
    pub fn new(store: Arc<dyn TaskStore>, runs: Arc<dyn RunStore>, namespace: &str) -> Self {
        let ids = json!({"type": "array", "items": {"type": "string"}});
        let schema = schema(json!({
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
        }));
        let tasks = Tasks::new(store, runs, namespace);
        Self { tasks, schema }
    }
}

/// The patch a `TaskUpdate` input asks for, and whether it deletes the
/// task instead; every field is checked, in Python's order.
fn change(input: &JsonObject) -> Result<(TaskPatch, bool), ToolOutputError> {
    let subject = owned(input, "subject")?;
    let description = owned(input, "description")?;
    let active_form = owned(input, "active_form")?;
    let status = owned(input, "status")?;
    let owner = owned(input, "owner")?;
    let delete = status.as_deref() == Some("deleted");
    let status = match status.filter(|_| !delete) {
        None => None,
        Some(name) => Some(TaskStatus::parse(&name).ok_or_else(|| {
            invalid("status must be one of pending, in_progress, completed, deleted")
        })?),
    };
    let patch = TaskPatch {
        subject,
        description,
        active_form,
        status,
        owner,
        add_blocks: optional_ids(input, "add_blocks")?,
        add_blocked_by: optional_ids(input, "add_blocked_by")?,
        remove_blocks: optional_ids(input, "remove_blocks")?,
        remove_blocked_by: optional_ids(input, "remove_blocked_by")?,
        metadata: optional_object(input, "metadata")?,
    };
    Ok((patch, delete))
}

#[async_trait]
impl Tool for TaskUpdate {
    fn name(&self) -> &str {
        TASK_TOOL_NAMES[3]
    }

    fn description(&self) -> &str {
        "Patch one task."
    }

    fn input_schema(&self) -> &JsonObject {
        &self.schema
    }

    fn scope(&self) -> ToolScope {
        ToolScope::Write
    }

    fn validate(&self, input: JsonObject) -> Result<JsonObject, ToolOutputError> {
        required(&input, "id")?;
        change(&input)?;
        Ok(input)
    }

    fn summarize(&self, input: &JsonObject) -> String {
        format!("Update task {}", field(input, "id"))
    }

    async fn execute(&self, input: JsonObject, ctx: ToolContext) -> ToolResult {
        let Tasks {
            store, namespace, ..
        } = &self.tasks;
        let session = self.tasks.session(&ctx).await?;
        let id = required(&input, "id")?;
        let (patch, delete) = change(&input)?;
        if delete {
            // Deleting a missing task reports the same, as Python does.
            let deleted = store.delete_task(namespace, &session, id).await;
            deleted.map_err(store_error)?;
            return text(format!("Task #{id} deleted."));
        }
        let task = store.update_task(namespace, &session, id, patch).await;
        let task = task.map_err(store_error)?.ok_or_else(not_found)?;
        text(format!(
            "Task #{} updated: [{}] {}",
            task.id,
            task.status.as_str(),
            task.subject
        ))
    }
}
