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
//! blocking answer. Every call is returned as a [`HookRecord`] so the loop
//! can emit it. No answer can skip later hooks and still let a tool run
//! (Linch 3.x #102): [`PreToolUseOutcome::Run`] only comes from a chain that
//! ran every matching hook.
//!
//! Observe-only lifecycle points are the event stream, not hooks, and
//! Python's `AfterProviderCall` is gone (provider retries live in the
//! reliability policy). Script hooks and RPC callbacks implement [`Hook`]
//! later; they are not part of this module.

mod chain;
mod hook;
mod tool;
mod turn;

#[cfg(test)]
mod tests;

use std::collections::BTreeSet;
use std::sync::Arc;

use serde_json::json;

use crate::canonical::{Digest, digest_with_domain};
use crate::json::JsonValue;
use crate::policy::HookPoint;

pub use chain::{ChainResult, HookChain, HookRecord, HookStatus};
pub use hook::{CallbackError, Hook, HookError, Point, ToolMatcher};
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
}

impl Hooks {
    /// Digest of the registered policies, for the run contract.
    ///
    /// Covers the set of `(point, policy_id, policy_version)` triples, sorted
    /// by the point's wire name, then id, then version. So it does not
    /// depend on registration order or on the order of [`HookPoint`]'s
    /// variants, and a policy registered twice at one point counts once. A
    /// resumed run compares it with the digest it started with.
    pub fn policy_digest(&self) -> Digest {
        let set: BTreeSet<(String, Arc<str>, u32)> = [
            self.user_prompt_submit.policies(),
            self.context_build.policies(),
            self.pre_tool_use.policies(),
            self.permission_request.policies(),
            self.post_tool_use.policies(),
            self.stop.policies(),
            self.post_turn.policies(),
        ]
        .into_iter()
        .flatten()
        .map(|(point, id, version)| (point_name(point), id, version))
        .collect();
        let triples = set
            .into_iter()
            .map(|(point, id, version)| json!([point, &*id, version]))
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
