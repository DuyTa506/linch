//! Strategies for the accounting kinds: usage, tool ends, the result and
//! the model-call records. Split from `builders` to keep both files near
//! this crate's per-file line target.

use super::*;
use crate::events::kinds::*;
use crate::events::model_call::*;
use crate::message::ToolResultBlock;
use proptest::option;
use proptest::prelude::*;

pub(super) fn usage_event() -> impl Strategy<Value = UsageEvent> {
    let costs = (option::of(finite_f64()), option::of(finite_f64()));
    (usage(), usage(), costs).prop_map(|(usage, cumulative, (cost_usd, cumulative_cost_usd))| {
        UsageEvent {
            usage,
            cumulative,
            cost_usd,
            cumulative_cost_usd,
        }
    })
}

pub(super) fn tool_result_block() -> impl Strategy<Value = ToolResultBlock> {
    (short_string(), short_string(), any::<bool>()).prop_map(|(tool_use_id, text, is_error)| {
        ToolResultBlock::text(tool_use_id, text, is_error)
    })
}

pub(super) fn tool_call_end_event() -> impl Strategy<Value = ToolCallEndEvent> {
    (
        short_string(),
        tool_result_block(),
        any::<u64>(),
        option::of(json_value(1)),
    )
        .prop_map(
            |(tool_name, result, duration_ms, canonical_output)| ToolCallEndEvent {
                tool_name,
                result,
                duration_ms,
                canonical_output,
            },
        )
}

pub(super) fn result_event() -> impl Strategy<Value = ResultEvent> {
    (
        prop_oneof![
            Just(ResultSubtype::Success),
            Just(ResultSubtype::Error),
            Just(ResultSubtype::Aborted),
            Just(ResultSubtype::Interrupted),
        ],
        stop_reason(),
        usage(),
        any::<bool>(),
        any::<u64>(),
        option::of(finite_f64()),
        option::of(short_string()),
        option::of(json_object(1)),
        option::of(short_string()),
        option::of(short_string()),
    )
        .prop_map(
            |(
                subtype,
                stop_reason,
                total_usage,
                usage_complete,
                duration_ms,
                total_cost_usd,
                final_text,
                structured_output,
                structured_error,
                trace_id,
            )| {
                ResultEvent {
                    subtype,
                    stop_reason,
                    total_usage,
                    usage_complete,
                    duration_ms,
                    total_cost_usd,
                    final_text,
                    structured_output,
                    structured_error,
                    trace_id,
                }
            },
        )
}

pub(super) fn model_call_kind() -> impl Strategy<Value = ModelCallKind> {
    prop_oneof![
        Just(ModelCallKind::Primary),
        Just(ModelCallKind::Compaction)
    ]
}

pub(super) fn model_call_delivery_event() -> impl Strategy<Value = ModelCallDeliveryEvent> {
    (
        run_id(),
        short_string(),
        short_string(),
        model_call_kind(),
        option::of(any::<u64>()),
        short_string(),
    )
        .prop_map(
            |(run_id, logical_call_id, delivery_attempt_id, kind, turn_index, requested_model)| {
                ModelCallDeliveryEvent {
                    run_id,
                    logical_call_id,
                    delivery_attempt_id,
                    kind,
                    turn_index,
                    requested_model,
                }
            },
        )
}

pub(super) fn call_disposition() -> impl Strategy<Value = CallDisposition> {
    prop_oneof![
        Just(CallDisposition::Accepted),
        Just(CallDisposition::Rejected),
        Just(CallDisposition::Cancelled),
        Just(CallDisposition::Failed),
        Just(CallDisposition::NotDispatched),
    ]
}

pub(super) fn model_call_receipt_event() -> impl Strategy<Value = ModelCallReceiptEvent> {
    (
        run_id(),
        short_string(),
        short_string(),
        model_call_kind(),
        option::of(short_string()),
        call_disposition(),
        option::of(usage()),
        option::of(finite_f64()),
        any::<u64>(),
    )
        .prop_map(
            |(
                run_id,
                logical_call_id,
                delivery_attempt_id,
                kind,
                actual_model,
                disposition,
                usage,
                cost_usd,
                duration_ms,
            )| {
                ModelCallReceiptEvent {
                    run_id,
                    logical_call_id,
                    delivery_attempt_id,
                    kind,
                    actual_model,
                    disposition,
                    usage,
                    cost_usd,
                    duration_ms,
                }
            },
        )
}

pub(super) fn model_call_usage_settlement_event()
-> impl Strategy<Value = ModelCallUsageSettlementEvent> {
    (
        run_id(),
        option::of(short_string()),
        short_string(),
        usage(),
        option::of(finite_f64()),
    )
        .prop_map(
            |(run_id, budget_owner_id, logical_call_id, usage, cost_usd)| {
                ModelCallUsageSettlementEvent {
                    run_id,
                    budget_owner_id,
                    logical_call_id,
                    usage,
                    cost_usd,
                }
            },
        )
}
