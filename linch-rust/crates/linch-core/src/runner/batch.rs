//! The tool half of a turn: plan, suspend or run, collect the results.

use std::collections::{HashMap, HashSet};

use super::cancel::Unpaired;
use super::sink::JournalSink;
use super::turn::{Batch, Runner, Step};
use crate::agent::RunError;
use crate::ask_user::{self, CANNOT_WAIT};
use crate::events::kinds::{
    InputRequestEvent, InputRequestKind, PermissionRequestEvent, PermissionRequestItem,
    SuspendStatus, SuspendedEvent, ToolCallEndEvent, UserEvent, UserEventSubtype,
};
use crate::events::{Envelope, Event};
use crate::hooks::ApprovalResponse;
use crate::journal::{INTERRUPTED_TOOL_CODE, ToolRecovery};
use crate::json::JsonObject;
use crate::message::{ContentBlock, Message, Role, ToolUseBlock};
use crate::policy::PermissionDecision;
use crate::run::{Phase, ToolDecisionRecord};
use crate::scheduler::{self, BatchPlan, ExecuteOptions, PlannedAction, RecordedApproval};
use crate::subagents::{CONTINUE_TOOL, SUBAGENT_TOOL};

/// Why a subagent's call that needs approval is denied.
const CANNOT_ASK: &str = "it needs approval, which a subagent cannot ask for; allow it with a \
     permission rule or a permission_request hook";

impl Runner {
    /// Plan the calls still to decide; suspend if one needs approval,
    /// otherwise run them and hold the results.
    pub(super) async fn batch(&mut self, batch: Batch) -> Result<Option<Step>, RunError> {
        let Batch {
            calls,
            mut decisions,
            approvals,
            after_seq,
        } = batch;
        let calls = self.rerun_subagents(calls);
        let blocks: Vec<ToolUseBlock> = calls
            .iter()
            .filter_map(|call| match call {
                ToolRecovery::Replan(block) => Some(block.clone()),
                ToolRecovery::Done(_) | ToolRecovery::Interrupted { .. } => None,
            })
            .collect();
        // A placeholder is the end of its tool's durable start; it rides
        // the next barrier.
        for call in &calls {
            if let ToolRecovery::Interrupted { tool_name, result } = call {
                self.journal.stage(Event::ToolCallEnd(ToolCallEndEvent {
                    tool_name: tool_name.clone(),
                    result: result.clone(),
                    duration_ms: 0,
                    canonical_output: None,
                    metadata: None,
                    error_code: Some(INTERRUPTED_TOOL_CODE.to_owned()),
                    cache_hit: false,
                }));
            }
        }
        let refusals = self.refusals(&blocks);
        let (agent, run) = (&self.agent, &self.run);
        let held_back = |block: &ToolUseBlock| refusals.get(&block.id).map(|r| r.message.clone());
        let plan = scheduler::plan_gated(
            &blocks,
            &self.snapshot,
            &agent.permissions,
            &agent.hooks,
            run,
            &approvals,
            &held_back,
        );
        // A cancel, even one that came before approve or resume, aborts
        // without asking the hooks (again), each call answered so the
        // session stays paired. The placeholders are staged above.
        let Some(mut plan) = self.cancel.run_until_cancelled(plan).await else {
            let calls = calls.into_iter().map(|call| match call {
                ToolRecovery::Interrupted { result, .. } => ToolRecovery::Done(result),
                call => call,
            });
            self.cancelled(Unpaired::Calls(calls.collect())).await?;
            return Ok(None);
        };
        if self.agent.child {
            plan.refuse_asks(CANNOT_ASK);
            plan.refuse_inputs(CANNOT_WAIT);
        }
        self.refuse_unoffered(&mut plan);
        if !refusals.is_empty() {
            // The calls the policy answered, not those refused first for
            // another reason (an unknown tool).
            let gated: HashSet<String> = (plan.calls().iter())
                .filter(
                    |call| match (&call.action, refusals.get(&call.tool_use_id)) {
                        (PlannedAction::Error { error }, Some(refusal)) => {
                            error.message == refusal.message
                        }
                        _ => false,
                    },
                )
                .map(|call| call.tool_use_id.clone())
                .collect();
            self.stage_refusals(&blocks, &refusals, &gated);
        }
        for call in plan.calls() {
            let records = call.hook_records.clone();
            self.push_records(records, Some(&call.tool_use_id));
        }
        for record in plan_decisions(&plan) {
            upsert(&mut decisions, record);
        }
        // Approvals first: an `AskUser` call waits until no call of its
        // batch needs one, so the user is asked once.
        if plan.is_awaiting_approval() {
            self.suspend(&plan, decisions).await?;
            return Ok(None);
        }
        if plan.is_awaiting_input() {
            self.suspend_for_input(&plan, decisions).await?;
            return Ok(None);
        }
        let after_seq = after_seq.unwrap_or_else(|| self.journal.last_seq());
        self.save(Phase::ToolBatch {
            decisions,
            after_seq,
        })
        .await?;
        let mut fresh = self.execute(plan).await?.into_iter();
        let results = calls.into_iter().map(|call| match call {
            ToolRecovery::Done(block) | ToolRecovery::Interrupted { result: block, .. } => block,
            ToolRecovery::Replan(_) => fresh.next().expect("execute returns one result per call"),
        });
        let mut blocks: Vec<ContentBlock> = results.map(ContentBlock::ToolResult).collect();
        let (notes, peers) = self.notes().await?;
        blocks.extend(notes);
        let notice = self.catalog_loads(&blocks);
        blocks.extend(notice);
        let message = Message::new(Role::User, blocks);
        self.stage_seen(Event::User(UserEvent {
            message: message.clone(),
            subtype: UserEventSubtype::ToolResult,
            steer_id: None,
        }));
        self.hold_peers(peers);
        self.save(Phase::ToolResults {
            message: message.clone(),
        })
        .await?;
        Ok(Some(Step::Results {
            message,
            appended: false,
        }))
    }

    /// Plan again each `Subagent` or `SubagentContinue` call that started
    /// and has no end, instead of standing in a placeholder: the call
    /// resumes its durable child run (see [`crate::subagents`]).
    fn rerun_subagents(&self, calls: Vec<ToolRecovery>) -> Vec<ToolRecovery> {
        let resumable = |call: &ToolRecovery| matches!(call, ToolRecovery::Interrupted { tool_name, .. } if tool_name == SUBAGENT_TOOL || tool_name == CONTINUE_TOOL);
        if self.agent.subagents.is_none() || !calls.iter().any(resumable) {
            return calls;
        }
        // The batch's calls are those of the response the view ends with,
        // in order.
        let Some(response) = self.log.last_visible() else {
            return calls;
        };
        let blocks: Vec<&ToolUseBlock> = response.tool_uses().collect();
        let calls = calls.into_iter().enumerate();
        calls
            .map(|(at, call)| match blocks.get(at) {
                Some(block) if resumable(&call) => ToolRecovery::Replan((*block).clone()),
                _ => call,
            })
            .collect()
    }

    /// Write through `AwaitingApproval` with the pending calls. The
    /// family is parked first, so its children outlive the stream even if
    /// the caller drops it at the `Suspended` envelope; a failed write
    /// takes that back.
    async fn suspend(
        &mut self,
        plan: &BatchPlan,
        decisions: Vec<ToolDecisionRecord>,
    ) -> Result<(), RunError> {
        let waiting = plan
            .calls()
            .iter()
            .filter(|call| matches!(call.action, PlannedAction::AwaitingApproval { .. }));
        let requests: Vec<PermissionRequestItem> = waiting
            .map(|call| PermissionRequestItem {
                tool_use_id: call.tool_use_id.clone(),
                tool_name: call.tool_name.clone(),
                input: call.input.clone(),
                summary: call.summary.clone(),
            })
            .collect();
        let pending: Vec<String> = requests.iter().map(|r| r.tool_use_id.clone()).collect();
        self.journal
            .stage(Event::PermissionRequest(PermissionRequestEvent {
                requests,
            }));
        self.journal.stage(Event::Suspended(SuspendedEvent {
            status: SuspendStatus::AwaitingApproval,
            pending_request_ids: pending.clone(),
        }));
        self.park_and_save(Phase::AwaitingApproval { decisions, pending })
            .await
    }

    /// Write through `AwaitingInput` with one `input_request` per `AskUser`
    /// call waiting for the user, parking the family as [`Self::suspend`]
    /// does.
    async fn suspend_for_input(
        &mut self,
        plan: &BatchPlan,
        decisions: Vec<ToolDecisionRecord>,
    ) -> Result<(), RunError> {
        let waiting = plan
            .calls()
            .iter()
            .filter(|call| matches!(call.action, PlannedAction::AwaitingInput));
        let mut pending = Vec::new();
        for call in waiting {
            // The plan validated the input with the tool.
            let questions = ask_user::questions(&call.input).unwrap_or_default();
            self.journal.stage(Event::InputRequest(InputRequestEvent {
                request_id: call.tool_use_id.clone(),
                kind: InputRequestKind::AskUser,
                prompt: ask_user::prompt(&questions),
                questions,
            }));
            pending.push(call.tool_use_id.clone());
        }
        self.journal.stage(Event::Suspended(SuspendedEvent {
            status: SuspendStatus::AwaitingInput,
            pending_request_ids: pending.clone(),
        }));
        self.park_and_save(Phase::AwaitingInput { decisions, pending })
            .await
    }

    /// Park the family, so its children outlive the stream, and write the
    /// suspend `phase` through; a failed write takes the park back.
    async fn park_and_save(&mut self, phase: Phase) -> Result<(), RunError> {
        let family = self.family.clone();
        if let Some(family) = &family {
            family.park(true);
        }
        let saved = self.save(phase).await;
        if let (Err(_), Some(family)) = (&saved, &family) {
            family.park(false);
        }
        saved
    }

    /// Run a decided plan with the journal as its durability sink.
    async fn execute(&mut self, plan: BatchPlan) -> Result<Vec<crate::ToolResultBlock>, RunError> {
        let spec = &self.agent.spec;
        let options = ExecuteOptions {
            max_concurrency: usize::try_from(spec.max_tool_concurrency).unwrap_or(usize::MAX),
            call_timeout: spec.call_timeout(),
            cancel: self.cancel.clone(),
            redactor: self.agent.result_redactor().cloned(),
            retry: spec.tool_retry.clone(),
            retry_clock: self.retry_clock.clone(),
            cache: self.agent.tool_cache.clone(),
        };
        let run_id = self.run.run_id.clone();
        let hooks = &self.agent.hooks;
        let sink = JournalSink::new(
            &mut self.journal,
            &self.outbox,
            run_id,
            self.run.session_id.clone(),
            hooks,
            &mut self.watch,
        );
        Ok(scheduler::execute(plan, &sink, hooks, &self.run, &options).await?)
    }

    /// Approve: commit the host's answers for pending calls with a
    /// `ToolBatch` checkpoint, so a crash after this point keeps them.
    pub(super) async fn commit_decisions(
        &mut self,
        decisions: Vec<ToolDecisionRecord>,
        after_seq: u64,
    ) -> Result<(), RunError> {
        self.save(Phase::ToolBatch {
            decisions,
            after_seq,
        })
        .await?;
        let ready = self.journal.flush().await?;
        self.event_emit(&ready).await;
        self.outbox.deliver(ready).await;
        Ok(())
    }
}

/// The permission decision of every planned call that reached permission.
/// Calls refused before it (unknown tool, invalid input, blocked) are
/// decided again from scratch on a re-plan, so they record nothing.
fn plan_decisions(plan: &BatchPlan) -> impl Iterator<Item = ToolDecisionRecord> + '_ {
    plan.calls().iter().filter_map(|call| {
        let decision = match &call.action {
            PlannedAction::Run | PlannedAction::Serve { .. } | PlannedAction::AwaitingInput => {
                PermissionDecision::Allow {}
            }
            PlannedAction::Denied { reason } => PermissionDecision::Deny {
                reason: reason.clone(),
            },
            PlannedAction::AwaitingApproval { reason } => PermissionDecision::Ask {
                reason: reason.clone(),
            },
            PlannedAction::Error { .. } | PlannedAction::Blocked { .. } => return None,
        };
        Some(ToolDecisionRecord {
            tool_use_id: call.tool_use_id.clone(),
            decision,
        })
    })
}

/// Replace the record for the same call, or add it.
pub(super) fn upsert(decisions: &mut Vec<ToolDecisionRecord>, record: ToolDecisionRecord) {
    match decisions
        .iter_mut()
        .find(|known| known.tool_use_id == record.tool_use_id)
    {
        Some(known) => *known = record,
        None => decisions.push(record),
    }
}

/// A host answer as a recorded decision.
pub(super) fn answer_decision(answer: &ApprovalResponse) -> PermissionDecision {
    match answer {
        ApprovalResponse::Allow => PermissionDecision::Allow {},
        ApprovalResponse::Deny { reason } => PermissionDecision::Deny {
            reason: reason.clone(),
        },
    }
}

/// The seq of the last `Assistant` event and the events after it: the
/// current tool batch's permission requests, starts and ends.
pub(super) fn turn_events(events: &[Envelope]) -> (u64, &[Envelope]) {
    let last = events
        .iter()
        .rposition(|envelope| matches!(envelope.event, Event::Assistant(_)));
    match last {
        Some(at) => (events[at].seq, &events[at + 1..]),
        None => (0, events),
    }
}

/// Host answers among `decisions`, each with the input the host saw in this
/// turn's permission requests (the latest request per call wins). Decisions
/// the engine made alone have no request and are decided again fresh.
pub(super) fn approvals(
    decisions: &[ToolDecisionRecord],
    turn: &[Envelope],
) -> HashMap<String, RecordedApproval> {
    let mut inputs: HashMap<&str, &JsonObject> = HashMap::new();
    for envelope in turn {
        if let Event::PermissionRequest(request) = &envelope.event {
            for item in &request.requests {
                inputs.insert(&item.tool_use_id, &item.input);
            }
        }
    }
    let answered = decisions.iter().filter_map(|record| {
        let response = match &record.decision {
            PermissionDecision::Allow {} => ApprovalResponse::Allow,
            PermissionDecision::Deny { reason } => ApprovalResponse::Deny {
                reason: reason.clone(),
            },
            PermissionDecision::Ask { .. } => return None,
        };
        let input = (*inputs.get(record.tool_use_id.as_str())?).clone();
        let approval = RecordedApproval { input, response };
        Some((record.tool_use_id.clone(), approval))
    });
    answered.collect()
}
