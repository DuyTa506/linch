//! Test-only proptest strategies for building arbitrary [`super::Event`]
//! values (and the few foundation types events embed).
//!
//! The foundation crate (`json.rs`, `ids.rs`, `message.rs`, `usage.rs`,
//! `run.rs`) ships no `Arbitrary`/strategy helpers of its own yet, so the
//! small building blocks here (`finite_f64`, `usage`, `message`,
//! `json_value`) are candidates for a shared test-support module if W2
//! (session log) or W3 (provider stream) need the same ones — see W1 notes.
//!
//! One strategy per [`super::Event`] kind lives in [`builders`] or, for the
//! accounting kinds, [`accounting`], split out
//! to stay near this crate's per-file line target.
#![cfg(test)]

mod accounting;
mod builders;

use super::event::{Event, IgnorableEvent, SubagentEvent};
use super::kinds::{HookEvent, HookOutcome};
use crate::ids::{RunId, SessionId};
use crate::json::{JsonObject, JsonValue};
use crate::message::{ContentBlock, Message, Role};
use crate::policy::{FailMode, HookPoint};
use crate::run::{RunStatus, StopReason};
use crate::usage::Usage;
use proptest::collection::vec;
use proptest::prelude::*;
use proptest::strategy::Union;

/// Short, readable, JSON-safe strings — plenty to exercise the codec
/// without proptest spending its budget on unicode edge cases that
/// `message.rs`/`json.rs` already cover.
fn short_string() -> impl Strategy<Value = String> {
    "[a-zA-Z0-9 _-]{0,12}"
}

/// A finite, non-negative monetary amount. `f64::NAN`/`INFINITY`, which
/// `any::<f64>()` can produce, are not valid JSON numbers; encoding one is
/// now a hard error (`wire::serialize_finite_f64`/`_option`), which this
/// round-trip generator does not want to exercise — it only produces
/// values this crate can actually encode. The rejection path itself has
/// its own dedicated unit tests next to the fields that use it.
fn finite_f64() -> impl Strategy<Value = f64> {
    (0u32..1_000_000u32).prop_map(|cents| cents as f64 / 100.0)
}

fn run_id() -> impl Strategy<Value = RunId> {
    short_string().prop_map(RunId::new)
}

fn session_id() -> impl Strategy<Value = SessionId> {
    short_string().prop_map(SessionId::new)
}

fn usage() -> impl Strategy<Value = Usage> {
    (any::<u64>(), any::<u64>(), any::<u64>(), any::<u64>()).prop_map(
        |(input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens)| Usage {
            input_tokens,
            output_tokens,
            cache_read_tokens,
            cache_creation_tokens,
        },
    )
}

/// A small representative `Message`: one text block. `ContentBlock`'s own
/// variants are already proven to round-trip by `message.rs`'s tests; this
/// only needs to produce *a* valid message to embed.
fn message() -> impl Strategy<Value = Message> {
    (
        prop_oneof![Just(Role::User), Just(Role::Assistant)],
        short_string(),
    )
        .prop_map(|(role, text)| Message::new(role, vec![ContentBlock::text(text)]))
}

fn stop_reason() -> impl Strategy<Value = StopReason> {
    prop_oneof![
        Just(StopReason::EndTurn),
        Just(StopReason::ToolUse),
        Just(StopReason::MaxTokens),
        Just(StopReason::StopSequence),
        Just(StopReason::Refusal),
        Just(StopReason::Error),
        Just(StopReason::LeaseLost),
        Just(StopReason::Interrupted),
    ]
}

fn run_status() -> impl Strategy<Value = RunStatus> {
    prop_oneof![
        Just(RunStatus::Pending),
        Just(RunStatus::Running),
        Just(RunStatus::AwaitingApproval),
        Just(RunStatus::AwaitingInput),
        Just(RunStatus::Completed),
        Just(RunStatus::Failed),
        Just(RunStatus::Aborted),
    ]
}

/// A JSON value with bounded depth, so recursive generation terminates.
fn json_value(depth: u32) -> BoxedStrategy<JsonValue> {
    let leaf = prop_oneof![
        Just(JsonValue::Null),
        any::<bool>().prop_map(JsonValue::Bool),
        any::<u32>().prop_map(|n| JsonValue::Number(n.into())),
        short_string().prop_map(JsonValue::String),
    ];
    if depth == 0 {
        leaf.boxed()
    } else {
        let inner = json_value(depth - 1);
        prop_oneof![
            leaf,
            vec(inner.clone(), 0..3).prop_map(JsonValue::Array),
            vec((short_string(), inner), 0..3).prop_map(|pairs| {
                let mut obj = JsonObject::new();
                for (key, value) in pairs {
                    obj.insert(key, value);
                }
                JsonValue::Object(obj)
            }),
        ]
        .boxed()
    }
}

fn json_object(depth: u32) -> impl Strategy<Value = JsonObject> {
    vec((short_string(), json_value(depth)), 0..3).prop_map(|pairs| {
        let mut obj = JsonObject::new();
        for (key, value) in pairs {
            obj.insert(key, value);
        }
        obj
    })
}

/// An arbitrary ignorable event: an unknown type with a few scalar fields.
fn ignorable_event() -> impl Strategy<Value = Event> {
    ("[a-z_]{1,10}", json_object(1)).prop_map(|(type_name, raw)| {
        // `type_name` is a plain lowercase word, never one of
        // `KNOWN_EVENT_TYPES`'s wire tags, so `IgnorableEvent::new` always
        // succeeds here.
        Event::Ignorable(IgnorableEvent::new(type_name, raw).expect("not a known event type"))
    })
}

/// An arbitrary `hook` record. Kept here rather than in [`builders`],
/// which is already past the per-file line target.
fn hook_event() -> impl Strategy<Value = HookEvent> {
    let point = prop_oneof![
        Just(HookPoint::UserPromptSubmit),
        Just(HookPoint::ContextBuild),
        Just(HookPoint::PreToolUse),
        Just(HookPoint::PermissionRequest),
        Just(HookPoint::PostToolUse),
        Just(HookPoint::Stop),
        Just(HookPoint::PostTurn),
    ];
    let fail_mode = prop_oneof![Just(FailMode::Open), Just(FailMode::Closed)];
    let outcome = prop_oneof![
        short_string().prop_map(|verdict| HookOutcome::Answered { verdict }),
        (short_string(), fail_mode.clone())
            .prop_map(|(error, fail_mode)| HookOutcome::Failed { error, fail_mode }),
        (any::<u64>(), fail_mode).prop_map(|(timeout_ms, fail_mode)| HookOutcome::TimedOut {
            timeout_ms,
            fail_mode
        }),
    ];
    let tool_use_id = proptest::option::of(short_string());
    (point, short_string(), any::<u32>(), tool_use_id, outcome).prop_map(
        |(point, policy_id, policy_version, tool_use_id, outcome)| HookEvent {
            point,
            policy_id,
            policy_version,
            tool_use_id,
            outcome,
        },
    )
}

/// An arbitrary event, including nested `subagent` wrapping up to
/// `depth_budget` levels deep (0 means "never nest").
pub(super) fn event(depth_budget: u32) -> BoxedStrategy<Event> {
    let mut variants: Vec<BoxedStrategy<Event>> = vec![
        builders::init_event().prop_map(Event::Init).boxed(),
        builders::user_event().prop_map(Event::User).boxed(),
        builders::assistant_event()
            .prop_map(Event::Assistant)
            .boxed(),
        builders::partial_assistant_event()
            .prop_map(Event::PartialAssistant)
            .boxed(),
        accounting::usage_event().prop_map(Event::Usage).boxed(),
        builders::tool_call_start_event()
            .prop_map(Event::ToolCallStart)
            .boxed(),
        accounting::tool_call_end_event()
            .prop_map(Event::ToolCallEnd)
            .boxed(),
        builders::tool_progress_event()
            .prop_map(Event::ToolProgress)
            .boxed(),
        builders::permission_request_event()
            .prop_map(Event::PermissionRequest)
            .boxed(),
        builders::input_request_event()
            .prop_map(Event::InputRequest)
            .boxed(),
        builders::suspended_event()
            .prop_map(Event::Suspended)
            .boxed(),
        builders::context_build_event()
            .prop_map(Event::ContextBuild)
            .boxed(),
        builders::compaction_event()
            .prop_map(Event::Compaction)
            .boxed(),
        builders::loop_guard_event()
            .prop_map(Event::LoopGuard)
            .boxed(),
        builders::budget_event().prop_map(Event::Budget).boxed(),
        builders::prompt_cache_advisory_event()
            .prop_map(Event::PromptCacheAdvisory)
            .boxed(),
        builders::skills_loaded_event()
            .prop_map(Event::SkillsLoaded)
            .boxed(),
        builders::skill_invoked_event()
            .prop_map(Event::SkillInvoked)
            .boxed(),
        builders::skill_completed_event()
            .prop_map(Event::SkillCompleted)
            .boxed(),
        builders::child_run_status_event()
            .prop_map(Event::ChildRunStatus)
            .boxed(),
        builders::workflow_event().prop_map(Event::Workflow).boxed(),
        builders::verification_event()
            .prop_map(Event::Verification)
            .boxed(),
        hook_event().prop_map(Event::Hook).boxed(),
        builders::error_event().prop_map(Event::Error).boxed(),
        accounting::result_event().prop_map(Event::Result).boxed(),
        accounting::model_call_delivery_event()
            .prop_map(Event::ModelCallDelivery)
            .boxed(),
        accounting::model_call_receipt_event()
            .prop_map(Event::ModelCallReceipt)
            .boxed(),
        accounting::model_call_usage_settlement_event()
            .prop_map(Event::ModelCallUsageSettlement)
            .boxed(),
        ignorable_event().boxed(),
    ];
    if depth_budget > 0 {
        variants.push(
            (
                run_id(),
                short_string(),
                any::<u64>(),
                event(depth_budget - 1),
            )
                .prop_map(|(child_run_id, tool_use_id, ts_ms, inner)| {
                    Event::Subagent(SubagentEvent {
                        child_run_id,
                        tool_use_id,
                        ts_ms,
                        event: Box::new(inner),
                    })
                })
                .boxed(),
        );
    }
    Union::new(variants).boxed()
}
