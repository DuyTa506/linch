//! The tool half of a turn: plan, suspend or run, collect the results.

use std::collections::HashMap;

use super::sink::JournalSink;
use super::turn::{Batch, Runner, Step};
use crate::agent::RunError;
use crate::events::kinds::{
    PermissionRequestEvent, PermissionRequestItem, SuspendStatus, SuspendedEvent, ToolCallEndEvent,
    UserEvent, UserEventSubtype,
};
use crate::events::{Envelope, Event};
use crate::hooks::ApprovalResponse;
use crate::journal::ToolRecovery;
use crate::json::JsonObject;
use crate::message::{ContentBlock, Message, Role, ToolUseBlock};
use crate::policy::PermissionDecision;
use crate::run::{Phase, ToolDecisionRecord};
use crate::scheduler::{self, BatchPlan, ExecuteOptions, PlannedAction, RecordedApproval};

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
                }));
            }
        }
        let (agent, run) = (&self.agent, &self.run);
        let plan = scheduler::plan(
            &blocks,
            &self.snapshot,
            &agent.permissions,
            &agent.hooks,
            run,
            &approvals,
        );
        // A cancel, even one that came before approve or resume, aborts
        // without asking the hooks (again).
        let Some(plan) = self.cancel.run_until_cancelled(plan).await else {
            self.abort().await?;
            return Ok(None);
        };
        for call in plan.calls() {
            let records = call.hook_records.clone();
            self.push_records(records, Some(&call.tool_use_id));
        }
        for record in plan_decisions(&plan) {
            upsert(&mut decisions, record);
        }
        if plan.is_awaiting_approval() {
            self.suspend(&plan, decisions).await?;
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
        let message = Message::new(Role::User, results.map(ContentBlock::ToolResult).collect());
        self.journal.stage(Event::User(UserEvent {
            message: message.clone(),
            subtype: UserEventSubtype::ToolResult,
        }));
        self.save(Phase::ToolResults {
            message: message.clone(),
        })
        .await?;
        Ok(Some(Step::Results {
            message,
            appended: false,
        }))
    }

    /// Write through `AwaitingApproval` with the pending calls.
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
        self.save(Phase::AwaitingApproval { decisions, pending })
            .await
    }

    /// Run a decided plan with the journal as its durability sink.
    async fn execute(&mut self, plan: BatchPlan) -> Result<Vec<crate::ToolResultBlock>, RunError> {
        let spec = &self.agent.spec;
        let options = ExecuteOptions {
            max_concurrency: usize::try_from(spec.max_tool_concurrency).unwrap_or(usize::MAX),
            call_timeout: spec.call_timeout(),
            cancel: self.cancel.clone(),
        };
        let run_id = self.run.run_id.clone();
        let sink = JournalSink::new(&mut self.journal, &self.outbox, run_id);
        let hooks = &self.agent.hooks;
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
            PlannedAction::Run | PlannedAction::Serve { .. } => PermissionDecision::Allow {},
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
