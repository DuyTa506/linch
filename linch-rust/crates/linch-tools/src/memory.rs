//! The memory tools `MemorySave`, `MemorySearch` and `MemoryDelete`, over a
//! [`MemoryStore`] the host binds.
//!
//! The model chooses only the text, the tags, the query and the id of what
//! to delete. The host chooses everything else: the store (so the
//! namespace) and the scope, which is the agent's shared memory or the
//! calling run's principal ([`MemoryScope`]). Neither is a tool input, so
//! no input reaches another agent's or another principal's memories.
//!
//! A store failure never reaches the model as the store worded it, because
//! a backend's message can name its address: the model sees a fixed text and
//! the code `store`.

use std::sync::Arc;

use async_trait::async_trait;
use linch_core::memory::{
    DEFAULT_SEARCH_LIMIT, MAX_CONTENT_BYTES, MAX_QUERY_BYTES, MAX_SEARCH_LIMIT, MAX_TAG_BYTES,
    MAX_TAGS, Memory, MemoryError, MemoryStore, NewMemory, principal_scope,
};
use linch_core::{
    JsonObject, JsonValue, Principal, Tool, ToolContext, ToolOutput, ToolOutputError, ToolResult,
    ToolScope,
};
use serde::{Deserialize, Serialize};
use serde_json::json;

use crate::input::{coded, invalid, optional_count, schema, string};

/// The tool names, in [`memory_tools`] order.
pub const MEMORY_TOOL_NAMES: [&str; 3] = ["MemorySave", "MemorySearch", "MemoryDelete"];

/// Whose memories an agent reads and writes.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum MemoryScope {
    /// One memory every session of the agent's namespace shares.
    #[default]
    Agent,
    /// One memory per principal (tenant and user): a run reads and writes
    /// only its own principal's, and a run without a principal has none.
    Principal,
}

/// A run in a [`MemoryScope::Principal`] agent has no principal.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct NoPrincipal;

impl MemoryScope {
    /// The store scope of a run acting for `principal`: `None` for the
    /// shared memory, else the principal's key.
    ///
    /// Raises:
    ///     NoPrincipal: The scope is `Principal` and the run has none. The
    ///         caller must not fall back to the shared memory.
    pub fn key(self, principal: Option<&Principal>) -> Result<Option<String>, NoPrincipal> {
        match (self, principal) {
            (Self::Agent, _) => Ok(None),
            (Self::Principal, Some(principal)) => Ok(Some(principal_scope(principal))),
            (Self::Principal, None) => Err(NoPrincipal),
        }
    }
}

/// `MemorySave`, `MemorySearch` and `MemoryDelete` over `store`, reading
/// and writing the memories of `scope`.
pub fn memory_tools(store: Arc<dyn MemoryStore>, scope: MemoryScope) -> Vec<Arc<dyn Tool>> {
    let bound = Bound { store, scope };
    vec![
        Arc::new(MemorySave::new(bound.clone())),
        Arc::new(MemorySearch::new(bound.clone())),
        Arc::new(MemoryDelete::new(bound)),
    ]
}

#[derive(Clone)]
struct Bound {
    store: Arc<dyn MemoryStore>,
    scope: MemoryScope,
}

impl Bound {
    fn scope_of(&self, ctx: &ToolContext) -> Result<Option<String>, ToolOutputError> {
        self.scope.key(ctx.principal.as_ref()).map_err(|_| {
            coded(
                "memories are kept per principal and this run has none",
                "not_allowed",
            )
        })
    }
}

fn store_error(error: MemoryError) -> ToolOutputError {
    match error {
        MemoryError::LimitReached { max } => coded(
            format!("the memory limit of {max} is reached; delete a memory first"),
            "limit_reached",
        ),
        MemoryError::Backend(_) => coded("the memory store is unavailable", "store"),
    }
}

fn text(text: String) -> ToolResult {
    Ok(ToolOutput::new(text))
}

fn tags(input: &JsonObject) -> Result<Vec<String>, ToolOutputError> {
    match input.get("tags") {
        None | Some(JsonValue::Null) => Ok(Vec::new()),
        Some(JsonValue::Array(items)) => items
            .iter()
            .map(|item| match item {
                JsonValue::String(tag) => Ok(tag.clone()),
                _ => Err(invalid("tags must be strings")),
            })
            .collect(),
        Some(_) => Err(invalid("tags must be an array of strings")),
    }
}

fn new_memory(input: &JsonObject, key: &str) -> Result<NewMemory, ToolOutputError> {
    NewMemory::new(key, string(input, "content")?, &tags(input)?).map_err(|error| invalid(error.0))
}

/// Save a fact for later runs. Scope `write`.
pub struct MemorySave {
    bound: Bound,
    schema: JsonObject,
}

impl MemorySave {
    fn new(bound: Bound) -> Self {
        let schema = schema(json!({
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The fact to remember, written so it makes sense on its own.",
                    "minLength": 1,
                    "maxLength": MAX_CONTENT_BYTES,
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1, "maxLength": MAX_TAG_BYTES},
                    "maxItems": MAX_TAGS,
                    "description": "Short labels that make the memory easier to find.",
                },
            },
            "required": ["content"],
            "additionalProperties": false,
        }));
        Self { bound, schema }
    }
}

#[async_trait]
impl Tool for MemorySave {
    fn name(&self) -> &str {
        MEMORY_TOOL_NAMES[0]
    }

    fn description(&self) -> &str {
        "Save a fact to long-term memory so that later runs can find it with MemorySearch. \
         Save what stays useful: preferences, decisions, names, outcomes."
    }

    fn input_schema(&self) -> &JsonObject {
        &self.schema
    }

    fn scope(&self) -> ToolScope {
        ToolScope::Write
    }

    fn validate(&self, input: JsonObject) -> Result<JsonObject, ToolOutputError> {
        new_memory(&input, "validate")?;
        Ok(input)
    }

    fn summarize(&self, input: &JsonObject) -> String {
        let content = input.get("content").and_then(JsonValue::as_str);
        let head: String = content.unwrap_or_default().chars().take(60).collect();
        format!("Save memory: {head}")
    }

    async fn execute(&self, input: JsonObject, ctx: ToolContext) -> ToolResult {
        let scope = self.bound.scope_of(&ctx)?;
        let memory = new_memory(&input, &ctx.idempotency_key())?;
        let saved = self.bound.store.save(scope.as_deref(), memory).await;
        let saved = saved.map_err(store_error)?;
        let mut output = ToolOutput::new(format!("Saved memory {}", saved.id));
        output.metadata.insert("id".into(), saved.id.into());
        Ok(output)
    }
}

/// Find saved facts that match a query. Scope `read`.
pub struct MemorySearch {
    bound: Bound,
    schema: JsonObject,
}

impl MemorySearch {
    fn new(bound: Bound) -> Self {
        let schema = schema(json!({
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Words to look for; a memory that has any of them matches, \
                                    and one that has more of them comes first.",
                    "minLength": 1,
                    "maxLength": MAX_QUERY_BYTES,
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_SEARCH_LIMIT,
                    "description": format!("Most memories to return; {DEFAULT_SEARCH_LIMIT} when absent."),
                },
            },
            "required": ["query"],
            "additionalProperties": false,
        }));
        Self { bound, schema }
    }
}

fn limit(input: &JsonObject) -> Result<usize, ToolOutputError> {
    let limit = optional_count(input, "limit")?.unwrap_or(DEFAULT_SEARCH_LIMIT as u64);
    match usize::try_from(limit) {
        Ok(limit) if (1..=MAX_SEARCH_LIMIT).contains(&limit) => Ok(limit),
        _ => Err(invalid(format!("limit must be 1 to {MAX_SEARCH_LIMIT}"))),
    }
}

fn query(input: &JsonObject) -> Result<&str, ToolOutputError> {
    let query = string(input, "query")?;
    if query.len() > MAX_QUERY_BYTES {
        return Err(invalid(format!("query is over {MAX_QUERY_BYTES} bytes")));
    }
    Ok(query)
}

/// A memory as the model reads it in a search result.
pub(crate) fn render(memory: &Memory) -> String {
    let tags = match memory.tags.is_empty() {
        true => String::new(),
        false => format!(" (tags: {})", memory.tags.join(", ")),
    };
    format!("[memory:{}] {}{tags}", memory.id, memory.content)
}

#[async_trait]
impl Tool for MemorySearch {
    fn name(&self) -> &str {
        MEMORY_TOOL_NAMES[1]
    }

    fn description(&self) -> &str {
        "Search long-term memory for facts saved earlier, in this or a past run."
    }

    fn input_schema(&self) -> &JsonObject {
        &self.schema
    }

    fn scope(&self) -> ToolScope {
        ToolScope::Read
    }

    fn validate(&self, input: JsonObject) -> Result<JsonObject, ToolOutputError> {
        query(&input)?;
        limit(&input)?;
        Ok(input)
    }

    fn summarize(&self, input: &JsonObject) -> String {
        let query = input.get("query").and_then(JsonValue::as_str);
        let head: String = query.unwrap_or_default().chars().take(60).collect();
        format!("Search memory: {head}")
    }

    async fn execute(&self, input: JsonObject, ctx: ToolContext) -> ToolResult {
        let scope = self.bound.scope_of(&ctx)?;
        let found = (self.bound.store)
            .search(scope.as_deref(), query(&input)?, limit(&input)?)
            .await;
        let found = found.map_err(store_error)?;
        if found.is_empty() {
            return text("No memories found.".to_owned());
        }
        text(found.iter().map(render).collect::<Vec<_>>().join("\n"))
    }
}

/// Forget one saved fact. Scope `write`.
pub struct MemoryDelete {
    bound: Bound,
    schema: JsonObject,
}

impl MemoryDelete {
    fn new(bound: Bound) -> Self {
        let schema = schema(json!({
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": "The id of a memory, as MemorySearch shows it.",
                    "minLength": 1,
                    "maxLength": 128,
                },
            },
            "required": ["id"],
            "additionalProperties": false,
        }));
        Self { bound, schema }
    }
}

#[async_trait]
impl Tool for MemoryDelete {
    fn name(&self) -> &str {
        MEMORY_TOOL_NAMES[2]
    }

    fn description(&self) -> &str {
        "Delete a fact from long-term memory when it is wrong or no longer true."
    }

    fn input_schema(&self) -> &JsonObject {
        &self.schema
    }

    fn scope(&self) -> ToolScope {
        ToolScope::Write
    }

    fn validate(&self, input: JsonObject) -> Result<JsonObject, ToolOutputError> {
        string(&input, "id")?;
        Ok(input)
    }

    fn summarize(&self, input: &JsonObject) -> String {
        let id = input.get("id").and_then(JsonValue::as_str).unwrap_or("");
        format!("Delete memory {id}")
    }

    async fn execute(&self, input: JsonObject, ctx: ToolContext) -> ToolResult {
        let scope = self.bound.scope_of(&ctx)?;
        let id = string(&input, "id")?;
        let deleted = self.bound.store.delete(scope.as_deref(), id).await;
        if deleted.map_err(store_error)? {
            text(format!("Deleted memory {id}"))
        } else {
            Err(coded(format!("No memory {id}"), "not_found"))
        }
    }
}
