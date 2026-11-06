//! The [`Event`] sum type: one payload struct per kind, dispatched by hand
//! around a `type` wire tag so an unknown tag can fall back to
//! [`IgnorableEvent`] instead of failing the whole decode.
//!
//! Every payload struct/enum in [`super::kinds`] and [`super::model_call`]
//! derives `Serialize`/`Deserialize` normally (`deny_unknown_fields`, no
//! `type` field of its own). Only `Event` itself needs a hand-written
//! codec: [`Event::to_value`] injects the tag per variant, and
//! [`Event::decode`] strips it before delegating to the payload's own
//! derive. `impl Deserialize for Event` is a convenience wrapper around
//! `decode` for a bare, non-enveloped event (always strict, depth 0); the
//! primary typed entry point is [`super::Envelope::from_value`].

mod ignorable;
#[cfg(test)]
mod tests;

pub use ignorable::IgnorableEvent;

use super::envelope::{DecodeCtx, EventError};
use super::kinds::*;
use super::model_call::{
    ModelCallDeliveryEvent, ModelCallReceiptEvent, ModelCallUsageSettlementEvent,
};
use super::wire::{json_kind_name, reject_unknown_fields, take_object, take_string};
use crate::ids::RunId;
use crate::json::{JsonObject, JsonValue};
use serde::{Deserialize, Serialize};

/// Where an event kind is durably persisted.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Persistence {
    /// Kept only on the live stream (e.g. transient progress); never
    /// written to the durable run event log.
    StreamOnly,
    /// Written only to the accounting journal; never streamed to a client.
    JournalOnly,
    /// Both streamed to a client and written to the durable run event log.
    Both,
}

/// One event in a run's stream or journal.
///
/// See the module docs for why `Event` hand-writes its `Serialize`/
/// `Deserialize` instead of deriving them.
#[derive(Debug, Clone, PartialEq)]
pub enum Event {
    /// Run initialization.
    Init(InitEvent),
    /// A user-authored or user-visible message.
    User(UserEvent),
    /// A completed assistant response.
    Assistant(AssistantEvent),
    /// A transient assistant delta while a response streams.
    PartialAssistant(PartialAssistantEvent),
    /// Per-call and cumulative token usage.
    Usage(UsageEvent),
    /// The start of one tool invocation.
    ToolCallStart(ToolCallStartEvent),
    /// The completed result of one tool invocation.
    ToolCallEnd(ToolCallEndEvent),
    /// Transient progress from an in-flight tool invocation.
    ToolProgress(ToolProgressEvent),
    /// A batch of tool calls awaiting permission.
    PermissionRequest(PermissionRequestEvent),
    /// A request for external input (ask-user or workflow interrupt).
    InputRequest(InputRequestEvent),
    /// A run suspended without holding a process.
    Suspended(SuspendedEvent),
    /// The context assembled for one provider request.
    ContextBuild(ContextBuildEvent),
    /// One context-compaction operation.
    Compaction(CompactionEvent),
    /// The loop guard tripped, or `max_turns` was reached.
    LoopGuard(LoopGuardEvent),
    /// A run budget crossed its warning ratio or was exhausted.
    Budget(BudgetEvent),
    /// A prefix-breaking change invalidated the provider's prompt cache.
    PromptCacheAdvisory(PromptCacheAdvisoryEvent),
    /// The skills loaded for a request or session.
    SkillsLoaded(SkillsLoadedEvent),
    /// A skill invocation.
    SkillInvoked(SkillInvokedEvent),
    /// Completion of a skill invocation.
    SkillCompleted(SkillCompletedEvent),
    /// An event emitted by a subagent run, wrapping the child's event.
    Subagent(SubagentEvent),
    /// A lifecycle status change on a child run.
    ChildRunStatus(ChildRunStatusEvent),
    /// Progress/journal event emitted by the workflow engine.
    Workflow(WorkflowEvent),
    /// A closed-loop gate acted on a would-be-final answer.
    Verification(VerificationEvent),
    /// One intercept-hook call. Stream-only.
    Hook(HookEvent),
    /// A terminal or recoverable error payload.
    Error(ErrorEvent),
    /// The terminal outcome of a run.
    Result(ResultEvent),
    /// A model-call delivery attempt was dispatched. Journal-only.
    ModelCallDelivery(ModelCallDeliveryEvent),
    /// The outcome of one model-call delivery attempt. Journal-only.
    ModelCallReceipt(ModelCallReceiptEvent),
    /// A settled receipt's usage was attributed to a budget owner.
    /// Journal-only.
    ModelCallUsageSettlement(ModelCallUsageSettlementEvent),
    /// An event of a type this build does not know, kept for pass-through.
    Ignorable(IgnorableEvent),
}

/// An event emitted by a subagent run, wrapping the child's event.
///
/// Defined here, alongside [`Event`], because its `event` field is `Event`
/// itself — [`super::kinds`] holds every other payload type, which have no
/// such cycle.
#[derive(Debug, Clone, PartialEq)]
pub struct SubagentEvent {
    /// The subagent's run.
    pub child_run_id: RunId,
    /// The event the subagent emitted.
    pub event: Box<Event>,
}

/// Serialize a payload, then inject the wire `type` tag.
///
/// Fails if `payload`'s `Serialize` implementation does — the one way any
/// of this crate's plain derived payload types do is a monetary
/// `f64`/`Option<f64>` field rejecting a non-finite value (see
/// `wire::serialize_finite_f64`).
fn tagged<T: Serialize>(tag: &'static str, payload: &T) -> Result<JsonValue, EventError> {
    let mut value = serde_json::to_value(payload).map_err(|source| EventError::EncodePayload {
        type_name: tag.to_string(),
        source,
    })?;
    if let JsonValue::Object(map) = &mut value {
        map.insert("type".to_string(), JsonValue::String(tag.to_string()));
    }
    Ok(value)
}

/// Decode a known event's payload, stripping `type` first so the payload's
/// own `deny_unknown_fields` does not reject its own tag.
fn payload<T: serde::de::DeserializeOwned>(
    type_name: &'static str,
    obj: JsonObject,
) -> Result<T, EventError> {
    serde_json::from_value(JsonValue::Object(obj)).map_err(|source| EventError::InvalidPayload {
        type_name: type_name.to_string(),
        source,
    })
}

impl Event {
    /// Decode an event from an owned JSON value under the given context.
    ///
    /// A known `type` always decodes strictly: a malformed payload is an
    /// error regardless of `ctx.ignorable`. An unknown `type` decodes to
    /// [`IgnorableEvent`] if `ctx.ignorable`, else errors. `subagent` carries
    /// `ctx` into its nested event unchanged except for the depth bound.
    pub(super) fn decode(value: JsonValue, ctx: DecodeCtx) -> Result<Event, EventError> {
        let mut obj = take_object(value)?;
        // Removed once up front (rather than per match arm): every known
        // arm's payload uses `deny_unknown_fields`, so `type` must not reach
        // it; the unknown-tag arm below re-inserts it for `IgnorableEvent`'s
        // verbatim `raw`.
        let type_value = obj.remove("type").ok_or(EventError::MissingField("type"))?;
        let type_name = match type_value {
            JsonValue::String(s) => s,
            other => {
                return Err(EventError::InvalidField {
                    field: "type",
                    expected: "a string",
                    found: json_kind_name(&other),
                });
            }
        };
        match type_name.as_str() {
            "init" => Ok(Event::Init(payload("init", obj)?)),
            "user" => Ok(Event::User(payload("user", obj)?)),
            "assistant" => Ok(Event::Assistant(payload("assistant", obj)?)),
            "partial_assistant" => Ok(Event::PartialAssistant(payload("partial_assistant", obj)?)),
            "usage" => Ok(Event::Usage(payload("usage", obj)?)),
            "tool_call_start" => Ok(Event::ToolCallStart(payload("tool_call_start", obj)?)),
            "tool_call_end" => Ok(Event::ToolCallEnd(payload("tool_call_end", obj)?)),
            "tool_progress" => Ok(Event::ToolProgress(payload("tool_progress", obj)?)),
            "permission_request" => Ok(Event::PermissionRequest(payload(
                "permission_request",
                obj,
            )?)),
            "input_request" => Ok(Event::InputRequest(payload("input_request", obj)?)),
            "suspended" => Ok(Event::Suspended(payload("suspended", obj)?)),
            "context_build" => Ok(Event::ContextBuild(payload("context_build", obj)?)),
            "compaction" => Ok(Event::Compaction(payload("compaction", obj)?)),
            "loop_guard" => Ok(Event::LoopGuard(payload("loop_guard", obj)?)),
            "budget" => Ok(Event::Budget(payload("budget", obj)?)),
            "prompt_cache_advisory" => Ok(Event::PromptCacheAdvisory(payload(
                "prompt_cache_advisory",
                obj,
            )?)),
            "skills_loaded" => Ok(Event::SkillsLoaded(payload("skills_loaded", obj)?)),
            "skill_invoked" => Ok(Event::SkillInvoked(payload("skill_invoked", obj)?)),
            "skill_completed" => Ok(Event::SkillCompleted(payload("skill_completed", obj)?)),
            "subagent" => {
                let child_run_id = RunId::from(take_string(&mut obj, "child_run_id")?);
                let nested_value = obj
                    .remove("event")
                    .ok_or(EventError::MissingField("event"))?;
                reject_unknown_fields(obj)?;
                let nested = Event::decode(nested_value, ctx.nested()?)?;
                Ok(Event::Subagent(SubagentEvent {
                    child_run_id,
                    event: Box::new(nested),
                }))
            }
            "child_run_status" => Ok(Event::ChildRunStatus(payload("child_run_status", obj)?)),
            "workflow" => Ok(Event::Workflow(payload("workflow", obj)?)),
            "verification" => Ok(Event::Verification(payload("verification", obj)?)),
            "hook" => Ok(Event::Hook(payload("hook", obj)?)),
            "error" => Ok(Event::Error(payload("error", obj)?)),
            "result" => Ok(Event::Result(payload("result", obj)?)),
            "model_call_delivery" => Ok(Event::ModelCallDelivery(payload(
                "model_call_delivery",
                obj,
            )?)),
            "model_call_receipt" => {
                Ok(Event::ModelCallReceipt(payload("model_call_receipt", obj)?))
            }
            "model_call_usage_settlement" => Ok(Event::ModelCallUsageSettlement(payload(
                "model_call_usage_settlement",
                obj,
            )?)),
            other => {
                if ctx.ignorable {
                    // `obj` had its original "type" field removed above;
                    // restore it so the sentinel's `raw` is the untouched
                    // source object.
                    obj.insert("type".to_string(), JsonValue::String(other.to_string()));
                    Ok(Event::Ignorable(IgnorableEvent::new_trusted(
                        other.to_string(),
                        obj,
                    )))
                } else {
                    Err(EventError::UnknownType(other.to_string()))
                }
            }
        }
    }

    /// Encode this event to its JSON wire representation.
    ///
    /// Raises:
    ///     EventError: If a payload field that must encode to a finite JSON
    ///         number (for example a monetary `f64`) does not.
    pub fn to_value(&self) -> Result<JsonValue, EventError> {
        match self {
            Event::Init(e) => tagged("init", e),
            Event::User(e) => tagged("user", e),
            Event::Assistant(e) => tagged("assistant", e),
            Event::PartialAssistant(e) => tagged("partial_assistant", e),
            Event::Usage(e) => tagged("usage", e),
            Event::ToolCallStart(e) => tagged("tool_call_start", e),
            Event::ToolCallEnd(e) => tagged("tool_call_end", e),
            Event::ToolProgress(e) => tagged("tool_progress", e),
            Event::PermissionRequest(e) => tagged("permission_request", e),
            Event::InputRequest(e) => tagged("input_request", e),
            Event::Suspended(e) => tagged("suspended", e),
            Event::ContextBuild(e) => tagged("context_build", e),
            Event::Compaction(e) => tagged("compaction", e),
            Event::LoopGuard(e) => tagged("loop_guard", e),
            Event::Budget(e) => tagged("budget", e),
            Event::PromptCacheAdvisory(e) => tagged("prompt_cache_advisory", e),
            Event::SkillsLoaded(e) => tagged("skills_loaded", e),
            Event::SkillInvoked(e) => tagged("skill_invoked", e),
            Event::SkillCompleted(e) => tagged("skill_completed", e),
            Event::Subagent(e) => {
                let mut obj = JsonObject::new();
                obj.insert(
                    "type".to_string(),
                    JsonValue::String("subagent".to_string()),
                );
                obj.insert(
                    "child_run_id".to_string(),
                    JsonValue::String(e.child_run_id.as_str().to_string()),
                );
                obj.insert("event".to_string(), e.event.to_value()?);
                Ok(JsonValue::Object(obj))
            }
            Event::ChildRunStatus(e) => tagged("child_run_status", e),
            Event::Workflow(e) => tagged("workflow", e),
            Event::Verification(e) => tagged("verification", e),
            Event::Hook(e) => tagged("hook", e),
            Event::Error(e) => tagged("error", e),
            Event::Result(e) => tagged("result", e),
            Event::ModelCallDelivery(e) => tagged("model_call_delivery", e),
            Event::ModelCallReceipt(e) => tagged("model_call_receipt", e),
            Event::ModelCallUsageSettlement(e) => tagged("model_call_usage_settlement", e),
            Event::Ignorable(e) => Ok(JsonValue::Object(e.raw().clone())),
        }
    }

    /// Where this event kind is durably persisted.
    ///
    /// Exhaustive by variant (no wildcard arm): adding an `Event` kind
    /// without classifying it here is a compile error, not a silent
    /// misclassification. `Subagent` is not a kind of its own: it recurses
    /// into the wrapped child event, so a wrapped `ToolProgress` is
    /// `StreamOnly` and a wrapped `ModelCallReceipt` is `JournalOnly`, not
    /// `Both`.
    pub fn persistence(&self) -> Persistence {
        use Persistence::*;
        match self {
            Event::ToolProgress(_) => StreamOnly,
            Event::PartialAssistant(_) => StreamOnly,
            Event::Hook(_) => StreamOnly,
            Event::ModelCallDelivery(_) => JournalOnly,
            Event::ModelCallReceipt(_) => JournalOnly,
            Event::ModelCallUsageSettlement(_) => JournalOnly,
            Event::Init(_) => Both,
            Event::User(_) => Both,
            Event::Assistant(_) => Both,
            Event::Usage(_) => Both,
            Event::ToolCallStart(_) => Both,
            Event::ToolCallEnd(_) => Both,
            Event::PermissionRequest(_) => Both,
            Event::InputRequest(_) => Both,
            Event::Suspended(_) => Both,
            Event::ContextBuild(_) => Both,
            Event::Compaction(_) => Both,
            Event::LoopGuard(_) => Both,
            Event::Budget(_) => Both,
            Event::PromptCacheAdvisory(_) => Both,
            Event::SkillsLoaded(_) => Both,
            Event::SkillInvoked(_) => Both,
            Event::SkillCompleted(_) => Both,
            Event::Subagent(e) => e.event.persistence(),
            Event::ChildRunStatus(_) => Both,
            Event::Workflow(_) => Both,
            Event::Verification(_) => Both,
            Event::Error(_) => Both,
            Event::Result(_) => Both,
            Event::Ignorable(_) => Both,
        }
    }

    /// Whether this event, or a nested `subagent` event, is an
    /// [`IgnorableEvent`] — i.e. whether an enclosing [`super::Envelope`]
    /// must set `ignorable: true` for this event to decode back.
    pub(super) fn contains_ignorable(&self) -> bool {
        match self {
            Event::Ignorable(_) => true,
            Event::Subagent(e) => e.event.contains_ignorable(),
            _ => false,
        }
    }
}

impl Serialize for Event {
    fn serialize<S>(&self, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: serde::Serializer,
    {
        self.to_value()
            .map_err(serde::ser::Error::custom)?
            .serialize(serializer)
    }
}

impl<'de> Deserialize<'de> for Event {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        let value = JsonValue::deserialize(deserializer)?;
        Event::decode(value, DecodeCtx::strict()).map_err(serde::de::Error::custom)
    }
}
