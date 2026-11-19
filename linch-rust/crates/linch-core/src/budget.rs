//! Run budgets: a token and USD spending cap shared by a run and its
//! subagent tree (Python's `linch.budget.RunBudget`).
//!
//! An [`AgentSpec`](crate::AgentSpec) with a [`RunBudget`] gives each root
//! run one meter. Every receipt the run settles is charged to it: the
//! four usage buckets as tokens, the priced cost as USD (an unpriced call
//! charges none, so only the token cap binds it). The subagent child runs
//! it starts charge the same meter, so one budget caps the tree.
//!
//! Before each provider call (and each compaction summary call) the run
//! checks the meter: spent at or over a cap stops it with a `budget`
//! `exceeded` event, a `BudgetExceededError` and a failed result. A capped
//! budget also refuses a call once any call of the tree had unknown usage
//! (a delivery cut off by a crash, a failed call that reported none), as
//! Python does: the cap could no longer be kept. The first charge that
//! reaches `warn_ratio` of a cap adds a `budget` `warning`, once per tree.
//!
//! A subagent definition may carry a budget of its own
//! ([`SubagentDefinition::budget`](crate::SubagentDefinition)): each child
//! run of it charges a meter *within* the tree's, so its spend counts for
//! both. The tree is checked first (an exhausted tree refuses the child
//! with the run's own `BudgetExceededError`); then the child's cap, which
//! ends that child with a `BudgetExceededError` whose message says
//! `subagent budget`, and not the parent. Its warning fires once per child
//! run, beside the tree's once per tree.
//!
//! The meter is not a durable record of its own. A root run that resumes
//! sums the receipts in its log and in the logs of its child runs (each
//! `<run id>:<tool_use_id>` of a `Subagent` or `SubagentContinue` call), so
//! the spend never starts over and no write is added.

use std::sync::{Arc, Mutex, MutexGuard, PoisonError};

use serde::{Deserialize, Serialize};
use serde_json::json;

use crate::events::kinds::{BudgetEvent, BudgetKind, ErrorEvent};
use crate::events::model_call::{CallDisposition, ModelCallReceiptEvent};
use crate::events::{Envelope, Event};
use crate::json::JsonObject;
use crate::usage::Usage;

fn default_warn_ratio() -> f64 {
    0.9
}

/// A spending cap for a run and its subagent tree. Without a cap it only
/// counts.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RunBudget {
    /// Most tokens the tree may spend, all four usage buckets summed.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub max_tokens: Option<u64>,
    /// Most USD the tree may spend, by the agent's pricing. The agent does
    /// not build when its model or one of its fallback models has no price,
    /// and a run ends with a `BudgetAccountingError` on a call served by a
    /// model that has none.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub max_cost_usd: Option<f64>,
    /// Fraction of either cap at which the one warning fires (default 0.9).
    #[serde(default = "default_warn_ratio")]
    pub warn_ratio: f64,
}

impl Default for RunBudget {
    fn default() -> Self {
        Self {
            max_tokens: None,
            max_cost_usd: None,
            warn_ratio: default_warn_ratio(),
        }
    }
}

impl RunBudget {
    /// Why this budget cannot run, if it cannot: a cost cap or warning
    /// ratio that is not a finite number, which no event could carry.
    pub(crate) fn problem(&self) -> Option<&'static str> {
        let finite = self.warn_ratio.is_finite() && self.max_cost_usd.is_none_or(f64::is_finite);
        (!finite).then_some("budget.max_cost_usd and budget.warn_ratio must be finite numbers")
    }

    fn capped(&self) -> bool {
        self.max_tokens.is_some() || self.max_cost_usd.is_some()
    }
}

/// What a run tree spent.
#[derive(Debug, Clone, Copy, Default, PartialEq)]
pub(crate) struct Spent {
    pub(crate) tokens: u64,
    pub(crate) usd: f64,
    /// The warning fired.
    pub(crate) warned: bool,
    /// A call's usage is unknown.
    pub(crate) unknown: bool,
    /// A call has usage and no cost: a cost cap cannot count it.
    pub(crate) unpriced: bool,
}

impl Spent {
    /// What the receipts in `events` spent, and whether a warning is among
    /// them. Only the run's own events count: a child's, wrapped in the
    /// log, are read from the child's own log.
    pub(crate) fn of(events: &[Envelope]) -> Self {
        let mut spent = Self::default();
        for envelope in events {
            match &envelope.event {
                Event::ModelCallReceipt(receipt) => spent.charge(receipt),
                Event::Budget(budget) if budget.kind == BudgetKind::Warning => spent.warned = true,
                _ => {}
            }
        }
        spent
    }

    fn charge(&mut self, receipt: &ModelCallReceiptEvent) {
        match receipt.usage {
            Some(usage) => {
                self.tokens = self.tokens.saturating_add(tokens(&usage));
                match receipt.cost_usd {
                    Some(cost) => self.usd += cost,
                    None => self.unpriced = true,
                }
            }
            None => self.unknown |= receipt.disposition != CallDisposition::NotDispatched,
        }
    }

    /// `self` and `other` together.
    pub(crate) fn and(self, other: Self) -> Self {
        Self {
            tokens: self.tokens.saturating_add(other.tokens),
            usd: self.usd + other.usd,
            warned: self.warned || other.warned,
            unknown: self.unknown || other.unknown,
            unpriced: self.unpriced || other.unpriced,
        }
    }
}

/// Every bucket: Python's `Usage.add` total.
fn tokens(usage: &Usage) -> u64 {
    usage
        .total_input_tokens()
        .saturating_add(usage.output_tokens)
}

/// Why the next call may not start.
pub(crate) enum Refusal {
    /// A cap is reached; the event reports the spend.
    Exceeded(BudgetEvent),
    /// The cap of a subagent definition is reached by one child run.
    SubagentExceeded(BudgetEvent),
    /// A capped budget cannot account for a call of the tree.
    Unknown,
    /// A cost cap cannot count a call the pricing has no rate for.
    Unpriced,
}

impl Refusal {
    /// The `error` payload of the terminal, as Python words it.
    pub(crate) fn error(&self) -> ErrorEvent {
        let error = match self {
            Self::Exceeded(event) => json!({
                "name": "BudgetExceededError",
                "message": format!(
                    "run budget exhausted ({} tokens, ${:.4} spent)",
                    event.spent_tokens, event.spent_usd
                ),
                "retryable": false,
            }),
            Self::SubagentExceeded(event) => json!({
                "name": "BudgetExceededError",
                "message": format!(
                    "subagent budget exhausted ({} tokens, ${:.4} spent)",
                    event.spent_tokens, event.spent_usd
                ),
                "retryable": false,
            }),
            Self::Unknown => json!({
                "name": "BudgetAccountingError",
                "message": "budget cannot admit a call after unknown use",
                "retryable": false,
            }),
            Self::Unpriced => json!({
                "name": "BudgetAccountingError",
                "message": "max_cost_usd cannot count a call the pricing has no price for; \
                            set a price for the model that served it or cap max_tokens",
                "retryable": false,
            }),
        };
        let error: JsonObject = error.as_object().cloned().unwrap_or_default();
        ErrorEvent { error }
    }
}

/// One run tree's budget and spend, shared by its runs in this process.
#[derive(Debug, Clone)]
pub(crate) struct Meter(Arc<Inner>);

#[derive(Debug)]
struct Inner {
    limits: RunBudget,
    spent: Mutex<Spent>,
    /// The meter this one is within: what it charges, it charges too.
    within: Option<Meter>,
    /// Whether this is a child run's own cap (a subagent definition's).
    subagent: bool,
}

impl Meter {
    /// A meter for `limits` that has spent `spent` already.
    pub(crate) fn new(limits: RunBudget, spent: Spent) -> Self {
        Self(Arc::new(Inner {
            limits,
            spent: Mutex::new(spent),
            within: None,
            subagent: false,
        }))
    }

    /// A meter for a child run's own `limits` that has spent `spent`
    /// already, within `tree` when the run has one: it charges both, and
    /// the tree refuses first.
    pub(crate) fn within(tree: Option<Meter>, limits: RunBudget, spent: Spent) -> Self {
        Self(Arc::new(Inner {
            limits,
            spent: Mutex::new(spent),
            within: tree,
            subagent: true,
        }))
    }

    fn spent(&self) -> MutexGuard<'_, Spent> {
        // Nothing panics while holding the lock.
        (self.0.spent.lock()).unwrap_or_else(PoisonError::into_inner)
    }

    /// Charge a settled receipt.
    pub(crate) fn charge(&self, receipt: &ModelCallReceiptEvent) {
        self.spent().charge(receipt);
        if let Some(tree) = &self.0.within {
            tree.charge(receipt);
        }
    }

    fn event(&self, kind: BudgetKind, spent: &Spent) -> BudgetEvent {
        BudgetEvent {
            kind,
            spent_tokens: spent.tokens,
            spent_usd: spent.usd,
            max_tokens: self.0.limits.max_tokens,
            max_cost_usd: self.0.limits.max_cost_usd,
        }
    }

    /// The warning, the first time the spend reaches `warn_ratio` of a cap.
    pub(crate) fn take_warning(&self) -> Option<BudgetEvent> {
        self.take_own_warning()
            .or_else(|| self.0.within.as_ref()?.take_warning())
    }

    fn take_own_warning(&self) -> Option<BudgetEvent> {
        let limits = &self.0.limits;
        let mut spent = self.spent();
        let ratio = limits.warn_ratio;
        let over = (limits.max_tokens).is_some_and(|max| spent.tokens as f64 >= ratio * max as f64)
            || (limits.max_cost_usd).is_some_and(|max| spent.usd >= ratio * max);
        if spent.warned || !over {
            return None;
        }
        spent.warned = true;
        Some(self.event(BudgetKind::Warning, &spent))
    }

    /// Why the next call may not start; `None` when it may.
    pub(crate) fn refusal(&self) -> Option<Refusal> {
        if let Some(refusal) = self.0.within.as_ref().and_then(Meter::refusal) {
            return Some(refusal);
        }
        let limits = &self.0.limits;
        let spent = *self.spent();
        let exceeded = (limits.max_tokens).is_some_and(|max| spent.tokens >= max)
            || (limits.max_cost_usd).is_some_and(|max| spent.usd >= max);
        if exceeded {
            let event = self.event(BudgetKind::Exceeded, &spent);
            return Some(match self.0.subagent {
                true => Refusal::SubagentExceeded(event),
                false => Refusal::Exceeded(event),
            });
        }
        if limits.max_cost_usd.is_some() && spent.unpriced {
            return Some(Refusal::Unpriced);
        }
        (limits.capped() && spent.unknown).then_some(Refusal::Unknown)
    }
}

#[cfg(test)]
mod tests;
