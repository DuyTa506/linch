//! The context budget (Python's `ContextBudget`): a cap on the request-only
//! context the `context_build` hooks add (retrieved memory, file memory,
//! whatever a hook adds), on [`AgentSpec::context_budget`](crate::AgentSpec).
//!
//! What it counts: only the blocks the hooks add to a request, at four
//! characters per token (the compaction estimate, rounded up per block).
//! The system prompt, the tools and the logged history are not counted:
//! compaction owns the history, and the spec owns the rest.
//!
//! What it does when they do not fit: drops whole blocks until they do,
//! never fails the run. Blocks go in this order: those of hooks the budget
//! does not name first, the later hook's first; then those of the hooks in
//! [`ContextBudget::drop_order`], the first listed first. Inside one hook the
//! last block goes first, so a retrieval hook that lists its best chunk
//! first keeps it longest. A hook in [`ContextBudget::pinned`] is never
//! dropped; if the pinned blocks alone are over the cap, the request goes
//! over and the event says by how much.
//!
//! The budget is part of the run contract. Its decisions are recomputed for
//! every request from the hooks' answers and are not logged for recovery:
//! the `context_build` event reports them for observers, and a resumed run
//! decides again.

use std::collections::HashSet;
use std::sync::Arc;

use serde::{Deserialize, Serialize};
use serde_json::json;

use crate::json::JsonObject;
use crate::message::TextBlock;

/// The cap on the context hooks add to one request, and what is dropped
/// first when it does not fit.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ContextBudget {
    /// Most estimated tokens the hooks' blocks may add to one request; at
    /// least 1.
    pub max_tokens: u64,
    /// Policy ids of `context_build` hooks, the one whose blocks are
    /// dropped first at the front. A hook not listed here or in `pinned` is
    /// dropped before every listed one.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub drop_order: Vec<String>,
    /// Policy ids of `context_build` hooks whose blocks are never dropped.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub pinned: Vec<String>,
}

/// One block a hook added, with who added it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct Sourced {
    /// The policy id of the hook.
    pub source: Arc<str>,
    /// Position among that hook's blocks, from 0.
    pub index: usize,
    pub block: TextBlock,
}

/// The blocks a request keeps, and what the budget reports of them.
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct Trimmed {
    pub blocks: Vec<TextBlock>,
    /// The `budget` of the `context_build` event.
    pub report: JsonObject,
}

/// Estimated tokens of `text`: four characters per token, rounded up.
fn tokens(text: &str) -> u64 {
    text.len().div_ceil(4) as u64
}

impl ContextBudget {
    /// A budget of `max_tokens`, dropping unlisted hooks only.
    pub fn new(max_tokens: u64) -> Self {
        Self {
            max_tokens,
            drop_order: Vec::new(),
            pinned: Vec::new(),
        }
    }

    /// Why this budget cannot be applied, if it cannot.
    pub fn problem(&self) -> Option<String> {
        if self.max_tokens == 0 {
            return Some("context_budget.max_tokens must be at least 1".into());
        }
        let mut seen = HashSet::new();
        for id in self.drop_order.iter().chain(&self.pinned) {
            if id.trim().is_empty() {
                return Some("context_budget names a blank hook policy id".into());
            }
            if !seen.insert(id.as_str()) {
                return Some(format!(
                    "context_budget names hook `{id}` twice (drop_order and pinned together)"
                ));
            }
        }
        None
    }

    /// Keep what fits of `blocks`, dropping by the declared order.
    pub(crate) fn apply(&self, blocks: Vec<Sourced>) -> Trimmed {
        let cost: Vec<u64> = blocks.iter().map(|b| tokens(&b.block.text)).collect();
        let mut used: u64 = cost.iter().sum();
        let mut dropped = vec![false; blocks.len()];
        for at in self.candidates(&blocks) {
            if used <= self.max_tokens {
                break;
            }
            dropped[at] = true;
            used -= cost[at];
        }
        let mut gone = Vec::new();
        let mut kept = Vec::new();
        for (at, sourced) in blocks.into_iter().enumerate() {
            if dropped[at] {
                gone.push(json!({
                    "source": &*sourced.source,
                    "index": sourced.index,
                    "tokens": cost[at],
                }));
            } else {
                kept.push(sourced.block);
            }
        }
        let mut report = JsonObject::new();
        report.insert("max_tokens".into(), self.max_tokens.into());
        report.insert("used_tokens".into(), used.into());
        report.insert(
            "remaining_tokens".into(),
            self.max_tokens.saturating_sub(used).into(),
        );
        report.insert("trimmed".into(), (!gone.is_empty()).into());
        if used > self.max_tokens {
            report.insert("over_by".into(), (used - self.max_tokens).into());
        }
        report.insert("dropped".into(), gone.into());
        Trimmed {
            blocks: kept,
            report,
        }
    }

    /// Indexes into `blocks` in the order they may be dropped: never a
    /// pinned hook's.
    fn candidates(&self, blocks: &[Sourced]) -> Vec<usize> {
        let rank = |source: &str| -> Option<usize> {
            if self.pinned.iter().any(|id| id == source) {
                return None;
            }
            // Unlisted hooks come before every listed one.
            Some(match self.drop_order.iter().position(|id| id == source) {
                Some(position) => position + 1,
                None => 0,
            })
        };
        let mut order: Vec<(usize, usize)> = (blocks.iter().enumerate())
            .filter_map(|(at, sourced)| Some((rank(&sourced.source)?, at)))
            .collect();
        // Within a rank the later block first. Blocks of unlisted hooks
        // share a rank, so the later hook's go before the earlier hook's.
        order.sort_by(|(a_rank, a_at), (b_rank, b_at)| a_rank.cmp(b_rank).then(b_at.cmp(a_at)));
        order.into_iter().map(|(_, at)| at).collect()
    }
}

#[cfg(test)]
mod tests;
