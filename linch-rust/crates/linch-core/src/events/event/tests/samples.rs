//! Hand-built sample events, split out from `tests.rs` to stay near the
//! crate's per-file line target.
//!
//! [`one_of_every_kind`] is one minimal, valid instance of every `Event`
//! kind, built by hand (not via `arbitrary`) so this list is a deliberate,
//! readable enumeration a reviewer can check against the card's kind
//! list. Several tests in the parent module use it to check exhaustive
//! per-kind behavior (persistence classification, the type-tag drift
//! guard, and the decode round trip) against a single, shared sample set.

use super::*;

/// One minimal, valid instance of every `Event` kind.
pub(super) fn one_of_every_kind() -> Vec<Event> {
    use crate::events::model_call::{
        CallDisposition, ModelCallDeliveryEvent, ModelCallKind, ModelCallReceiptEvent,
        ModelCallUsageSettlementEvent,
    };
    use crate::message::ToolResultBlock;
    use crate::run::{RunStatus, StopReason};
    use crate::usage::Usage;

    vec![
        Event::Init(InitEvent {
            session_id: crate::ids::SessionId::new("s1"),
            run_id: RunId::new("r1"),
            parent_run_id: None,
            model: "claude".into(),
            provider: "anthropic".into(),
            tools: vec![],
            trace_id: None,
            resumed: true,
            turn: 3,
        }),
        Event::User(UserEvent {
            message: crate::message::Message::user_text("hi"),
            subtype: UserEventSubtype::Prompt,
        }),
        Event::Assistant(AssistantEvent {
            message: crate::message::Message::assistant_text("hi"),
            stop_reason: StopReason::EndTurn,
        }),
        Event::PartialAssistant(PartialAssistantEvent {
            delta: JsonObject::new(),
        }),
        Event::Usage(UsageEvent {
            usage: Usage::default(),
            cumulative: Usage::default(),
            cost_usd: Some(0.25),
            cumulative_cost_usd: Some(1.5),
            actual_model: Some("claude-served".into()),
        }),
        Event::ToolCallStart(ToolCallStartEvent {
            tool_use_id: "c1".into(),
            tool_name: "Read".into(),
            input: JsonObject::new(),
            summary: "reading".into(),
        }),
        Event::ToolCallEnd(ToolCallEndEvent {
            tool_name: "Read".into(),
            result: ToolResultBlock::text("c1", "done", false),
            duration_ms: 1,
            canonical_output: None,
            metadata: None,
            error_code: Some("not_found".into()),
        }),
        Event::ToolProgress(ToolProgressEvent {
            tool_use_id: "c1".into(),
            tool_name: "Read".into(),
            message: "working".into(),
            data: None,
        }),
        Event::PermissionRequest(PermissionRequestEvent { requests: vec![] }),
        Event::InputRequest(InputRequestEvent {
            request_id: "req1".into(),
            kind: InputRequestKind::AskUser,
            prompt: "continue?".into(),
        }),
        Event::Suspended(SuspendedEvent {
            status: SuspendStatus::AwaitingApproval,
            pending_request_ids: vec!["c1".into()],
        }),
        Event::ContextBuild(ContextBuildEvent {
            system_blocks: 1,
            messages: 1,
            selected_tools: None,
            budget: JsonObject::new(),
            metadata: JsonObject::new(),
            model: "m".into(),
        }),
        Event::Compaction(CompactionEvent {
            messages_before: 10,
            messages_after: 2,
            tokens_before: 100,
            tokens_after: 10,
            strategy: "summarize".into(),
            duration_ms: 7,
        }),
        Event::LoopGuard(LoopGuardEvent {
            reason: LoopGuardReason::MaxTurns,
            detail: "too many turns".into(),
            action: LoopGuardAction::Stop,
        }),
        Event::Budget(BudgetEvent {
            kind: BudgetKind::Warning,
            spent_tokens: 1,
            spent_usd: 0.1,
            max_tokens: None,
            max_cost_usd: None,
        }),
        Event::PromptCacheAdvisory(PromptCacheAdvisoryEvent {
            reason: PromptCacheAdvisoryReason::ModelChanged,
            detail: "model changed".into(),
        }),
        Event::SkillsLoaded(SkillsLoadedEvent { skills: vec![] }),
        Event::SkillInvoked(SkillInvokedEvent {
            name: "deploy".into(),
            args: None,
            model_override: None,
            allowed_tools: None,
        }),
        Event::SkillCompleted(SkillCompletedEvent {
            name: "deploy".into(),
            is_error: false,
        }),
        Event::Subagent(SubagentEvent {
            child_run_id: RunId::new("child-1"),
            event: Box::new(Event::Error(ErrorEvent {
                error: JsonObject::new(),
            })),
        }),
        Event::ChildRunStatus(ChildRunStatusEvent {
            child_run_id: RunId::new("child-1"),
            status: RunStatus::Running,
        }),
        Event::Workflow(WorkflowEvent {
            kind: WorkflowKind::Phase,
            title: String::new(),
            call_key: String::new(),
            occurrence: 0,
            subagent_type: String::new(),
            result_text: None,
            structured_output: None,
            structured_error: None,
        }),
        Event::Verification(VerificationEvent {
            verifier: "output_schema".into(),
            action: VerificationAction::Retry,
            feedback: String::new(),
            attempt: 1,
        }),
        Event::Hook(HookEvent {
            point: crate::policy::HookPoint::PreToolUse,
            policy_id: "cache".into(),
            policy_version: 1,
            tool_use_id: Some("t1".into()),
            outcome: HookOutcome::Answered {
                verdict: "continue".into(),
            },
        }),
        Event::Error(ErrorEvent {
            error: JsonObject::new(),
        }),
        Event::Result(ResultEvent {
            subtype: ResultSubtype::Success,
            stop_reason: StopReason::EndTurn,
            total_usage: Usage::default(),
            usage_complete: true,
            duration_ms: 0,
            total_cost_usd: Some(1.5),
            final_text: None,
            structured_output: None,
            structured_error: None,
            trace_id: None,
        }),
        Event::ModelCallDelivery(ModelCallDeliveryEvent {
            run_id: RunId::new("r1"),
            logical_call_id: "lc1".into(),
            delivery_attempt_id: "da1".into(),
            kind: ModelCallKind::Primary,
            turn_index: None,
            requested_model: "claude".into(),
        }),
        Event::ModelCallReceipt(ModelCallReceiptEvent {
            run_id: RunId::new("r1"),
            logical_call_id: "lc1".into(),
            delivery_attempt_id: "da1".into(),
            kind: ModelCallKind::Primary,
            actual_model: None,
            disposition: CallDisposition::Cancelled,
            usage: None,
            cost_usd: None,
            duration_ms: 0,
        }),
        Event::ModelCallUsageSettlement(ModelCallUsageSettlementEvent {
            run_id: RunId::new("r1"),
            budget_owner_id: None,
            logical_call_id: "lc1".into(),
            usage: Usage::default(),
            cost_usd: None,
        }),
        Event::Ignorable(IgnorableEvent::new("future_kind", JsonObject::new()).unwrap()),
    ]
}
