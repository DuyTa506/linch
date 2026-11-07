//! Model-call accounting records: journal-only [`super::Event`] kinds.
//!
//! These describe the wire shape of one delivery attempt's lifecycle only —
//! identity, outcome, and budget attribution. Ledger invariants ("each
//! delivery has an id", "a receipt settles exactly once", "budget is shared
//! across the whole agent tree", from `INVENTORY.md` row 6) are enforced by
//! the ledger itself, which is out of scope for P2 (providers + ledger +
//! budget land later). `usage: Option<Usage>` follows the same rule as the
//! rest of this crate: unknown usage is `None`, never a zero [`Usage`].

use super::wire::serialize_finite_f64_option;
use crate::ids::RunId;
use crate::usage::Usage;
use serde::{Deserialize, Serialize};

/// Which logical operation a model call belongs to.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ModelCallKind {
    /// A primary turn call.
    Primary,
    /// A compaction call.
    Compaction,
}

/// A model-call delivery attempt was dispatched.
///
/// Covers both of Python's `intent` and `dispatch_started` ledger records:
/// P2 only needs "a delivery with this id was announced", not the
/// queued-vs-dispatched distinction.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ModelCallDeliveryEvent {
    /// Run making the call.
    pub run_id: RunId,
    /// Identifier for the logical operation this delivery attempts.
    pub logical_call_id: String,
    /// Identifier for this specific delivery attempt; the unit of
    /// deduplication.
    pub delivery_attempt_id: String,
    /// Logical operation kind.
    pub kind: ModelCallKind,
    /// Turn this call belongs to, if applicable.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub turn_index: Option<u64>,
    /// Model requested for this delivery.
    pub requested_model: String,
}

/// How a model-call delivery was disposed of.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CallDisposition {
    /// The response was accepted and used.
    Accepted,
    /// The response was rejected (e.g. failed validation).
    Rejected,
    /// The delivery was cancelled before completion.
    Cancelled,
    /// The delivery failed (transport or provider error).
    Failed,
    /// The delivery was never dispatched.
    NotDispatched,
}

/// The outcome of one model-call delivery attempt.
///
/// Also covers Python's `abandoned` ledger record, represented here as a
/// receipt with `disposition: Cancelled` or `Failed` and `usage: None`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ModelCallReceiptEvent {
    /// Run that made the call.
    pub run_id: RunId,
    /// Logical operation this receipt settles.
    pub logical_call_id: String,
    /// Delivery attempt this receipt reports on.
    pub delivery_attempt_id: String,
    /// Logical operation kind.
    pub kind: ModelCallKind,
    /// Model that actually served the call, when dispatched.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub actual_model: Option<String>,
    /// How the delivery was disposed of.
    pub disposition: CallDisposition,
    /// Usage reported for the call, or `None` if unknown or never
    /// dispatched.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub usage: Option<Usage>,
    /// Cost of the call, when known. Encoding a NaN or infinite value is an
    /// error (see `serialize_finite_f64_option`), never a silent `null`
    /// indistinguishable from "unknown".
    #[serde(
        default,
        skip_serializing_if = "Option::is_none",
        serialize_with = "serialize_finite_f64_option"
    )]
    pub cost_usd: Option<f64>,
    /// Elapsed delivery time in milliseconds.
    pub duration_ms: u64,
}

/// A settled receipt's usage was attributed to a budget owner.
///
/// Covers Python's `projection` ledger record: the budget-ledger side
/// effect of a receipt, distinct from the receipt itself so one receipt can
/// be projected onto a budget shared across an agent tree.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ModelCallUsageSettlementEvent {
    /// Run whose call is being settled.
    pub run_id: RunId,
    /// Budget owner credited with this usage, when budgets are shared
    /// across an agent tree.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub budget_owner_id: Option<String>,
    /// Logical operation this settlement accounts for.
    pub logical_call_id: String,
    /// Usage attributed to the budget owner.
    pub usage: Usage,
    /// Cost attributed to the budget owner, when known. Encoding a NaN or
    /// infinite value is an error (see `serialize_finite_f64_option`),
    /// never a silent `null` indistinguishable from "unknown".
    #[serde(
        default,
        skip_serializing_if = "Option::is_none",
        serialize_with = "serialize_finite_f64_option"
    )]
    pub cost_usd: Option<f64>,
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn delivery_round_trips() {
        let delivery = ModelCallDeliveryEvent {
            run_id: RunId::new("r1"),
            logical_call_id: "lc1".into(),
            delivery_attempt_id: "da1".into(),
            kind: ModelCallKind::Primary,
            turn_index: Some(2),
            requested_model: "claude".into(),
        };
        let value = serde_json::to_value(&delivery).unwrap();
        assert_eq!(
            value,
            json!({
                "run_id": "r1",
                "logical_call_id": "lc1",
                "delivery_attempt_id": "da1",
                "kind": "primary",
                "turn_index": 2,
                "requested_model": "claude",
            })
        );
        assert_eq!(
            serde_json::from_value::<ModelCallDeliveryEvent>(value).unwrap(),
            delivery
        );
    }

    #[test]
    fn abandoned_delivery_is_a_receipt_with_no_usage() {
        let receipt = ModelCallReceiptEvent {
            run_id: RunId::new("r1"),
            logical_call_id: "lc1".into(),
            delivery_attempt_id: "da1".into(),
            kind: ModelCallKind::Primary,
            actual_model: None,
            disposition: CallDisposition::Cancelled,
            usage: None,
            cost_usd: None,
            duration_ms: 0,
        };
        let value = serde_json::to_value(&receipt).unwrap();
        assert!(value.get("usage").is_none());
        assert!(value.get("actual_model").is_none());
        assert_eq!(
            serde_json::from_value::<ModelCallReceiptEvent>(value).unwrap(),
            receipt
        );
    }

    #[test]
    fn non_finite_receipt_cost_fails_to_encode() {
        let receipt = ModelCallReceiptEvent {
            run_id: RunId::new("r1"),
            logical_call_id: "lc1".into(),
            delivery_attempt_id: "da1".into(),
            kind: ModelCallKind::Primary,
            actual_model: None,
            disposition: CallDisposition::Accepted,
            usage: None,
            cost_usd: Some(f64::NAN),
            duration_ms: 0,
        };
        assert!(serde_json::to_value(receipt).is_err());
    }

    #[test]
    fn non_finite_settlement_cost_fails_to_encode() {
        let settlement = ModelCallUsageSettlementEvent {
            run_id: RunId::new("r1"),
            budget_owner_id: None,
            logical_call_id: "lc1".into(),
            usage: Usage::default(),
            cost_usd: Some(f64::INFINITY),
        };
        assert!(serde_json::to_value(settlement).is_err());
    }
}
