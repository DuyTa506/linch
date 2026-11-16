//! The observe-only points: `agent_start`, `agent_stop`, `turn_start`,
//! `provider_call_start`, `provider_call_stop`, `tool_use_start`,
//! `tool_use_stop`, `post_tool_use_failure`, `pre_compact`, `post_compact`,
//! `subagent_start`, `subagent_stop`, `event_emit`.
//!
//! Each one answers `()` (`{}` on the wire): a hook here cannot change the
//! run, only observe it. Registration refuses a closed fail mode
//! ([`can_fail_closed`](super::hook::can_fail_closed)), so a failure is
//! always recorded and skipped, never turned into a blocking answer. The
//! [`observe_point!`] macro below carries this shared shape; each point adds
//! only its marker, its `HookPoint` and its request type.

use crate::events::Event;
use crate::ids::{RunId, SessionId};
use crate::json::JsonObject;
use crate::run::{RunStatus, StopReason};
use crate::tool::ToolOutputError;
use crate::usage::Usage;

use super::chain::{ChainResult, HookChain, HookRecord};
use super::hook::{Point, sealed::Sealed};
use crate::policy::HookPoint;

/// Define an observe-only point's marker, [`Point`] impl and
/// [`HookChain::run`]/`run_into`. `$request` is the point's request type,
/// defined separately so it can carry its own field docs.
macro_rules! observe_point {
    (
        $(#[$doc:meta])*
        marker: $marker:ident,
        point: $point:ident,
        request: $request:ty,
    ) => {
        $(#[$doc])*
        #[derive(Debug, Clone, Copy)]
        pub struct $marker;

        impl Sealed for $marker {}

        impl Point for $marker {
            const POINT: HookPoint = HookPoint::$point;
            type Request = $request;
            type Response = ();

            fn tool_name(_: &Self::Request) -> Option<&str> {
                None
            }

            fn verdict(_: &Self::Response) -> &'static str {
                "observe"
            }
        }

        impl HookChain<$marker> {
            /// Run every hook in order. This point cannot fail closed;
            /// failures are only recorded.
            pub async fn run(&self, request: &$request) -> ChainResult<()> {
                let mut records = Vec::new();
                self.run_into(request, &mut records).await;
                ChainResult {
                    outcome: (),
                    records,
                }
            }

            /// [`Self::run`], pushing each record into `records` as its hook
            /// returns, so a dropped chain keeps the records of the hooks
            /// that did.
            pub(crate) async fn run_into(
                &self,
                request: &$request,
                records: &mut Vec<HookRecord>,
            ) {
                for entry in self.entries().iter() {
                    let _ = entry.invoke(request, records).await;
                }
            }
        }
    };
}

/// A new run is about to start. Not fired on a resume, and not fired when
/// `user_prompt_submit` blocked the prompt: the run is never created then.
#[derive(Debug, Clone, PartialEq)]
pub struct AgentStartRequest {
    /// Session the run belongs to.
    pub session_id: SessionId,
    /// Run starting.
    pub run_id: RunId,
    /// Model selected for the run.
    pub model: String,
    /// The prompt's text, after `user_prompt_submit`.
    pub prompt: String,
    /// Names of the tools available to the run.
    pub tools: Vec<String>,
}

observe_point! {
    /// Marker for [`HookPoint::AgentStart`].
    marker: AgentStart,
    point: AgentStart,
    request: AgentStartRequest,
}

/// A run reached its terminal result, on every end path.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AgentStopRequest {
    /// Session whose run stopped.
    pub session_id: SessionId,
    /// Run that stopped.
    pub run_id: RunId,
    /// The run's terminal status.
    pub status: RunStatus,
    /// Why the run's last response stopped.
    pub stop_reason: StopReason,
}

observe_point! {
    /// Marker for [`HookPoint::AgentStop`].
    marker: AgentStop,
    point: AgentStop,
    request: AgentStopRequest,
}

/// A turn is about to start, before the compaction/dispatch decision.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TurnStartRequest {
    /// Session executing the turn.
    pub session_id: SessionId,
    /// Run executing the turn.
    pub run_id: RunId,
    /// Zero-based turn index about to start.
    pub turn: u32,
}

observe_point! {
    /// Marker for [`HookPoint::TurnStart`].
    marker: TurnStart,
    point: TurnStart,
    request: TurnStartRequest,
}

/// A provider call is about to be dispatched.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProviderCallStartRequest {
    /// Session making the call.
    pub session_id: SessionId,
    /// Run making the call.
    pub run_id: RunId,
    /// Turn the call belongs to.
    pub turn: u32,
    /// Model the call is requesting.
    pub model: String,
}

observe_point! {
    /// Marker for [`HookPoint::ProviderCallStart`].
    marker: ProviderCallStart,
    point: ProviderCallStart,
    request: ProviderCallStartRequest,
}

/// A provider call returned, on every exit path: accepted, empty, failed or
/// cancelled.
#[derive(Debug, Clone, PartialEq)]
pub struct ProviderCallStopRequest {
    /// Session that made the call.
    pub session_id: SessionId,
    /// Run that made the call.
    pub run_id: RunId,
    /// Turn the call belongs to.
    pub turn: u32,
    /// Model that served the call, when dispatched; else the one requested.
    pub model: String,
    /// How the delivery was disposed of.
    pub disposition: crate::events::model_call::CallDisposition,
    /// Usage reported for the call, when known.
    pub usage: Option<Usage>,
    /// Elapsed delivery time in milliseconds.
    pub duration_ms: u64,
}

observe_point! {
    /// Marker for [`HookPoint::ProviderCallStop`].
    marker: ProviderCallStop,
    point: ProviderCallStop,
    request: ProviderCallStopRequest,
}

/// A tool call is about to run (only [`crate::scheduler::PlannedAction::Run`]
/// calls; a served, denied or blocked call never starts one).
#[derive(Debug, Clone, PartialEq)]
pub struct ToolUseStartRequest {
    /// Session that owns the call.
    pub session_id: SessionId,
    /// Run executing the call.
    pub run_id: RunId,
    /// Provider call id of this tool use.
    pub tool_use_id: String,
    /// Tool about to run.
    pub tool_name: String,
    /// Input the tool runs with.
    pub input: JsonObject,
}

observe_point! {
    /// Marker for [`HookPoint::ToolUseStart`].
    marker: ToolUseStart,
    point: ToolUseStart,
    request: ToolUseStartRequest,
}

/// A tool call finished running.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ToolUseStopRequest {
    /// Session that owns the call.
    pub session_id: SessionId,
    /// Run that executed the call.
    pub run_id: RunId,
    /// Provider call id of this tool use.
    pub tool_use_id: String,
    /// Tool that ran.
    pub tool_name: String,
    /// Whether the result is an error.
    pub is_error: bool,
    /// Tool duration in milliseconds.
    pub duration_ms: u64,
}

observe_point! {
    /// Marker for [`HookPoint::ToolUseStop`].
    marker: ToolUseStop,
    point: ToolUseStop,
    request: ToolUseStopRequest,
}

/// A tool call's result is an error, after the `post_tool_use` chain ran.
#[derive(Debug, Clone, PartialEq)]
pub struct PostToolUseFailureRequest {
    /// Session that owns the call.
    pub session_id: SessionId,
    /// Run that executed the call.
    pub run_id: RunId,
    /// Provider call id of this tool use.
    pub tool_use_id: String,
    /// Tool that failed.
    pub tool_name: String,
    /// Input the tool ran with.
    pub input: JsonObject,
    /// The failure, after `post_tool_use` had its chance to replace it.
    pub error: ToolOutputError,
}

observe_point! {
    /// Marker for [`HookPoint::PostToolUseFailure`].
    marker: PostToolUseFailure,
    point: PostToolUseFailure,
    request: PostToolUseFailureRequest,
}

/// Compaction is about to run, after its due checks passed.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PreCompactRequest {
    /// Session whose context will be compacted.
    pub session_id: SessionId,
    /// Run performing compaction.
    pub run_id: RunId,
    /// Turn compaction runs for.
    pub turn: u32,
    /// Message count before compaction.
    pub messages: u64,
    /// Token estimate before compaction.
    pub tokens: u64,
    /// Compaction strategy about to run.
    pub strategy: String,
}

observe_point! {
    /// Marker for [`HookPoint::PreCompact`].
    marker: PreCompact,
    point: PreCompact,
    request: PreCompactRequest,
}

/// Compaction ran and replaced the view with a summary. Not fired when the
/// summary call failed or gave no text: the turn went on uncompacted.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PostCompactRequest {
    /// Session whose context was compacted.
    pub session_id: SessionId,
    /// Run that performed compaction.
    pub run_id: RunId,
    /// Turn compaction ran for.
    pub turn: u32,
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

observe_point! {
    /// Marker for [`HookPoint::PostCompact`].
    marker: PostCompact,
    point: PostCompact,
    request: PostCompactRequest,
}

/// A subagent's child run is about to start. Fired on the parent's hooks,
/// not the child's; not fired when the child run already exists (a resume,
/// or a continuation).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SubagentStartRequest {
    /// Parent session that spawned the subagent.
    pub session_id: SessionId,
    /// Parent run that spawned the subagent.
    pub run_id: RunId,
    /// Child session identifier.
    pub child_session_id: SessionId,
    /// Child run identifier.
    pub subagent_run_id: RunId,
    /// Subagent definition used by the child.
    pub subagent_type: String,
    /// Human-readable child name.
    pub display_name: String,
    /// Prompt sent to the child.
    pub prompt: String,
}

observe_point! {
    /// Marker for [`HookPoint::SubagentStart`].
    marker: SubagentStart,
    point: SubagentStart,
    request: SubagentStartRequest,
}

/// A subagent's child run reached a terminal result. Fired on the parent's
/// hooks, not the child's.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SubagentStopRequest {
    /// Parent session that spawned the subagent.
    pub session_id: SessionId,
    /// Parent run that spawned the subagent.
    pub run_id: RunId,
    /// Child session identifier.
    pub child_session_id: SessionId,
    /// Child run identifier.
    pub subagent_run_id: RunId,
    /// Subagent definition the child ran.
    pub subagent_type: String,
    /// Human-readable child name.
    pub display_name: String,
    /// How the child's run ended.
    pub subtype: crate::events::kinds::ResultSubtype,
    /// The child's final text, else its last response's text.
    pub text: String,
    /// The child's last error, as `name: message`, when it ended in error.
    pub error: Option<String>,
}

observe_point! {
    /// Marker for [`HookPoint::SubagentStop`].
    marker: SubagentStop,
    point: SubagentStop,
    request: SubagentStopRequest,
}

/// An event is about to be delivered to the caller. Fired only for events
/// that are both streamed and durably logged
/// ([`crate::events::Persistence::Both`]): never for a hook's own
/// [`Event::Hook`] record (it never reaches this point, so firing it cannot
/// recurse), and never for a stream-only delta or tool-progress event.
#[derive(Debug, Clone, PartialEq)]
pub struct EventEmitRequest {
    /// Session the event belongs to.
    pub session_id: SessionId,
    /// Run the event belongs to.
    pub run_id: RunId,
    /// The event about to be delivered.
    pub event: Event,
}

observe_point! {
    /// Marker for [`HookPoint::EventEmit`].
    marker: EventEmit,
    point: EventEmit,
    request: EventEmitRequest,
}
