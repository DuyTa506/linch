//! What a run's subagent child runs spent, for its `Result`.
//!
//! The run's own `total_usage` counts its own calls only. Each child run
//! reports its own total in its own `Result`; the parent finds its
//! children as the budget does: from the `Subagent` and `SubagentContinue`
//! calls in its log (each child run is `<run id>:<tool_use_id>`), then
//! reads each one's last event, so a child that ended while its parent was
//! down, or that the parent re-read instead of driving, counts the same.

use std::collections::BTreeMap;

use super::{CONTINUE_TOOL, SUBAGENT_TOOL};
use crate::agent::AgentInner;
use crate::events::kinds::{ResultEvent, SubagentUsage};
use crate::events::{Envelope, Event};
use crate::ids::RunId;
use crate::json::JsonValue;
use crate::store::StoreError;

/// The child-spawning calls of one run, in log order.
#[derive(Debug, Default, Clone)]
pub(crate) struct Spawned {
    /// `(tool_use_id, subagent_type)` of each call, once per id.
    calls: Vec<(String, Option<String>)>,
}

impl Spawned {
    /// Whether no call was seen.
    pub(crate) fn is_empty(&self) -> bool {
        self.calls.is_empty()
    }

    /// The calls in `events`.
    pub(crate) fn of(events: &[Envelope]) -> Self {
        let mut spawned = Self::default();
        events.iter().for_each(|e| spawned.see(&e.event));
        spawned
    }

    /// Note `event` if it starts a child-spawning call.
    pub(crate) fn see(&mut self, event: &Event) {
        let Event::ToolCallStart(start) = event else {
            return;
        };
        let spawns = [SUBAGENT_TOOL, CONTINUE_TOOL].contains(&start.tool_name.as_str());
        if !spawns || self.calls.iter().any(|(id, _)| *id == start.tool_use_id) {
            return;
        }
        let kind = (start.tool_name == SUBAGENT_TOOL)
            .then(|| start.input.get("subagent_type").and_then(JsonValue::as_str))
            .flatten()
            .map(str::trim)
            .filter(|kind| !kind.is_empty() && !["general-purpose", "_default"].contains(kind))
            .map(str::to_owned);
        self.calls.push((start.tool_use_id.clone(), kind));
    }

    /// What each child run that ended spent, by child run id. A child that
    /// has not ended, or does not exist (its call never got to start it),
    /// is left out.
    pub(crate) async fn usage(
        &self,
        agent: &AgentInner,
        run_id: &RunId,
    ) -> Result<BTreeMap<String, SubagentUsage>, StoreError> {
        let mut usage = BTreeMap::new();
        for (tool_use_id, subagent_type) in &self.calls {
            let child = RunId::new(format!("{run_id}:{tool_use_id}"));
            let record = match agent.runs.load(&agent.namespace, &child).await {
                Ok(record) if record.status.is_terminal() => record,
                Ok(_) | Err(StoreError::NotFound { .. }) => continue,
                Err(error) => return Err(error),
            };
            // The `Result` is the last event of a terminal run.
            let after = record.last_seq.saturating_sub(1);
            let events = agent.runs.events(&agent.namespace, &child, after).await?;
            let result = events.iter().rev().find_map(|e| match &e.event {
                Event::Result(result) => Some(result),
                _ => None,
            });
            if let Some(result) = result {
                usage.insert(child.to_string(), of(result, subagent_type.clone()));
            }
        }
        Ok(usage)
    }
}

fn of(result: &ResultEvent, subagent_type: Option<String>) -> SubagentUsage {
    SubagentUsage {
        subagent_type,
        total_usage: result.total_usage,
        usage_complete: result.usage_complete,
        usage_by_model: result.usage_by_model.clone(),
        total_cost_usd: result.total_cost_usd,
    }
}
