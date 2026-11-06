//! Context and control-flow kinds: usage, context assembly, compaction,
//! loop guard, budget, prompt-cache advisory, and verification.

use super::is_zero;
use crate::events::wire::{serialize_finite_f64, serialize_finite_f64_option};
use crate::json::JsonObject;
use crate::usage::Usage;
use serde::{Deserialize, Serialize};

/// Per-call and cumulative token usage.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct UsageEvent {
    /// Usage reported for the current provider call.
    pub usage: Usage,
    /// Usage accumulated across the run.
    pub cumulative: Usage,
}

/// The context assembled for one provider request.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ContextBuildEvent {
    /// Number of system blocks selected.
    pub system_blocks: u64,
    /// Number of conversation messages selected.
    pub messages: u64,
    /// Tool names offered in the request, when recorded.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub selected_tools: Option<Vec<String>>,
    /// Context-budget accounting details used for the request.
    pub budget: JsonObject,
    /// Additional context-builder metadata for observers.
    pub metadata: JsonObject,
}

/// One context-compaction operation.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CompactionEvent {
    /// Message count before compaction.
    pub messages_before: u64,
    /// Message count after compaction.
    pub messages_after: u64,
    /// Token estimate before compaction.
    pub tokens_before: u64,
    /// Token estimate after compaction.
    pub tokens_after: u64,
    /// Compaction strategy used.
    pub strategy: String,
}

/// Why a [`LoopGuardEvent`] tripped.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum LoopGuardReason {
    /// The same tool call repeated beyond the configured limit.
    RepeatedToolCall,
    /// Tool calls kept failing beyond the configured limit.
    RepeatedFailures,
    /// The run reached its configured turn limit.
    MaxTurns,
}

/// What the loop did after a [`LoopGuardEvent`] tripped.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum LoopGuardAction {
    /// The loop stopped with a hard error.
    Stop,
    /// One tools-disabled turn was injected before stopping.
    ForceFinal,
}

/// Emitted when the loop guard trips or `max_turns` is reached.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LoopGuardEvent {
    /// Machine-readable trip condition.
    pub reason: LoopGuardReason,
    /// Human-readable description of why the guard tripped.
    pub detail: String,
    /// Loop action taken after the guard tripped.
    pub action: LoopGuardAction,
}

/// Which [`Usage`]/cost threshold a [`BudgetEvent`] reports.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum BudgetKind {
    /// The budget crossed its warning ratio (emitted once per budget).
    Warning,
    /// The budget was exhausted; the run stops with an error result.
    Exceeded,
}

/// Emitted when a run budget crosses its warning ratio or is exhausted.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BudgetEvent {
    /// Budget transition represented by the event.
    pub kind: BudgetKind,
    /// Tokens spent when the event was emitted.
    pub spent_tokens: u64,
    /// Cost spent when the event was emitted. Encoding a NaN or infinite
    /// value is an error (see `serialize_finite_f64`), never a silent
    /// `null`.
    #[serde(serialize_with = "serialize_finite_f64")]
    pub spent_usd: f64,
    /// Configured token limit, if any.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub max_tokens: Option<u64>,
    /// Configured cost limit, if any. Encoding a NaN or infinite value is
    /// an error (see `serialize_finite_f64_option`), never a silent
    /// `null` indistinguishable from "no limit".
    #[serde(
        default,
        skip_serializing_if = "Option::is_none",
        serialize_with = "serialize_finite_f64_option"
    )]
    pub max_cost_usd: Option<f64>,
}

/// Why a [`PromptCacheAdvisoryEvent`] fired.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PromptCacheAdvisoryReason {
    /// The tools leading the cached prefix changed.
    ToolSetChanged,
    /// The model changed; the provider's cache is keyed per model.
    ModelChanged,
}

/// Advisory that a prefix-breaking change invalidated the provider's prompt
/// cache between provider calls on the same session.
///
/// Observational only: Linch reports the risk but never rewrites the
/// request.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PromptCacheAdvisoryEvent {
    /// Prefix-breaking change that invalidated the cache.
    pub reason: PromptCacheAdvisoryReason,
    /// Human-readable description of what changed and why it matters.
    pub detail: String,
}

/// Why a [`VerificationEvent`] fired.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum VerificationAction {
    /// Feedback was injected and the loop continues.
    Retry,
    /// The run fails.
    Stop,
    /// A retry verdict was returned but no retries remain; the answer is
    /// accepted as-is.
    Exhausted,
}

/// Emitted when a closed-loop gate acts on a would-be-final answer.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct VerificationEvent {
    /// Name of the gate that produced the action.
    pub verifier: String,
    /// Verification action taken.
    pub action: VerificationAction,
    /// Feedback or error message attached to the verdict.
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub feedback: String,
    /// Retry attempts used so far in this run.
    #[serde(default, skip_serializing_if = "is_zero")]
    pub attempt: u64,
}

#[cfg(test)]
mod tests {
    use super::*;

    fn budget(spent_usd: f64, max_cost_usd: Option<f64>) -> BudgetEvent {
        BudgetEvent {
            kind: BudgetKind::Warning,
            spent_tokens: 0,
            spent_usd,
            max_tokens: None,
            max_cost_usd,
        }
    }

    #[test]
    fn non_finite_spent_usd_fails_to_encode() {
        assert!(serde_json::to_value(budget(f64::NAN, None)).is_err());
        assert!(serde_json::to_value(budget(f64::INFINITY, None)).is_err());
        assert!(serde_json::to_value(budget(f64::NEG_INFINITY, None)).is_err());
    }

    #[test]
    fn non_finite_max_cost_usd_fails_to_encode() {
        assert!(serde_json::to_value(budget(0.0, Some(f64::NAN))).is_err());
        assert!(serde_json::to_value(budget(0.0, Some(f64::INFINITY))).is_err());
    }

    #[test]
    fn finite_values_round_trip() {
        let event = budget(1.5, Some(10.0));
        let value = serde_json::to_value(&event).unwrap();
        assert_eq!(serde_json::from_value::<BudgetEvent>(value).unwrap(), event);
    }
}
