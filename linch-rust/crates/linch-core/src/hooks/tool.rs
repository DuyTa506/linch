//! The tool points: `pre_tool_use`, `permission_request`, `post_tool_use`.

use serde::{Deserialize, Serialize};

use crate::ids::{RunId, SessionId};
use crate::json::JsonObject;
use crate::policy::{HookPoint, PermissionDecision, Principal};
use crate::tool::{ToolOutputError, ToolResult};

use super::chain::{ChainResult, HookChain, Step};
use super::hook::{Point, sealed::Sealed};

/// Marker for [`HookPoint::PreToolUse`].
#[derive(Debug, Clone, Copy)]
pub struct PreToolUse;

/// A validated tool call about to be checked and run.
#[derive(Debug, Clone, PartialEq)]
pub struct PreToolUseRequest {
    /// Session that owns the call.
    pub session_id: SessionId,
    /// Run executing the call.
    pub run_id: RunId,
    /// Provider call id of this tool use.
    pub tool_use_id: String,
    /// Tool being called.
    pub tool_name: String,
    /// Input as it stands: the model's, or the last rewrite.
    pub input: JsonObject,
}

/// A `pre_tool_use` hook's answer.
#[derive(Debug, Clone, PartialEq)]
pub enum PreToolUseResponse {
    /// No change.
    Continue,
    /// Use this input from here on; later hooks see it.
    Rewrite {
        /// The new input. The scheduler validates it again after the chain.
        input: JsonObject,
    },
    /// Do not run the call. Stops the chain.
    Block {
        /// Shown to the model.
        reason: String,
    },
    /// Do not run the call; use this result (a cache hit). Stops the chain.
    Resolve {
        /// The precomputed result.
        result: ToolResult,
    },
}

/// What the `pre_tool_use` chain decided. Each variant carries the final
/// input, so the recorded call matches what was checked.
#[derive(Debug, Clone, PartialEq)]
pub enum PreToolUseOutcome {
    /// Every matching hook ran and none blocked or resolved: run the tool.
    /// No other outcome lets the tool run (Linch 3.x #102).
    Run {
        /// Input to validate again and run.
        input: JsonObject,
    },
    /// A hook blocked the call, or failed closed.
    Blocked {
        /// Final input.
        input: JsonObject,
        /// Shown to the model.
        reason: String,
    },
    /// A hook answered with a precomputed result; do not run the tool.
    Resolved {
        /// Final input.
        input: JsonObject,
        /// The result to use.
        result: ToolResult,
    },
}

impl Sealed for PreToolUse {}

impl Point for PreToolUse {
    const POINT: HookPoint = HookPoint::PreToolUse;
    type Request = PreToolUseRequest;
    type Response = PreToolUseResponse;

    fn tool_name(request: &Self::Request) -> Option<&str> {
        Some(&request.tool_name)
    }

    fn verdict(response: &Self::Response) -> &'static str {
        match response {
            PreToolUseResponse::Continue => "continue",
            PreToolUseResponse::Rewrite { .. } => "rewrite",
            PreToolUseResponse::Block { .. } => "block",
            PreToolUseResponse::Resolve { .. } => "resolve",
        }
    }
}

impl HookChain<PreToolUse> {
    /// Run the matching hooks in order. A rewrite feeds the next hook; a
    /// block, a resolve, or a closed failure stops the chain, and only a
    /// chain that ran every matching hook ends in [`PreToolUseOutcome::Run`].
    pub async fn run(&self, mut request: PreToolUseRequest) -> ChainResult<PreToolUseOutcome> {
        let mut records = Vec::new();
        for entry in self.entries().iter() {
            let outcome = match entry.invoke(&request, &mut records).await {
                Step::Skip | Step::Answer(PreToolUseResponse::Continue) => continue,
                Step::Answer(PreToolUseResponse::Rewrite { input }) => {
                    request.input = input;
                    continue;
                }
                Step::Answer(PreToolUseResponse::Block { reason }) | Step::Closed(reason) => {
                    PreToolUseOutcome::Blocked {
                        input: request.input,
                        reason,
                    }
                }
                Step::Answer(PreToolUseResponse::Resolve { result }) => {
                    PreToolUseOutcome::Resolved {
                        input: request.input,
                        result,
                    }
                }
            };
            return ChainResult { outcome, records };
        }
        ChainResult {
            outcome: PreToolUseOutcome::Run {
                input: request.input,
            },
            records,
        }
    }
}

/// Marker for [`HookPoint::PermissionRequest`].
#[derive(Debug, Clone, Copy)]
pub struct PermissionRequest;

/// A tool call the permission engine answered with
/// [`PermissionDecision::Ask`].
#[derive(Debug, Clone, PartialEq)]
pub struct ApprovalRequest {
    /// Session that owns the call.
    pub session_id: SessionId,
    /// Run executing the call.
    pub run_id: RunId,
    /// Provider call id of this tool use.
    pub tool_use_id: String,
    /// Tool being called.
    pub tool_name: String,
    /// Input the call would run with.
    pub input: JsonObject,
    /// Why approval is needed, from the permission engine.
    pub reason: Option<String>,
    /// Caller identity, when the host supplied one.
    pub principal: Option<Principal>,
}

/// A `permission_request` hook's answer, and the host's answer to a
/// pending call in [`crate::Agent::approve`].
///
/// On the wire it is tagged by `decision`: `{"decision": "allow"}` or
/// `{"decision": "deny", "reason": "..."}`. Decoding is strict: an unknown
/// field is refused on `allow` too, which serde does not do for a unit
/// variant, so both variants travel as struct variants.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(from = "ApprovalWire", into = "ApprovalWire")]
pub enum ApprovalResponse {
    /// Approve the call. Later hooks still run and may deny it.
    Allow,
    /// Deny the call. Stops the chain.
    Deny {
        /// Shown to the model.
        reason: String,
    },
}

/// The wire form of [`ApprovalResponse`].
#[derive(Serialize, Deserialize)]
#[serde(tag = "decision", rename_all = "snake_case", deny_unknown_fields)]
enum ApprovalWire {
    Allow {},
    Deny { reason: String },
}

impl From<ApprovalWire> for ApprovalResponse {
    fn from(wire: ApprovalWire) -> Self {
        match wire {
            ApprovalWire::Allow {} => Self::Allow,
            ApprovalWire::Deny { reason } => Self::Deny { reason },
        }
    }
}

impl From<ApprovalResponse> for ApprovalWire {
    fn from(response: ApprovalResponse) -> Self {
        match response {
            ApprovalResponse::Allow => Self::Allow {},
            ApprovalResponse::Deny { reason } => Self::Deny { reason },
        }
    }
}

impl Sealed for PermissionRequest {}

impl Point for PermissionRequest {
    const POINT: HookPoint = HookPoint::PermissionRequest;
    type Request = ApprovalRequest;
    type Response = ApprovalResponse;

    fn tool_name(request: &Self::Request) -> Option<&str> {
        Some(&request.tool_name)
    }

    fn verdict(response: &Self::Response) -> &'static str {
        match response {
            ApprovalResponse::Allow => "allow",
            ApprovalResponse::Deny { .. } => "deny",
        }
    }
}

impl HookChain<PermissionRequest> {
    /// Run the matching hooks in order; the strictest answer wins.
    ///
    /// A deny or a closed failure stops the chain with
    /// [`PermissionDecision::Deny`]. An allow does not stop it, so a later
    /// hook can still deny. When at least one hook allowed and none denied
    /// the result is [`PermissionDecision::Allow`]; when no hook answered it
    /// stays [`PermissionDecision::Ask`] and the run waits for the host.
    pub async fn run(&self, request: &ApprovalRequest) -> ChainResult<PermissionDecision> {
        let mut records = Vec::new();
        let mut allowed = false;
        for entry in self.entries().iter() {
            match entry.invoke(request, &mut records).await {
                Step::Skip => {}
                Step::Answer(ApprovalResponse::Allow) => allowed = true,
                Step::Answer(ApprovalResponse::Deny { reason }) | Step::Closed(reason) => {
                    return ChainResult {
                        outcome: PermissionDecision::Deny { reason },
                        records,
                    };
                }
            }
        }
        let outcome = if allowed {
            PermissionDecision::Allow {}
        } else {
            PermissionDecision::Ask {
                reason: request.reason.clone(),
            }
        };
        ChainResult { outcome, records }
    }
}

/// Marker for [`HookPoint::PostToolUse`].
#[derive(Debug, Clone, Copy)]
pub struct PostToolUse;

/// A tool call that finished.
#[derive(Debug, Clone, PartialEq)]
pub struct PostToolUseRequest {
    /// Session that owns the call.
    pub session_id: SessionId,
    /// Run executing the call.
    pub run_id: RunId,
    /// Provider call id of this tool use.
    pub tool_use_id: String,
    /// Tool that ran.
    pub tool_name: String,
    /// Input the tool ran with.
    pub input: JsonObject,
    /// Result as it stands: the tool's, or the last replacement.
    pub result: ToolResult,
}

/// A `post_tool_use` hook's answer.
#[derive(Debug, Clone, PartialEq)]
pub enum PostToolUseResponse {
    /// No change.
    Continue,
    /// Use this result from here on; later hooks see it.
    Replace {
        /// The new result.
        result: ToolResult,
    },
}

impl Sealed for PostToolUse {}

impl Point for PostToolUse {
    const POINT: HookPoint = HookPoint::PostToolUse;
    type Request = PostToolUseRequest;
    type Response = PostToolUseResponse;

    fn tool_name(request: &Self::Request) -> Option<&str> {
        Some(&request.tool_name)
    }

    fn verdict(response: &Self::Response) -> &'static str {
        match response {
            PostToolUseResponse::Continue => "continue",
            PostToolUseResponse::Replace { .. } => "replace",
        }
    }
}

impl HookChain<PostToolUse> {
    /// Run the matching hooks in order and return the final result. A
    /// replacement feeds the next hook. A closed failure withholds the
    /// result: it becomes an error and the chain stops, so a failed
    /// redaction hook cannot leak what it was meant to remove.
    pub async fn run(&self, mut request: PostToolUseRequest) -> ChainResult<ToolResult> {
        let mut records = Vec::new();
        for entry in self.entries().iter() {
            match entry.invoke(&request, &mut records).await {
                Step::Skip | Step::Answer(PostToolUseResponse::Continue) => {}
                Step::Answer(PostToolUseResponse::Replace { result }) => request.result = result,
                Step::Closed(reason) => {
                    let message = format!("{reason}; the tool result was withheld");
                    return ChainResult {
                        outcome: Err(ToolOutputError::new(message)),
                        records,
                    };
                }
            }
        }
        ChainResult {
            outcome: request.result,
            records,
        }
    }
}
