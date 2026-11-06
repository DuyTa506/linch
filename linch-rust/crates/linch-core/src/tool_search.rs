//! Tool search: past a size, a session is offered only the tools that
//! always load and `tool_search`, and the model searches for the rest.
//!
//! Turn it on with [`crate::AgentBuilder::tool_search`]. While the agent
//! has more tools than [`ToolSearch::max_tools`] (`tool_search` aside), a
//! new session's frozen prefix holds only the tools whose
//! [`crate::Tool::always_load`] says so, plus `tool_search`; while it has
//! more model-invocable skills than [`ToolSearch::max_skills`], the skill
//! listing names them and lists none. Below both, every tool is offered
//! inline and `tool_search` is not.
//!
//! The model searches by keywords, or by names with `select:a,b`. The
//! result holds the schemas and skill descriptions found; the runner reads
//! their names back from the logged result, the way a skill load is read
//! from a `Skill` result, and tells them at the tail as added tools and
//! skills in a [`crate::CatalogChangeBlock`]. So a found tool is offered
//! from the next request on, never in the response that searched, and
//! the loads are in the session log: they cost no write and outlive a
//! crash or a compaction. The prefix stays byte for byte.
//!
//! A call of a registered tool the session was not offered is refused:
//! `Tool 'X' is deferred; call tool_search with select:X first`.

#[cfg(test)]
mod tests;
mod tool;

use std::collections::BTreeSet;

use serde::{Deserialize, Serialize};

use crate::catalog::Live;
use crate::message::{ContentBlock, Message, ToolResultPart};
use crate::registry::ToolSnapshot;

pub(crate) use tool::SearchTool;

/// The name of the tool search tool.
pub const TOOL_SEARCH_TOOL: &str = "tool_search";

/// The refusal of a `tool_search` call when the session is not offered it.
pub(crate) const NOT_OFFERED: &str = "No deferred catalog is offered on this turn.";

const HEADER: &str = "Search deferred tools and skills by keywords or select:a,b. Returns tool \
                      schemas and skill descriptions; tools load for the next request.";

/// How large a catalog may grow before tool search defers part of it.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct ToolSearch {
    /// The most tools, `tool_search` aside, offered inline; past it only
    /// the tools that always load are, and the rest wait for a search.
    pub max_tools: usize,
    /// The most model-invocable skills listed inline; past it the listing
    /// only names them, and a search loads their descriptions.
    pub max_skills: usize,
}

impl Default for ToolSearch {
    /// Python Linch's thresholds: 30 tools, 50 skills.
    fn default() -> Self {
        Self {
            max_tools: 30,
            max_skills: 50,
        }
    }
}

/// The tools and skills one batch's searches found.
#[derive(Debug, Default)]
pub(crate) struct Loads {
    pub(crate) tools: BTreeSet<String>,
    pub(crate) skills: BTreeSet<String>,
}

impl Loads {
    pub(crate) fn is_empty(&self) -> bool {
        self.tools.is_empty() && self.skills.is_empty()
    }

    /// What the `tool_search` calls of `response` found, read from their
    /// results among `blocks`. A failed or unreadable result found nothing.
    pub(crate) fn of(response: &Message, blocks: &[ContentBlock]) -> Self {
        let searches: Vec<&str> = (response.tool_uses())
            .filter(|call| call.name == TOOL_SEARCH_TOOL)
            .map(|call| call.id.as_str())
            .collect();
        let mut loads = Self::default();
        for block in blocks {
            let ContentBlock::ToolResult(result) = block else {
                continue;
            };
            if result.is_error || !searches.contains(&result.tool_use_id.as_str()) {
                continue;
            }
            let Some(ToolResultPart::Text(text)) = result.content.first() else {
                continue;
            };
            let Ok(found) = serde_json::from_str::<Found>(&text.text) else {
                continue;
            };
            loads.tools.extend(found.tools.into_iter().map(|t| t.name));
            loads
                .skills
                .extend(found.skills.into_iter().map(|s| s.name));
        }
        loads
    }
}

/// A search result, read back for the names only.
#[derive(Deserialize)]
struct Found {
    tools: Vec<Named>,
    skills: Vec<Named>,
}

#[derive(Deserialize)]
struct Named {
    name: String,
}

/// Narrow `live`, all that `snapshot` registers, to what a session is
/// offered under `search`. `told` and `told_skill` say whether the model
/// was told of a tool or a skill already (or a search just found it);
/// those stay offered. Sets [`Live::deferred_skills`] when skills defer.
pub(crate) fn offer(
    search: &ToolSearch,
    snapshot: &ToolSnapshot,
    live: &mut Live,
    told: &dyn Fn(&str) -> bool,
    told_skill: &dyn Fn(&str) -> bool,
) {
    let candidates = (live.tools.iter())
        .filter(|tool| tool.name != TOOL_SEARCH_TOOL)
        .count();
    let defer_tools = candidates > search.max_tools;
    let defer_skills = live.skills.len() > search.max_skills;
    if !defer_tools && !defer_skills {
        live.tools.retain(|tool| tool.name != TOOL_SEARCH_TOOL);
        return;
    }
    let always = |name: &str| (snapshot.resolve(name)).is_some_and(|t| t.tool().always_load());
    let mut tools: Vec<&str> = (live.tools.iter())
        .map(|tool| tool.name.as_str())
        .filter(|name| defer_tools && *name != TOOL_SEARCH_TOOL && !always(name))
        .collect();
    tools.sort_unstable();
    let skills: Vec<String> = match defer_skills {
        true => (live.skills.iter()).map(|s| s.stamp.name.clone()).collect(),
        false => Vec::new(),
    };
    let description = format!(
        "{HEADER}\nDeferred tools: {}\nDeferred skills: {}",
        tools.join(", "),
        skills.join(", ")
    );
    live.tools.retain(|tool| {
        let name = tool.name.as_str();
        name == TOOL_SEARCH_TOOL || !defer_tools || always(name) || told(name)
    });
    for tool in &mut live.tools {
        if tool.name == TOOL_SEARCH_TOOL {
            tool.description.clone_from(&description);
        }
    }
    if defer_skills {
        live.skills.retain(|skill| told_skill(&skill.stamp.name));
        live.deferred_skills = Some(skills);
    }
}
