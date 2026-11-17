//! Intercept callbacks at hook points (tier 2 and up).
//!
//! Each [`HookPoint`] has a marker type ([`PreToolUse`], [`Stop`], ...) that
//! fixes its request and response through [`Point`], so a hook can only
//! answer what its point allows. A [`Hook`] implements one point; it is
//! registered on that point's [`HookChain`] in [`Hooks`], which returns a
//! [`Registration`](crate::Registration).
//!
//! A chain runs the matching hooks in registration order. A rewrite feeds
//! the next hook; a blocking answer stops the chain. A hook that errors or
//! times out is handled by its [`FailMode`](crate::FailMode): open skips the
//! hook and the chain goes on, closed turns the failure into the point's
//! blocking answer (at `stop` it fails the run, see [`StopOutcome`]). Every
//! call is returned as a [`HookRecord`] so the loop can emit it. No answer
//! can skip later hooks and still let a tool run (Linch 3.x #102):
//! [`PreToolUseOutcome::Run`] only comes from a chain that ran every
//! matching hook.
//!
//! Thirteen further points observe only (`observe`): `agent_start`,
//! `agent_stop`, `turn_start`, `provider_call_start`, `provider_call_stop`,
//! `tool_use_start`, `tool_use_stop`, `post_tool_use_failure`, `pre_compact`,
//! `post_compact`, `subagent_start`, `subagent_stop`, `event_emit`. A hook
//! there answers `()` and cannot fail closed. `event_emit` fires once per
//! event about to be delivered to the caller, but only for events that are
//! both streamed and durably logged: never for a hook's own record, and
//! never for a stream-only delta or tool-progress event (see `observe`).
//! [`BeforeProviderCall`] (`call`) lets a hook replace the provider
//! request about to be sent, block the call, or skip it and take another
//! turn with feedback (Python's `force_continue` there); [`AfterProviderCall`]
//! (`assembly`) lets a hook replace the assembled response a provider call
//! just produced, block it before it is staged and appended to the session,
//! or accept it and take another turn instead of acting on it (Python's
//! `retry`/`force_continue` there). Unlike the observe-only points, both
//! points' blocking verdicts can fail closed. `before_final_answer` has no
//! Rust hook point of its own: [`Stop`] (`turn`) can rewrite the run's
//! final-answer text, block the stop outright, or retry the answer once
//! per run call, folding that point in; its `Continue` is Python's stop
//! `force_continue`.
//! Script hooks and RPC callbacks implement [`Hook`] later; they are not
//! part of this module.

mod assembly;
mod call;
mod chain;
mod execute;
mod hook;
mod observe;
mod tool;
mod turn;

#[cfg(test)]
mod tests;

use serde_json::json;

use crate::canonical::{Digest, digest_with_domain};
use crate::json::JsonValue;
use crate::policy::HookPoint;

pub use assembly::{
    AfterProviderCall, AfterProviderCallOutcome, AfterProviderCallRequest,
    AfterProviderCallResponse,
};
pub use call::{
    BeforeProviderCall, BeforeProviderCallOutcome, BeforeProviderCallRequest,
    BeforeProviderCallResponse,
};
pub use chain::{ChainResult, HookChain, HookRecord, HookStatus};
pub use execute::{Next, ToolCall, ToolMiddleware, ToolPipeline};
pub use hook::{CallbackError, Hook, HookError, Point, ToolMatcher};
pub use observe::{
    AgentStart, AgentStartRequest, AgentStop, AgentStopRequest, EventEmit, EventEmitRequest,
    PostCompact, PostCompactRequest, PostToolUseFailure, PostToolUseFailureRequest, PreCompact,
    PreCompactRequest, ProviderCallStart, ProviderCallStartRequest, ProviderCallStop,
    ProviderCallStopRequest, SubagentStart, SubagentStartRequest, SubagentStop,
    SubagentStopRequest, ToolUseStart, ToolUseStartRequest, ToolUseStop, ToolUseStopRequest,
    TurnStart, TurnStartRequest,
};
pub use tool::{
    ApprovalRequest, ApprovalResponse, PermissionRequest, PostToolUse, PostToolUseRequest,
    PostToolUseResponse, PreToolUse, PreToolUseOutcome, PreToolUseRequest, PreToolUseResponse,
};
pub use turn::{
    ContextBuild, ContextBuildRequest, ContextBuildResponse, PostTurn, PostTurnRequest, Stop,
    StopOutcome, StopRequest, StopResponse, UserPromptOutcome, UserPromptRequest,
    UserPromptResponse, UserPromptSubmit,
};

/// Domain separator for [`Hooks::policy_digest`].
const POLICY_DOMAIN: &str = "linch.hooks.policy";

/// The hook chains of one agent, one per [`HookPoint`]. Shared across runs.
#[derive(Default)]
pub struct Hooks {
    /// Hooks at [`HookPoint::UserPromptSubmit`].
    pub user_prompt_submit: HookChain<UserPromptSubmit>,
    /// Hooks at [`HookPoint::ContextBuild`].
    pub context_build: HookChain<ContextBuild>,
    /// Hooks at [`HookPoint::PreToolUse`].
    pub pre_tool_use: HookChain<PreToolUse>,
    /// Hooks at [`HookPoint::PermissionRequest`].
    pub permission_request: HookChain<PermissionRequest>,
    /// Hooks at [`HookPoint::PostToolUse`].
    pub post_tool_use: HookChain<PostToolUse>,
    /// Hooks at [`HookPoint::Stop`].
    pub stop: HookChain<Stop>,
    /// Hooks at [`HookPoint::PostTurn`].
    pub post_turn: HookChain<PostTurn>,
    /// Hooks at [`HookPoint::AgentStart`].
    pub agent_start: HookChain<AgentStart>,
    /// Hooks at [`HookPoint::AgentStop`].
    pub agent_stop: HookChain<AgentStop>,
    /// Hooks at [`HookPoint::TurnStart`].
    pub turn_start: HookChain<TurnStart>,
    /// Hooks at [`HookPoint::BeforeProviderCall`].
    pub before_provider_call: HookChain<BeforeProviderCall>,
    /// Hooks at [`HookPoint::ProviderCallStart`].
    pub provider_call_start: HookChain<ProviderCallStart>,
    /// Hooks at [`HookPoint::ProviderCallStop`].
    pub provider_call_stop: HookChain<ProviderCallStop>,
    /// Hooks at [`HookPoint::AfterProviderCall`].
    pub after_provider_call: HookChain<AfterProviderCall>,
    /// Hooks at [`HookPoint::ToolUseStart`].
    pub tool_use_start: HookChain<ToolUseStart>,
    /// Hooks at [`HookPoint::ToolUseStop`].
    pub tool_use_stop: HookChain<ToolUseStop>,
    /// Hooks at [`HookPoint::PostToolUseFailure`].
    pub post_tool_use_failure: HookChain<PostToolUseFailure>,
    /// Hooks at [`HookPoint::PreCompact`].
    pub pre_compact: HookChain<PreCompact>,
    /// Hooks at [`HookPoint::PostCompact`].
    pub post_compact: HookChain<PostCompact>,
    /// Hooks at [`HookPoint::SubagentStart`].
    pub subagent_start: HookChain<SubagentStart>,
    /// Hooks at [`HookPoint::SubagentStop`].
    pub subagent_stop: HookChain<SubagentStop>,
    /// Hooks at [`HookPoint::EventEmit`].
    pub event_emit: HookChain<EventEmit>,
    /// The `tool_execute` waterfall: middleware around the tool body, at
    /// [`HookPoint::ToolExecute`].
    pub tool_execute: ToolPipeline,
}

impl Hooks {
    /// Digest of the registered policies, for the run contract.
    ///
    /// Covers every `(point, policy_id, policy_version)` triple, point by
    /// point in a fixed order (`user_prompt_submit`, `context_build`,
    /// `pre_tool_use`, `permission_request`, `post_tool_use`, `stop`,
    /// `post_turn`, `agent_start`, `agent_stop`, `turn_start`,
    /// `before_provider_call`, `provider_call_start`, `provider_call_stop`,
    /// `after_provider_call`, `tool_use_start`, `tool_use_stop`,
    /// `post_tool_use_failure`, `pre_compact`, `post_compact`,
    /// `subagent_start`, `subagent_stop`, `event_emit`, `tool_execute`) and in chain order
    /// within a point, duplicates kept. The order of a point's hooks decides
    /// what its chain does (a rewrite feeds the next hook, a block stops
    /// it), so reordering them, or registering one twice, changes the
    /// digest; the order of registrations across points does not. A resumed
    /// run compares it with the digest it started with.
    pub fn policy_digest(&self) -> Digest {
        let triples = [
            self.user_prompt_submit.policies(),
            self.context_build.policies(),
            self.pre_tool_use.policies(),
            self.permission_request.policies(),
            self.post_tool_use.policies(),
            self.stop.policies(),
            self.post_turn.policies(),
            self.agent_start.policies(),
            self.agent_stop.policies(),
            self.turn_start.policies(),
            self.before_provider_call.policies(),
            self.provider_call_start.policies(),
            self.provider_call_stop.policies(),
            self.after_provider_call.policies(),
            self.tool_use_start.policies(),
            self.tool_use_stop.policies(),
            self.post_tool_use_failure.policies(),
            self.pre_compact.policies(),
            self.post_compact.policies(),
            self.subagent_start.policies(),
            self.subagent_stop.policies(),
            self.event_emit.policies(),
            self.tool_execute.policies(),
        ]
        .into_iter()
        .flatten()
        .map(|(point, id, version)| json!([point_name(point), &*id, version]))
        .collect();
        digest_with_domain(POLICY_DOMAIN, &JsonValue::Array(triples))
            .expect("an array of flat triples is far below the depth limit")
    }
}

/// The point's wire name (`pre_tool_use`, ...).
fn point_name(point: HookPoint) -> String {
    match serde_json::to_value(point) {
        Ok(JsonValue::String(name)) => name,
        other => unreachable!("HookPoint serializes as a string, got {other:?}"),
    }
}
