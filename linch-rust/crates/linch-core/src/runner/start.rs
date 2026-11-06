//! Entering a run: create a new one, or claim and reconcile an existing one.

use std::collections::HashMap;
use std::sync::Arc;
use std::time::Instant;

use super::batch::{answer_decision, approvals, turn_events, upsert};
use super::progress::Progress;
use super::turn::{Batch, Runner, Step};
use super::{Outbox, hooks, inbox};
use crate::agent::{AgentInner, RunError, RunOptions};
use crate::events::kinds::{InitEvent, UserEvent, UserEventSubtype};
use crate::events::{Envelope, Event};
use crate::hooks::ApprovalResponse;
use crate::ids::{RunId, SessionId};
use crate::journal::{Journal, JournalError, Recovery, reconcile};
use crate::run::{Checkpoint, Phase, StopReason, ToolDecisionRecord};
use crate::scheduler::RunIdentity;
use crate::store::{Lease, RunRecord, StoreError};
use crate::usage::Usage;

/// Create the run (the `Started` write-through), append the prompt, and go.
/// `parent` is the parent run of a subagent's child run.
pub(super) async fn new_run(
    agent: Arc<AgentInner>,
    run_id: RunId,
    prompt: String,
    options: RunOptions,
    outbox: Outbox,
    parent: Option<RunId>,
) -> Result<(), RunError> {
    let started = Instant::now();
    let namespace = agent.namespace.clone();
    if options.run_id.is_some() {
        // A caller-chosen id may be taken: refuse it before any write.
        match agent.runs.load(&namespace, &run_id).await {
            Ok(_) => return Err(duplicate(&run_id, &namespace).into()),
            Err(StoreError::NotFound { .. }) => {}
            Err(error) => return Err(error.into()),
        }
    }
    let session_id = options.session_id;
    let prompt = hooks::submit(&agent, &outbox, &session_id, &run_id, prompt).await?;
    let sessions = &agent.sessions;
    let entries = match sessions.load(&namespace, &session_id).await {
        Ok(entries) => entries,
        // When another run created the session first, it is still empty as
        // far as this run knows: the run create admits one of the two.
        Err(StoreError::NotFound { .. }) => match sessions.create(&namespace, &session_id).await {
            Ok(()) | Err(StoreError::Conflict { .. }) => Vec::new(),
            Err(error) => return Err(error.into()),
        },
        Err(error) => return Err(error.into()),
    };
    // Peer messages that waited for the session join the prompt.
    let peers = inbox::drain(&agent, &session_id).await?;
    let mut prompt = prompt;
    prompt.content.extend(inbox::blocks(&peers));
    let lease = Lease {
        namespace,
        run_id: run_id.clone(),
        owner: options.owner,
        epoch: 0,
    };
    let mut journal = Journal::new(Arc::clone(&agent.runs), lease, 0);
    let tools = agent.snapshot().definitions();
    journal.stage(Event::Init(InitEvent {
        session_id: session_id.clone(),
        run_id: run_id.clone(),
        parent_run_id: parent.clone(),
        model: agent.spec.model.clone(),
        tools: tools.into_iter().map(|tool| tool.name).collect(),
        trace_id: None,
    }));
    journal.stage(Event::User(UserEvent {
        message: prompt.clone(),
        subtype: UserEventSubtype::Prompt,
    }));
    let checkpoint = Checkpoint {
        turn: 0,
        usage: Usage::default(),
        usage_complete: true,
        cost_usd: None,
        session_len: entries.len() as u64,
        phase: Phase::Started {
            prompt: prompt.clone(),
        },
    };
    let contract = agent.contract;
    let created = journal
        .create(session_id.clone(), parent, contract, checkpoint)
        .await;
    let ready = match created {
        Ok(ready) => ready,
        // Nothing of this run is logged, so the drained messages go back.
        Err(error) => {
            inbox::redeliver(&agent, peers).await;
            return Err(match error {
                // Another start with the same id created it first, or the
                // session already has a run that is not terminal.
                JournalError::Store(
                    error @ (StoreError::Conflict { .. } | StoreError::SessionBusy { .. }),
                ) => error.into(),
                error => error.into(),
            });
        }
    };
    outbox.deliver(ready).await;
    let progress = Progress::first(started);
    let run = identity(session_id, run_id, &options.principal);
    let runner = Runner::new(
        agent,
        journal,
        outbox,
        run,
        options.cancel,
        entries,
        progress,
    );
    let prompt = Step::Prompt {
        prompt,
        appended: false,
        moved: false,
    };
    runner.drive(prompt).await
}

/// Claim an existing run, check it may continue, reconcile its durable
/// state, and go on from there. `answers` is set for an approve, which is
/// accepted only on a run awaiting approval. With a token that is already
/// cancelled, nothing is claimed or written and the stream ends empty.
pub(super) async fn continue_run(
    agent: Arc<AgentInner>,
    run_id: RunId,
    answers: Option<HashMap<String, ApprovalResponse>>,
    options: RunOptions,
    outbox: Outbox,
) -> Result<(), RunError> {
    let started = Instant::now();
    if options.cancel.is_cancelled() {
        return Ok(());
    }
    let (runs, namespace) = (&agent.runs, agent.namespace.as_str());
    // Check before claiming, so a mistaken call does not fence a live owner;
    // check again after, against the state the claim fixed.
    check(
        &runs.load(namespace, &run_id).await?,
        &agent,
        answers.as_ref(),
    )?;
    let lease = match runs.claim(namespace, &run_id, &options.owner).await {
        Ok(lease) => lease,
        Err(StoreError::InvalidTransition { from, .. }) if from.is_terminal() => {
            return Err(RunError::Terminal {
                run_id,
                status: from,
            });
        }
        Err(error) => return Err(error.into()),
    };
    let record = runs.load(namespace, &run_id).await?;
    check(&record, &agent, answers.as_ref())?;
    let events = runs.events(namespace, &run_id, 0).await?;
    let entries = agent.sessions.load(namespace, &record.session_id).await?;
    let checkpoint = &record.checkpoint;
    let (assistant_seq, turn) = turn_events(&events);
    let mut commit = None;
    let step = match reconcile(checkpoint, &events, &entries)? {
        Recovery::Started { prompt, appended } => Step::Prompt {
            prompt,
            appended,
            // Runs that ended before this one was created appended since.
            moved: !appended && entries.len() as u64 != checkpoint.session_len,
        },
        Recovery::Redispatch { attempt, abandoned } => Step::Dispatch { attempt, abandoned },
        Recovery::Received {
            message,
            stop_reason,
            appended,
        } => Step::Received {
            message,
            stop_reason,
            appended,
        },
        Recovery::ToolBatch { decisions, calls } => {
            let Phase::ToolBatch { after_seq, .. } = checkpoint.phase else {
                unreachable!("a tool batch recovery comes from a tool batch checkpoint");
            };
            Step::Batch(Batch {
                approvals: approvals(&decisions, turn),
                calls,
                decisions,
                after_seq: Some(after_seq),
            })
        }
        Recovery::AwaitingApproval { decisions, pending } => {
            let (decisions, changed) = answered(decisions, &pending, answers.unwrap_or_default());
            // A batch can suspend after some of its calls ran (a re-plan on
            // resume): read every call's state from the events after the
            // response, so nothing runs twice.
            let batch = Checkpoint {
                phase: Phase::ToolBatch {
                    decisions: decisions.clone(),
                    after_seq: assistant_seq,
                },
                ..checkpoint.clone()
            };
            let Recovery::ToolBatch { calls, .. } = reconcile(&batch, &events, &entries)? else {
                unreachable!("a tool batch checkpoint reconciles to a tool batch");
            };
            if changed {
                commit = Some(decisions.clone());
            }
            Step::Batch(Batch {
                approvals: approvals(&decisions, turn),
                calls,
                decisions,
                after_seq: Some(assistant_seq),
            })
        }
        Recovery::ToolResults { message, appended } => Step::Results { message, appended },
        Recovery::Recompact { attempt, abandoned } => Step::Compact { attempt, abandoned },
        Recovery::Compacted {
            summary,
            keep_from,
            appended,
        } => Step::Compacted {
            summary,
            keep_from,
            appended,
        },
    };
    let journal = Journal::new(Arc::clone(&agent.runs), lease, record.last_seq);
    let progress = Progress::of(checkpoint, last_stop(&events), started);
    let run = identity(record.session_id.clone(), run_id, &options.principal);
    let mut runner = Runner::new(
        agent,
        journal,
        outbox,
        run,
        options.cancel,
        entries,
        progress,
    );
    if let Some(decisions) = commit {
        runner.commit_decisions(decisions, assistant_seq).await?;
    }
    runner.drive(step).await
}

/// The error for a new run whose id is already taken.
fn duplicate(run_id: &RunId, namespace: &str) -> StoreError {
    let message = format!("run {run_id} already exists in {namespace}");
    StoreError::Conflict { message }
}

/// Why the run's last durable response stopped; `end_turn` before any.
fn last_stop(events: &[Envelope]) -> StopReason {
    let last = events
        .iter()
        .rev()
        .find_map(|envelope| match &envelope.event {
            Event::Assistant(response) => Some(response.stop_reason),
            _ => None,
        });
    last.unwrap_or(StopReason::EndTurn)
}

/// Refuse a terminal run, a run of another contract, and an approve on a
/// run that is not awaiting approval. A run whose approve committed its
/// answers is in its tool batch and may still have a live owner, so an
/// approve of it (a retry, or a second click) is refused too: the host
/// resumes it.
fn check(
    record: &RunRecord,
    agent: &AgentInner,
    answers: Option<&HashMap<String, ApprovalResponse>>,
) -> Result<(), RunError> {
    if record.status.is_terminal() {
        return Err(RunError::Terminal {
            run_id: record.run_id.clone(),
            status: record.status,
        });
    }
    if record.contract != agent.contract {
        return Err(RunError::ContractMismatch {
            run_id: record.run_id.clone(),
            expected: agent.contract,
            found: record.contract,
        });
    }
    let suspended = matches!(record.checkpoint.phase, Phase::AwaitingApproval { .. });
    if answers.is_some() && !suspended {
        return Err(RunError::NotSuspended {
            run_id: record.run_id.clone(),
        });
    }
    Ok(())
}

/// Record the host's answers for the pending calls; also says whether any
/// answer applied.
fn answered(
    mut decisions: Vec<ToolDecisionRecord>,
    pending: &[String],
    answers: HashMap<String, ApprovalResponse>,
) -> (Vec<ToolDecisionRecord>, bool) {
    let mut changed = false;
    for id in pending {
        if let Some(answer) = answers.get(id) {
            let decision = answer_decision(answer);
            let tool_use_id = id.clone();
            upsert(
                &mut decisions,
                ToolDecisionRecord {
                    tool_use_id,
                    decision,
                },
            );
            changed = true;
        }
    }
    (decisions, changed)
}

fn identity(
    session_id: SessionId,
    run_id: RunId,
    principal: &Option<crate::policy::Principal>,
) -> RunIdentity {
    RunIdentity {
        session_id,
        run_id,
        principal: principal.clone(),
    }
}
