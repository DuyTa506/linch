//! The session catalog: the frozen prefix every request of a session
//! starts with, and the changes the model was told about since.
//!
//! A session's first prompt is logged together with a [`PrefixEntry`]:
//! the system blocks and the tools the agent offers at that moment. Every
//! request of the session then sends exactly those system blocks and
//! those tools first, so the provider's prompt cache keeps them whatever
//! changes in the registry, the skills or the subagents later. Only a new
//! session takes the live catalog into its own prefix.
//!
//! A later change goes at the conversation's tail: a
//! [`CatalogChangeBlock`] in the next user message the run logs anyway
//! (the prompt, the tool results, stop feedback), so it costs no write.
//! The catalog folds every such block of the full history, in order:
//! - a removed tool keeps its row in the request, so the cached tools
//!   stay put; a call to it is refused as not registered;
//! - an added tool's definition is appended to the request's tools, which
//!   costs the provider's cache once (an adapter that can add tools in
//!   place renders it at the notice instead);
//! - a changed tool replaces its row only when its input schema changed;
//!   a new description alone is told in the notice text.

mod notice;
#[cfg(test)]
mod tests;

use std::collections::BTreeMap;

use crate::agent::AgentInner;
use crate::canonical::Digest;
use crate::message::{CatalogChangeBlock, ContentBlock, Message, SkillStamp, SystemBlock};
use crate::provider::ToolDefinition;
use crate::session_log::{LogEntry, PrefixEntry, SessionLog};
use crate::tool_search::{self, Loads};

/// The catalog state of one session, folded from its log.
#[derive(Debug, Clone)]
pub(crate) struct Catalog {
    prefix: PrefixEntry,
    /// The tools of every request: the prefix's, a row replaced in place
    /// when its input schema changed, then each tool added later.
    rows: Vec<ToolDefinition>,
    /// The tools the model was last told are offered, in order.
    told: Vec<ToolDefinition>,
    /// The skills the model was last told about, by name.
    skills: BTreeMap<String, Digest>,
}

/// The catalog the agent offers now.
pub(crate) struct Live {
    /// The tools a new request would offer, in order.
    pub(crate) tools: Vec<ToolDefinition>,
    /// The skills the model may invoke, sorted by name.
    pub(crate) skills: Vec<LiveSkill>,
    /// Every skill's name, when tool search defers the skills: a new
    /// session's listing names them instead of listing them.
    pub(crate) deferred_skills: Option<Vec<String>>,
}

/// One skill of the live catalog.
#[derive(Debug, Clone)]
pub(crate) struct LiveSkill {
    pub(crate) stamp: SkillStamp,
    /// The line a notice shows for it.
    pub(crate) description: String,
}

impl Catalog {
    /// The catalog of `log`; `None` before its prefix is logged.
    pub(crate) fn of(log: &SessionLog) -> Option<Self> {
        Self::of_entries(log.entries())
    }

    /// The catalog of stored `entries`: their first prefix, then every
    /// historical message's changes; `None` before a prefix.
    pub(crate) fn of_entries(entries: &[LogEntry]) -> Option<Self> {
        let prefix = entries.iter().find_map(|entry| match entry {
            LogEntry::Prefix(prefix) => Some(prefix),
            _ => None,
        })?;
        let mut catalog = Self::new(prefix.clone());
        for entry in entries {
            if let LogEntry::Message(entry) = entry
                && entry.historical
            {
                catalog.apply(&entry.message);
            }
        }
        Some(catalog)
    }

    fn new(prefix: PrefixEntry) -> Self {
        let skills = (prefix.skills.iter())
            .map(|stamp| (stamp.name.clone(), stamp.digest))
            .collect();
        Self {
            rows: prefix.tools.clone(),
            told: prefix.tools.clone(),
            skills,
            prefix,
        }
    }

    /// The system blocks of every request.
    pub(crate) fn system(&self) -> &[SystemBlock] {
        &self.prefix.system
    }

    /// Whether the model was told the tool `name` is offered.
    pub(crate) fn offers(&self, name: &str) -> bool {
        self.told.iter().any(|tool| tool.name == name)
    }

    /// Whether the model was told of the skill `name`.
    pub(crate) fn knows_skill(&self, name: &str) -> bool {
        self.skills.contains_key(name)
    }

    /// The tools of the next request, in order. `keep` narrows them for
    /// one turn (a skill's `allowed_tools`).
    pub(crate) fn tools(&self, keep: Option<&dyn Fn(&str) -> bool>) -> Vec<ToolDefinition> {
        let rows = self.rows.iter();
        match keep {
            None => rows.cloned().collect(),
            Some(keep) => rows.filter(|row| keep(&row.name)).cloned().collect(),
        }
    }

    /// Fold the catalog changes of a logged `message`.
    pub(crate) fn apply(&mut self, message: &Message) {
        for block in &message.content {
            if let ContentBlock::CatalogChange(change) = block {
                self.fold(change);
            }
        }
    }

    fn fold(&mut self, change: &CatalogChangeBlock) {
        self.told
            .retain(|tool| !change.removed_tools.contains(&tool.name));
        for definition in &change.tool_definitions {
            upsert(&mut self.told, definition, |_, _| true);
            let schema_changed =
                |row: &ToolDefinition, new: &ToolDefinition| row.input_schema != new.input_schema;
            upsert(&mut self.rows, definition, schema_changed);
        }
        for name in &change.removed_skills {
            self.skills.remove(name);
        }
        for stamp in &change.skills {
            self.skills.insert(stamp.name.clone(), stamp.digest);
        }
    }

    /// The notice of what `live` changed since the model was last told,
    /// or `None` when nothing did.
    pub(crate) fn change(&self, live: &Live) -> Option<CatalogChangeBlock> {
        let told: BTreeMap<&str, Digest> = (self.skills.iter())
            .map(|(name, digest)| (name.as_str(), *digest))
            .collect();
        notice::diff(&self.told, &live.tools, &told, &live.skills)
    }

    /// The consolidated notice of every change since the prefix, for a
    /// compaction that folds the notices away: what the model was told,
    /// against the prefix. Skill lines take their description from `live`.
    pub(crate) fn restatement(&self, live: &[LiveSkill]) -> Option<CatalogChangeBlock> {
        let base: BTreeMap<&str, Digest> = (self.prefix.skills.iter())
            .map(|stamp| (stamp.name.as_str(), stamp.digest))
            .collect();
        let told: Vec<LiveSkill> = (self.skills.iter())
            .map(|(name, digest)| {
                let line = live.iter().find(|skill| skill.stamp.name == *name);
                LiveSkill {
                    stamp: SkillStamp {
                        name: name.clone(),
                        digest: *digest,
                    },
                    description: line.map(|s| s.description.clone()).unwrap_or_default(),
                }
            })
            .collect();
        notice::diff(&self.prefix.tools, &self.told, &base, &told)
    }
}

/// Put `definition` in `rows`: replace the row of its name when `replace`
/// says so, else keep it; append it when no row has its name.
fn upsert(
    rows: &mut Vec<ToolDefinition>,
    definition: &ToolDefinition,
    replace: impl Fn(&ToolDefinition, &ToolDefinition) -> bool,
) {
    match rows.iter_mut().find(|row| row.name == definition.name) {
        Some(row) if replace(row, definition) => *row = definition.clone(),
        Some(_) => {}
        None => rows.push(definition.clone()),
    }
}

/// The prefix a new session takes: the agent's system blocks and what it
/// offers a session, as they are now.
pub(crate) fn prefix(agent: &AgentInner) -> PrefixEntry {
    let live = live(agent, None, &Loads::default());
    PrefixEntry {
        system: agent.system(live.deferred_skills.as_deref()),
        tools: live.tools,
        skills: live.skills.into_iter().map(|skill| skill.stamp).collect(),
    }
}

/// What `agent` offers now to a session whose model `known` says what it
/// was told (`None` for a new session), with `loads` a batch's searches
/// just found. Without tool search: all it has.
pub(crate) fn live(agent: &AgentInner, known: Option<&Catalog>, loads: &Loads) -> Live {
    let snapshot = agent.snapshot();
    let mut live = Live {
        tools: snapshot.definitions(),
        skills: agent.listed_skills(),
        deferred_skills: None,
    };
    if let Some(search) = &agent.tool_search {
        let told = |name: &str| known.is_some_and(|c| c.offers(name)) || loads.tools.contains(name);
        let told_skill =
            |name: &str| known.is_some_and(|c| c.knows_skill(name)) || loads.skills.contains(name);
        tool_search::offer(search, &snapshot, &mut live, &told, &told_skill);
    }
    live
}

/// A version of what `agent` offers: it moves whenever a registration or
/// a reload may have changed the live catalog.
pub(crate) fn version(agent: &AgentInner) -> (u64, u64, u64) {
    let skills = (agent.skills.as_ref()).map_or(0, |versions| versions.revision());
    let subagents = (agent.subagents.as_ref()).map_or(0, |versions| versions.revision());
    (agent.tools.generation(), skills, subagents)
}
