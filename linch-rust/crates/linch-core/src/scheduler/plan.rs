//! [`plan`]: decide every call of a batch before anything runs.

use std::collections::HashMap;
use std::sync::Arc;

use crate::hooks::{
    ApprovalRequest, ApprovalResponse, HookRecord, Hooks, PreToolUseOutcome, PreToolUseRequest,
};
use crate::json::JsonObject;
use crate::message::ToolUseBlock;
use crate::permissions::{PermissionCall, PermissionEngine};
use crate::policy::PermissionDecision;
use crate::registry::{RegisteredTool, ToolSnapshot};
use crate::tool::{ToolOutputError, ToolResult};

use super::{RecordedApproval, RunIdentity};

/// The decided calls of one assistant response, in call order.
///
/// Only [`plan`] builds one, and its entries are read-only, so a decision
/// cannot be changed between planning and [`super::execute`].
#[derive(Debug)]
pub struct BatchPlan {
    pub(super) calls: Vec<PlannedCall>,
}

impl BatchPlan {
    /// The planned calls, in call order.
    pub fn calls(&self) -> &[PlannedCall] {
        &self.calls
    }

    /// Whether a call still needs approval. The run must then suspend
    /// without executing the plan.
    pub fn is_awaiting_approval(&self) -> bool {
        self.calls
            .iter()
            .any(|call| matches!(call.action, PlannedAction::AwaitingApproval { .. }))
    }
}

/// One tool call and what will happen to it.
#[derive(Debug)]
pub struct PlannedCall {
    /// Provider call id of this tool use.
    pub tool_use_id: String,
    /// Tool name the model asked for.
    pub tool_name: String,
    /// Final input: validated and after `pre_tool_use` rewrites. The model's
    /// input when the call never got that far.
    pub input: JsonObject,
    /// [`Tool::summarize`](crate::Tool::summarize) of the final input, or the
    /// tool name for a call refused before permission (unknown tool, invalid
    /// input, blocked).
    pub summary: String,
    /// What the scheduler decided.
    pub action: PlannedAction,
    /// The `pre_tool_use` and `permission_request` hooks called, in call
    /// order, for the loop to emit.
    pub hook_records: Vec<HookRecord>,
    pub(super) tool: Option<Arc<RegisteredTool>>,
}

/// The decision for one call.
#[derive(Debug, Clone, PartialEq)]
pub enum PlannedAction {
    /// Run the tool on the final input.
    Run,
    /// Serve a `pre_tool_use` result (a cache hit) without running the tool.
    /// Permission allowed the call on its final input.
    Serve {
        /// The result a hook supplied.
        result: ToolResult,
    },
    /// The tool is unknown or its input is invalid.
    Error {
        /// Shown to the model.
        error: ToolOutputError,
    },
    /// A `pre_tool_use` hook blocked the call.
    Blocked {
        /// Shown to the model.
        reason: String,
    },
    /// Permission denied the call.
    Denied {
        /// Shown to the model.
        reason: String,
    },
    /// The call needs a host approval; the run suspends.
    AwaitingApproval {
        /// Why approval is needed, for the approval prompt.
        reason: Option<String>,
    },
}

/// Decide every call in `blocks`, in order, without running any tool.
///
/// Per call: resolve the name in `tools`, validate the input, run the
/// `pre_tool_use` chain of `hooks`, validate the chain's final input again,
/// evaluate `permissions` (running the `permission_request` chain on `Ask`),
/// then apply the entry of `approvals` recorded for the call. An unknown tool
/// or invalid model input skips the hooks.
///
/// `approvals` holds host answers from an earlier suspend, by `tool_use_id`.
/// A recorded allow only lifts an `Ask` on the input the host saw, a
/// recorded deny always denies (whatever the input), and nothing overrides
/// a fresh deny.
///
/// Returns one entry per block, in order. When any entry awaits approval
/// ([`BatchPlan::is_awaiting_approval`]) the run must suspend instead of
/// calling [`super::execute`].
pub async fn plan(
    blocks: &[ToolUseBlock],
    tools: &ToolSnapshot,
    permissions: &PermissionEngine,
    hooks: &Hooks,
    run: &RunIdentity,
    approvals: &HashMap<String, RecordedApproval>,
) -> BatchPlan {
    let planner = Planner {
        permissions,
        hooks,
        run,
        approvals,
    };
    let mut calls = Vec::with_capacity(blocks.len());
    for block in blocks {
        calls.push(planner.call(block, tools.resolve(&block.name)).await);
    }
    BatchPlan { calls }
}

struct Planner<'a> {
    permissions: &'a PermissionEngine,
    hooks: &'a Hooks,
    run: &'a RunIdentity,
    approvals: &'a HashMap<String, RecordedApproval>,
}

impl Planner<'_> {
    async fn call(&self, block: &ToolUseBlock, tool: Option<&Arc<RegisteredTool>>) -> PlannedCall {
        let mut call = PlannedCall {
            tool_use_id: block.id.clone(),
            tool_name: block.name.clone(),
            input: block.input.clone(),
            summary: block.name.clone(),
            action: PlannedAction::Run,
            hook_records: Vec::new(),
            tool: tool.cloned(),
        };
        let Some(tool) = tool else {
            let message = format!("Tool {:?} is not registered", block.name);
            call.action = PlannedAction::Error {
                error: ToolOutputError::new(message),
            };
            return call;
        };
        match tool.validate_input(block.input.clone()) {
            Ok(input) => call.input = input,
            Err(error) => {
                call.action = PlannedAction::Error { error };
                return call;
            }
        }
        if !self.pre_tool_use(&mut call, tool).await {
            return call;
        }
        call.summary = tool.tool().summarize(&call.input);
        // An allow keeps `Run`, or the `Serve` a hook asked for; a deny or an
        // ask discards a served result.
        match self.permission(&mut call, tool).await {
            PermissionDecision::Allow {} => {}
            PermissionDecision::Deny { reason } => call.action = PlannedAction::Denied { reason },
            PermissionDecision::Ask { reason } => {
                call.action = PlannedAction::AwaitingApproval { reason };
            }
        }
        call
    }

    /// Run the `pre_tool_use` chain and validate its final input. Returns
    /// whether the call goes on to permission; when it does not,
    /// `call.action` holds the refusal. A resolved call goes on with a
    /// tentative [`PlannedAction::Serve`].
    async fn pre_tool_use(&self, call: &mut PlannedCall, tool: &RegisteredTool) -> bool {
        if self.hooks.pre_tool_use.is_empty() {
            return true;
        }
        let chain = self
            .hooks
            .pre_tool_use
            .run(PreToolUseRequest {
                session_id: self.run.session_id.clone(),
                run_id: self.run.run_id.clone(),
                tool_use_id: call.tool_use_id.clone(),
                tool_name: call.tool_name.clone(),
                input: call.input.clone(),
            })
            .await;
        call.hook_records = chain.records;
        call.input = match chain.outcome {
            PreToolUseOutcome::Run { input } => input,
            PreToolUseOutcome::Resolved { input, result } => {
                call.action = PlannedAction::Serve { result };
                input
            }
            PreToolUseOutcome::Blocked { input, reason } => {
                call.input = input;
                call.action = PlannedAction::Blocked { reason };
                return false;
            }
        };
        match tool.validate_input(call.input.clone()) {
            Ok(input) => {
                call.input = input;
                true
            }
            Err(mut error) => {
                error.message = format!(
                    "A pre_tool_use hook produced invalid input: {}",
                    error.message
                );
                call.action = PlannedAction::Error { error };
                false
            }
        }
    }

    /// Evaluate permission on the final input, ask the `permission_request`
    /// chain on `Ask`, then apply a recorded approval.
    async fn permission(
        &self,
        call: &mut PlannedCall,
        tool: &RegisteredTool,
    ) -> PermissionDecision {
        let principal = self.run.principal.as_ref();
        let scope = tool.tool().scope();
        let permission_call = PermissionCall::new(&call.tool_name, scope, &call.input);
        let mut decision = self.permissions.evaluate(&permission_call, principal);
        if let PermissionDecision::Ask { reason } = &decision
            && !self.hooks.permission_request.is_empty()
        {
            let chain = self
                .hooks
                .permission_request
                .run(&ApprovalRequest {
                    session_id: self.run.session_id.clone(),
                    run_id: self.run.run_id.clone(),
                    tool_use_id: call.tool_use_id.clone(),
                    tool_name: call.tool_name.clone(),
                    input: call.input.clone(),
                    reason: reason.clone(),
                    principal: principal.cloned(),
                })
                .await;
            call.hook_records.extend(chain.records);
            decision = chain.outcome;
        }
        // A recorded deny stands whatever the input; an allow covers only
        // the input the host saw.
        let recorded = self.approvals.get(&call.tool_use_id).filter(|recorded| {
            matches!(recorded.response, ApprovalResponse::Deny { .. })
                || recorded.input == call.input
        });
        apply_recorded(decision, recorded.map(|recorded| &recorded.response))
    }
}

/// Combine a fresh decision with a recorded host answer: deny wins from
/// either side, and a recorded allow only lifts an `Ask`.
fn apply_recorded(
    decision: PermissionDecision,
    recorded: Option<&ApprovalResponse>,
) -> PermissionDecision {
    match (decision, recorded) {
        (deny @ PermissionDecision::Deny { .. }, _) => deny,
        (_, Some(ApprovalResponse::Deny { reason })) => PermissionDecision::Deny {
            reason: reason.clone(),
        },
        (PermissionDecision::Ask { .. }, Some(ApprovalResponse::Allow)) => {
            PermissionDecision::Allow {}
        }
        (decision, _) => decision,
    }
}
