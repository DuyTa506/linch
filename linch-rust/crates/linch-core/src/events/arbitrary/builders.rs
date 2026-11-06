//! One proptest strategy per [`super::super::Event`] kind, plus the model-
//! call journal kinds. Split out from `arbitrary/mod.rs` to stay near this
//! crate's per-file line target; the shared leaf strategies these build on
//! (`short_string`, `json_object`, `usage`, ...) live there.
#![cfg(test)]

use super::*;
use crate::events::kinds::*;
use crate::events::model_call::*;
use crate::message::ToolResultBlock;
use proptest::collection::vec;
use proptest::option;
use proptest::prelude::*;

pub(super) fn init_event() -> impl Strategy<Value = InitEvent> {
    (
        session_id(),
        run_id(),
        option::of(run_id()),
        short_string(),
        vec(short_string(), 0..3),
        option::of(short_string()),
    )
        .prop_map(
            |(session_id, run_id, parent_run_id, model, tools, trace_id)| InitEvent {
                session_id,
                run_id,
                parent_run_id,
                model,
                tools,
                trace_id,
            },
        )
}

pub(super) fn user_event() -> impl Strategy<Value = UserEvent> {
    (
        message(),
        prop_oneof![
            Just(UserEventSubtype::Prompt),
            Just(UserEventSubtype::ToolResult),
            Just(UserEventSubtype::Alignment),
            Just(UserEventSubtype::Notification),
        ],
    )
        .prop_map(|(message, subtype)| UserEvent { message, subtype })
}

pub(super) fn assistant_event() -> impl Strategy<Value = AssistantEvent> {
    (message(), stop_reason()).prop_map(|(message, stop_reason)| AssistantEvent {
        message,
        stop_reason,
    })
}

pub(super) fn partial_assistant_event() -> impl Strategy<Value = PartialAssistantEvent> {
    json_object(1).prop_map(|delta| PartialAssistantEvent { delta })
}

pub(super) fn usage_event() -> impl Strategy<Value = UsageEvent> {
    (usage(), usage()).prop_map(|(usage, cumulative)| UsageEvent { usage, cumulative })
}

pub(super) fn tool_call_start_event() -> impl Strategy<Value = ToolCallStartEvent> {
    (
        short_string(),
        short_string(),
        json_object(1),
        short_string(),
    )
        .prop_map(
            |(tool_use_id, tool_name, input, summary)| ToolCallStartEvent {
                tool_use_id,
                tool_name,
                input,
                summary,
            },
        )
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
        option::of(non_null_json_value(1)),
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

pub(super) fn tool_progress_event() -> impl Strategy<Value = ToolProgressEvent> {
    (
        short_string(),
        short_string(),
        short_string(),
        option::of(json_object(1)),
    )
        .prop_map(
            |(tool_use_id, tool_name, message, data)| ToolProgressEvent {
                tool_use_id,
                tool_name,
                message,
                data,
            },
        )
}

pub(super) fn permission_request_event() -> impl Strategy<Value = PermissionRequestEvent> {
    vec(
        (
            short_string(),
            short_string(),
            json_object(1),
            short_string(),
        )
            .prop_map(
                |(tool_use_id, tool_name, input, summary)| PermissionRequestItem {
                    tool_use_id,
                    tool_name,
                    input,
                    summary,
                },
            ),
        0..3,
    )
    .prop_map(|requests| PermissionRequestEvent { requests })
}

pub(super) fn input_request_event() -> impl Strategy<Value = InputRequestEvent> {
    (
        short_string(),
        prop_oneof![
            Just(InputRequestKind::AskUser),
            Just(InputRequestKind::WorkflowInterrupt)
        ],
        short_string(),
    )
        .prop_map(|(request_id, kind, prompt)| InputRequestEvent {
            request_id,
            kind,
            prompt,
        })
}

pub(super) fn suspended_event() -> impl Strategy<Value = SuspendedEvent> {
    (
        prop_oneof![
            Just(SuspendStatus::AwaitingApproval),
            Just(SuspendStatus::AwaitingInput)
        ],
        vec(short_string(), 0..3),
    )
        .prop_map(|(status, pending_request_ids)| SuspendedEvent {
            status,
            pending_request_ids,
        })
}

pub(super) fn context_build_event() -> impl Strategy<Value = ContextBuildEvent> {
    (
        any::<u64>(),
        any::<u64>(),
        option::of(vec(short_string(), 0..3)),
        json_object(1),
        json_object(1),
    )
        .prop_map(
            |(system_blocks, messages, selected_tools, budget, metadata)| ContextBuildEvent {
                system_blocks,
                messages,
                selected_tools,
                budget,
                metadata,
            },
        )
}

pub(super) fn compaction_event() -> impl Strategy<Value = CompactionEvent> {
    (
        any::<u64>(),
        any::<u64>(),
        any::<u64>(),
        any::<u64>(),
        short_string(),
    )
        .prop_map(
            |(messages_before, messages_after, tokens_before, tokens_after, strategy)| {
                CompactionEvent {
                    messages_before,
                    messages_after,
                    tokens_before,
                    tokens_after,
                    strategy,
                }
            },
        )
}

pub(super) fn loop_guard_event() -> impl Strategy<Value = LoopGuardEvent> {
    (
        prop_oneof![
            Just(LoopGuardReason::RepeatedToolCall),
            Just(LoopGuardReason::RepeatedFailures),
            Just(LoopGuardReason::MaxTurns),
        ],
        short_string(),
        prop_oneof![
            Just(LoopGuardAction::Stop),
            Just(LoopGuardAction::ForceFinal)
        ],
    )
        .prop_map(|(reason, detail, action)| LoopGuardEvent {
            reason,
            detail,
            action,
        })
}

pub(super) fn budget_event() -> impl Strategy<Value = BudgetEvent> {
    (
        prop_oneof![Just(BudgetKind::Warning), Just(BudgetKind::Exceeded)],
        any::<u64>(),
        finite_f64(),
        option::of(any::<u64>()),
        option::of(finite_f64()),
    )
        .prop_map(
            |(kind, spent_tokens, spent_usd, max_tokens, max_cost_usd)| BudgetEvent {
                kind,
                spent_tokens,
                spent_usd,
                max_tokens,
                max_cost_usd,
            },
        )
}

pub(super) fn prompt_cache_advisory_event() -> impl Strategy<Value = PromptCacheAdvisoryEvent> {
    (
        prop_oneof![
            Just(PromptCacheAdvisoryReason::ToolSetChanged),
            Just(PromptCacheAdvisoryReason::ModelChanged)
        ],
        short_string(),
    )
        .prop_map(|(reason, detail)| PromptCacheAdvisoryEvent { reason, detail })
}

pub(super) fn skills_loaded_event() -> impl Strategy<Value = SkillsLoadedEvent> {
    vec(json_object(1), 0..3).prop_map(|skills| SkillsLoadedEvent { skills })
}

pub(super) fn skill_invoked_event() -> impl Strategy<Value = SkillInvokedEvent> {
    (
        short_string(),
        option::of(short_string()),
        option::of(short_string()),
        option::of(vec(short_string(), 0..3)),
    )
        .prop_map(
            |(name, args, model_override, allowed_tools)| SkillInvokedEvent {
                name,
                args,
                model_override,
                allowed_tools,
            },
        )
}

pub(super) fn skill_completed_event() -> impl Strategy<Value = SkillCompletedEvent> {
    (short_string(), any::<bool>())
        .prop_map(|(name, is_error)| SkillCompletedEvent { name, is_error })
}

pub(super) fn child_run_status_event() -> impl Strategy<Value = ChildRunStatusEvent> {
    (run_id(), run_status()).prop_map(|(child_run_id, status)| ChildRunStatusEvent {
        child_run_id,
        status,
    })
}

pub(super) fn workflow_event() -> impl Strategy<Value = WorkflowEvent> {
    (
        prop_oneof![
            Just(WorkflowKind::Phase),
            Just(WorkflowKind::AgentStart),
            Just(WorkflowKind::AgentEnd),
            Just(WorkflowKind::AgentReplayed),
            Just(WorkflowKind::StepStart),
            Just(WorkflowKind::StepEnd),
            Just(WorkflowKind::StepReplayed),
            Just(WorkflowKind::InterruptRequested),
            Just(WorkflowKind::InterruptResolved),
            Just(WorkflowKind::InterruptReplayed),
        ],
        short_string(),
        short_string(),
        any::<u64>(),
        short_string(),
        option::of(short_string()),
        option::of(json_object(1)),
        option::of(short_string()),
    )
        .prop_map(
            |(
                kind,
                title,
                call_key,
                occurrence,
                subagent_type,
                result_text,
                structured_output,
                structured_error,
            )| {
                WorkflowEvent {
                    kind,
                    title,
                    call_key,
                    occurrence,
                    subagent_type,
                    result_text,
                    structured_output,
                    structured_error,
                }
            },
        )
}

pub(super) fn verification_event() -> impl Strategy<Value = VerificationEvent> {
    (
        short_string(),
        prop_oneof![
            Just(VerificationAction::Retry),
            Just(VerificationAction::Stop),
            Just(VerificationAction::Exhausted)
        ],
        short_string(),
        any::<u64>(),
    )
        .prop_map(|(verifier, action, feedback, attempt)| VerificationEvent {
            verifier,
            action,
            feedback,
            attempt,
        })
}

pub(super) fn error_event() -> impl Strategy<Value = ErrorEvent> {
    json_object(1).prop_map(|error| ErrorEvent { error })
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
