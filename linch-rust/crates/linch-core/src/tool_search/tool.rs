//! The `tool_search` tool: match a query against the agent's tools and
//! skills, and return what it found.

use std::collections::BTreeSet;

use async_trait::async_trait;
use serde_json::json;

use super::TOOL_SEARCH_TOOL;
use crate::json::{JsonObject, JsonValue};
use crate::subagents::Handle;
use crate::tool::{Tool, ToolContext, ToolOutput, ToolOutputError, ToolResult, ToolScope};

/// The most entries a keyword search returns, of tools and of skills each.
const MAX_MATCHES: usize = 5;

/// Searches the tools and the model-invocable skills of its agent. The
/// description the model reads is the catalog's, which lists what is
/// deferred (see [`super::offer`]).
pub(crate) struct SearchTool {
    agent: Handle,
    schema: JsonObject,
}

impl SearchTool {
    pub(crate) fn new(agent: Handle) -> Self {
        let schema = json!({
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        });
        Self {
            agent,
            schema: schema.as_object().cloned().unwrap_or_default(),
        }
    }
}

#[async_trait]
impl Tool for SearchTool {
    fn name(&self) -> &str {
        TOOL_SEARCH_TOOL
    }

    fn description(&self) -> &str {
        "Search deferred tools and skills."
    }

    fn input_schema(&self) -> &JsonObject {
        &self.schema
    }

    fn scope(&self) -> ToolScope {
        ToolScope::Read
    }

    fn always_load(&self) -> bool {
        true
    }

    fn validate(&self, mut input: JsonObject) -> Result<JsonObject, ToolOutputError> {
        let query = (input.get("query").and_then(JsonValue::as_str)).map(str::trim);
        let Some(query) = query.filter(|query| !query.is_empty()) else {
            return Err(ToolOutputError::new("query must be a non-empty string"));
        };
        let query = JsonValue::String(query.to_owned());
        input.insert("query".into(), query);
        Ok(input)
    }

    fn summarize(&self, input: &JsonObject) -> String {
        let query = input.get("query").and_then(JsonValue::as_str);
        format!("tool_search({})", query.unwrap_or_default())
    }

    async fn execute(&self, input: JsonObject, _ctx: ToolContext) -> ToolResult {
        let Some(agent) = self.agent.get().and_then(|agent| agent.upgrade()) else {
            return Err(ToolOutputError::new("the agent is gone"));
        };
        let query = input.get("query").and_then(JsonValue::as_str);
        let query = query.unwrap_or_default();
        let mut tools = agent.snapshot().definitions();
        tools.retain(|tool| tool.name != TOOL_SEARCH_TOOL);
        let tools = found(query, &tools, |t| (&t.name, &t.description));
        let skills = agent.listed_skills();
        let skills = found(query, &skills, |s| (&s.stamp.name, &s.description));
        let skills: Vec<JsonValue> = (skills.iter())
            .map(|s| json!({"name": s.stamp.name, "description": s.description}))
            .collect();
        Ok(ToolOutput::new(json!({"tools": tools, "skills": skills})))
    }
}

/// The entries `query` finds, as Python Linch matches: `select:a,b` takes
/// the entries of those names in their order; otherwise each entry scores
/// the query's words found in its name and description, and the best
/// [`MAX_MATCHES`] that score at all come first, ties by name.
pub(super) fn found<'a, T>(
    query: &str,
    entries: &'a [T],
    key: impl Fn(&T) -> (&str, &str),
) -> Vec<&'a T> {
    if let Some(names) = query.strip_prefix("select:") {
        let names: BTreeSet<&str> = names.split(',').map(str::trim).collect();
        let named = |entry: &&T| names.contains(key(entry).0);
        return entries.iter().filter(named).collect();
    }
    let tokens = words(query);
    let mut scored: Vec<(usize, &T)> = (entries.iter())
        .map(|entry| {
            let (name, description) = key(entry);
            let text = words(&format!("{name} {description}"));
            (text.intersection(&tokens).count(), entry)
        })
        .collect();
    scored.sort_by(|(a, x), (b, y)| b.cmp(a).then_with(|| key(x).0.cmp(key(y).0)));
    (scored.into_iter().take(MAX_MATCHES))
        .filter(|(score, _)| *score > 0)
        .map(|(_, entry)| entry)
        .collect()
}

/// The lower-cased runs of ASCII letters and digits in `text`.
fn words(text: &str) -> BTreeSet<String> {
    let text = text.to_lowercase();
    let word = |c: char| c.is_ascii_lowercase() || c.is_ascii_digit();
    (text.split(|c: char| !word(c)))
        .filter(|w| !w.is_empty())
        .map(str::to_owned)
        .collect()
}
