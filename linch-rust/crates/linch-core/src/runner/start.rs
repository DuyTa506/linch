//! Entering a run: create a new one, or claim and reconcile an existing one.

use std::collections::{HashMap, HashSet};
use std::sync::Arc;
use std::time::Instant;

use tokio_util::sync::CancellationToken;

use super::batch::{answer_decision, approvals, turn_events, upsert};
use super::lease::Held;
use super::progress::Progress;
use super::turn::{Batch, Runner, Step};
use super::{Driving, Family, Shared, cancel, hooks, inbox};
use crate::agent::{AgentInner, RunError, RunOptions, tool_choice_problem};
use crate::ask_user::{self, AskUserAnswer};
use crate::budget::{Meter, Spent};
use crate::catalog::{self, Catalog};
use crate::events::kinds::{
    InitEvent, SkillsLoadedEvent, ToolCallEndEvent, ToolCallStartEvent, UserEvent, UserEventSubtype,
};
use crate::events::{Envelope, Event};
use crate::hooks::ApprovalResponse;
use crate::ids::{RunId, SessionId};
use crate::journal::{Journal, JournalError, Recovery, ToolRecovery, reconcile};
use crate::loop_guard::Watch;
use crate::message::ContentBlock;
use crate::provider::{OutputSchema, ToolChoice};
use crate::reliability::Standing;
use crate::run::{Checkpoint, Phase, StopReason, ToolDecisionRecord};
use crate::scheduler::RunIdentity;
use crate::skills::SKILL_TOOL;
use crate::store::{Lease, RunRecord, StoreError};
use crate::structured::{self, FinalTool, OutputContract};
use crate::subagents::{CONTINUE_TOOL, SUBAGENT_TOOL};
use crate::tool_search::Loads;
use crate::truncation::Truncated;
use crate::usage::Usage;

/// Create the run (the `Started` write-through), append the prompt, and go.
/// `parent` is the parent run of a subagent's child run, `inherited` the
/// budget of its tree; a root run with a budget starts its own.
pub(super) async fn new_run(
    agent: Arc<AgentInner>,
    run_id: RunId,
    prompt: String,
    options: RunOptions,
    shared: Shared,
    parent: Option<RunId>,
    inherited: Option<Meter>,
) -> Result<(), RunError> {
    let Shared { outbox, driving } = shared;
    let started = Instant::now();
    let namespace = agent.namespace.clone();
    // The run's own schema, its final tool and tool choice are checked
    // before anything is written.
    let output = match options.output_schema.clone() {
        Some(schema) => Some(compile(&run_id, schema)?),
        None => None,
    };
    let final_tool = run_final_tool(&agent, &run_id, output.as_deref())?;
    if let Some(choice) = &options.tool_choice {
        let tools = agent.snapshot();
        let offered = |name: &str| {
            tools.resolve(name).is_some() || final_tool.as_ref().is_some_and(|t| t.name == name)
        };
        let thinking = agent.spec.thinking.as_ref();
        let toolless = tools.definitions().is_empty() && final_tool.is_none();
        let provider = &*agent.provider;
        if let Some(reason) = tool_choice_problem(provider, choice, thinking, offered, toolless) {
            return Err(RunError::InvalidToolChoice { run_id, reason });
        }
    }
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
    hooks::agent_start(&agent, &outbox, &session_id, &run_id, &prompt).await;
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
    // Peer messages that waited for the session join the prompt, then the
    // notice of a catalog change since the session's last run.
    let peers = inbox::drain(&agent, &session_id).await?;
    let mut prompt = prompt;
    prompt.content.extend(inbox::blocks(&agent, &peers));
    let seen = Catalog::of_entries(&entries).map(|known| {
        let version = catalog::version(&agent);
        let change = known.change(&catalog::live(&agent, Some(&known), &Loads::default()));
        prompt
            .content
            .extend(change.map(ContentBlock::CatalogChange));
        version
    });
    let lease = Lease {
        namespace,
        run_id: run_id.clone(),
        owner: options.owner,
        epoch: 0,
    };
    let mut journal = Journal::new(Arc::clone(&agent.runs), lease, 0);
    let tools = agent.snapshot().definitions();
    let offers_skill = tools.iter().any(|tool| tool.name == SKILL_TOOL);
    journal.stage(Event::Init(InitEvent {
        session_id: session_id.clone(),
        run_id: run_id.clone(),
        parent_run_id: parent.clone(),
        model: agent.spec.model.clone(),
        provider: agent.provider.id().to_owned(),
        tools: tools.into_iter().map(|tool| tool.name).collect(),
        trace_id: options.trace_id.clone(),
        resumed: false,
        turn: 0,
        output_schema: options.output_schema.clone(),
        tool_choice: options.tool_choice.clone(),
        final_tool: final_tool.as_ref().map(|tool| tool.name.clone()),
    }));
    // A session's first run lists the agent's skills, as
    // Python does once per session, when the run offers `Skill` (a child
    // may not); it rides the create write.
    if let (true, true, Some(skills)) = (entries.is_empty(), offers_skill, &agent.skills) {
        let skills = skills.current().descriptors();
        if !skills.is_empty() {
            journal.stage(Event::SkillsLoaded(SkillsLoadedEvent { skills }));
        }
    }
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
        .create(
            session_id.clone(),
            parent,
            options.principal.clone(),
            contract,
            checkpoint,
        )
        .await;
    let ready = match created {
        Ok(ready) => ready,
        // Nothing of this run is logged, so the drained messages go back.
        Err(error) => {
            inbox::release(&agent, &peers).await;
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
    let mut held = Held::new(&agent, journal.lease());
    held.watch(&journal);
    // The create logged the prompt that carries them.
    inbox::ack(&agent, &peers).await;
    let family = drive_family(&agent, &driving, &run_id, &session_id);
    let budget = inherited.or_else(|| {
        let limits = agent.spec.budget.clone()?;
        Some(Meter::new(limits, Spent::default()))
    });
    share(&family, &budget);
    let seq = journal.durable_seq();
    hooks::event_emit(&agent.hooks, &outbox, &session_id, &run_id, seq, &ready).await;
    outbox.deliver(ready).await;
    let progress = Progress::first(started);
    let run = identity(session_id, run_id, &options.principal, options.trace_id);
    let mut runner = Runner::new(
        agent,
        journal,
        outbox,
        run,
        options.cancel,
        entries,
        progress,
    );
    runner.family = family;
    runner.budget = budget;
    runner.seen = seen;
    if output.is_some() {
        runner.output = output;
    }
    runner.final_tool = final_tool;
    if options.tool_choice.is_some() {
        runner.tool_choice = options.tool_choice;
    }
    let prompt = Step::Prompt {
        prompt,
        appended: false,
        moved: false,
    };
    held.drive(runner.drive(prompt)).await
}

/// What an approve or an answer brings to a suspended run.
pub(crate) enum Reply {
    /// The host's approval answer per pending `tool_use_id`.
    Approvals(HashMap<String, ApprovalResponse>),
    /// The user's answer per pending input `request_id`.
    Answers(HashMap<String, AskUserAnswer>),
    /// End the run `aborted` (see [`crate::Agent::cancel`]).
    Cancel,
}

/// Claim an existing run, check it may continue, reconcile its durable
/// state, and go on from there. `reply` is set for an approve, accepted
/// only on a run awaiting approval, or an answer, accepted only on a run
/// awaiting input and only when it fits the questions. With a token that is
/// already cancelled, nothing is claimed or written and the stream ends
/// empty. `inherited` is the budget of a child run's tree. A child run is
/// `seize`d: its parent's driver takes it over even from a live holder,
/// which a refusal would otherwise report to the parent as a failed child.
pub(super) async fn continue_run(
    agent: Arc<AgentInner>,
    run_id: RunId,
    reply: Option<Reply>,
    options: RunOptions,
    shared: Shared,
    inherited: Option<Meter>,
    seize: bool,
) -> Result<(), RunError> {
    let Shared { outbox, driving } = shared;
    let started = Instant::now();
    if options.cancel.is_cancelled() {
        return Ok(());
    }
    let cancelling = matches!(reply, Some(Reply::Cancel));
    let (runs, namespace) = (&agent.runs, agent.namespace.as_str());
    // Check before claiming, so a mistaken call does not fence a live owner
    // (and a wrong answer leaves the run suspended); check again after,
    // against the state the claim fixed.
    let record = runs.load(namespace, &run_id).await?;
    check(&record, &agent, reply.as_ref())?;
    if let (Some(Reply::Answers(answers)), Phase::AwaitingInput { pending, .. }) =
        (&reply, &record.checkpoint.phase)
    {
        let events = runs.events(namespace, &run_id, 0).await?;
        check_answers(&run_id, &events, pending, answers)?;
    }
    let claimed = match seize {
        true => runs.seize(namespace, &run_id, &options.owner).await,
        false => runs.claim(namespace, &run_id, &options.owner).await,
    };
    let lease = match claimed {
        Ok(lease) => lease,
        Err(StoreError::InvalidTransition { from, .. }) if from.is_terminal() => {
            return Err(RunError::Terminal {
                run_id,
                status: from,
            });
        }
        Err(error) => return Err(error.into()),
    };
    // From here on, a return that leaves the run `running` releases it.
    let mut held = Held::new(&agent, &lease);
    let record = runs.load(namespace, &run_id).await?;
    // A cancel goes on from whatever state its claim found, unless the run
    // ended since: the claim fenced out whoever drove it since the check.
    match cancelling {
        true if record.status.is_terminal() => {
            return Err(RunError::Terminal {
                run_id,
                status: record.status,
            });
        }
        true => {}
        false => check(&record, &agent, reply.as_ref())?,
    }
    // A family this process still holds (the run suspended here) is taken
    // over as it is; its children never went through a crash.
    let family = drive_family(&agent, &driving, &run_id, &record.session_id);
    let events = runs.events(namespace, &run_id, 0).await?;
    let entries = agent.sessions.load(namespace, &record.session_id).await?;
    let checkpoint = &record.checkpoint;
    let (assistant_seq, turn) = turn_events(&events);
    let mut commit = None;
    let mut answered = Vec::new();
    let recovery = reconcile(checkpoint, &events, &entries)?;
    let step = match recovery {
        recovery if cancelling => cancel::step(recovery, || {
            batch_calls(checkpoint, assistant_seq, &events, &entries)
        })?,
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
            retry_feedback,
        } => Step::Received {
            message,
            stop_reason,
            appended,
            retry_feedback,
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
            let given = match reply {
                Some(Reply::Approvals(given)) => given,
                _ => HashMap::new(),
            };
            let (decisions, changed) = decided(decisions, &pending, given);
            let calls = batch_calls(checkpoint, assistant_seq, &events, &entries)?;
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
        Recovery::AwaitingInput { decisions, pending } => {
            let answers = match reply {
                Some(Reply::Answers(answers)) => {
                    check_answers(&run_id, &events, &pending, &answers)?;
                    answers
                }
                _ => HashMap::new(),
            };
            let mut calls = batch_calls(checkpoint, assistant_seq, &events, &entries)?;
            // Each answer is its call's start and end, committed before the
            // run goes on: the model reads the durable result.
            for call in &mut calls {
                let ToolRecovery::Replan(block) = call else {
                    continue;
                };
                let Some(answer) = answers.get(&block.id) else {
                    continue;
                };
                let mut result = answer.render(&block.id);
                if let Some(redactor) = agent.result_redactor() {
                    redactor.result_block(&mut result);
                }
                answered.push(Event::ToolCallStart(ToolCallStartEvent {
                    tool_use_id: block.id.clone(),
                    tool_name: block.name.clone(),
                    input: block.input.clone(),
                    summary: ask_user::summary(&block.input),
                }));
                answered.push(Event::ToolCallEnd(ToolCallEndEvent {
                    tool_name: block.name.clone(),
                    result: result.clone(),
                    duration_ms: 0,
                    canonical_output: None,
                    metadata: None,
                    error_code: None,
                }));
                *call = ToolRecovery::Done(result);
            }
            if !answered.is_empty() {
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
        Recovery::Recompact {
            attempt,
            abandoned,
            redispatch,
        } => Step::Compact {
            attempt,
            abandoned,
            redispatch,
        },
        Recovery::Compacted {
            summary,
            keep_from,
            appended,
            attempt,
        } => Step::Compacted {
            summary,
            keep_from,
            appended,
            attempt,
        },
        Recovery::Cleared {
            before,
            attempt,
            appended,
        } => Step::Cleared {
            before,
            attempt,
            appended,
        },
        Recovery::Unshortened => Step::Unshortened,
        // A cancel's intent is durable: every continuation finishes it.
        Recovery::Aborting { message, appended } => {
            Step::Abort(cancel::Unpaired::Saved { message, appended })
        }
        Recovery::Finishing {
            message,
            ending,
            appended,
        } => Step::Finishing {
            message,
            ending: Box::new(ending),
            appended,
        },
    };
    let mut journal = Journal::new(Arc::clone(&agent.runs), lease, record.last_seq);
    held.watch(&journal);
    let fallbacks = &agent.spec.fallback_models;
    let standing = Standing::of(&events, fallbacks);
    let progress = Progress::of(checkpoint, last_stop(&events), standing, started);
    let trace_id = trace_of(&events);
    // This call's start, for observers; it rides the first barrier.
    let tools = agent.snapshot().definitions();
    journal.stage(Event::Init(InitEvent {
        session_id: record.session_id.clone(),
        run_id: run_id.clone(),
        parent_run_id: record.parent_run_id.clone(),
        model: agent.spec.model.clone(),
        provider: agent.provider.id().to_owned(),
        tools: tools.into_iter().map(|tool| tool.name).collect(),
        trace_id: trace_id.clone(),
        resumed: true,
        turn: u64::from(checkpoint.turn),
        output_schema: None,
        tool_choice: None,
        final_tool: None,
    }));
    for event in answered {
        journal.stage(event);
    }
    // A continuation that names no principal acts for the one the run was
    // started for, so a role rule cannot be escaped by leaving it out.
    let principal = options.principal.clone().or(record.principal.clone());
    let run = identity(record.session_id.clone(), run_id, &principal, trace_id);
    // The children picked up below charge the budget too.
    let budget = match inherited.or_else(|| family.as_ref()?.budget()) {
        Some(meter) => Some(meter),
        None => match agent.spec.budget.clone() {
            Some(limits) => Some(Meter::new(
                limits,
                spent(&agent, &run.run_id, &events).await?,
            )),
            None => None,
        },
    };
    share(&family, &budget);
    if let Some(family) = &family {
        crate::subagents::reattach(&agent, family, &run, &events).await;
    }
    // A cancel's run sees its token cancelled from the start, so a step
    // that would call a provider ends the run instead.
    let token = match cancelling {
        true => {
            let token = CancellationToken::new();
            token.cancel();
            token
        }
        false => options.cancel,
    };
    let mut runner = Runner::new(agent, journal, outbox, run, token, entries, progress);
    runner.family = family;
    runner.budget = budget;
    runner.schema_retries = structured::retries_spent(&events);
    runner.verify_retries = crate::verification::retries_spent(&events);
    let polls = runner.agent.snapshot().polls();
    runner.watch = Watch::of(&runner.agent.spec.loop_guard, polls, &events);
    let forced = crate::compaction::Forced::of(&events, checkpoint.turn);
    (runner.forced, runner.summary_calls) = (forced.spent, forced.summary_calls);
    if let Some(truncation) = &runner.agent.spec.truncation_recovery {
        let spent = Truncated::of(&events, &truncation.reminder());
        runner.truncations = spent.attempts;
        // A response already received continues the parts before it.
        runner.truncated = match checkpoint.phase {
            Phase::AssistantReceived { .. } => spent.before_latest,
            _ => spent.chain,
        };
    }
    if let Some(schema) = schema_of(&events) {
        runner.output = Some(compile(&runner.run.run_id, schema)?);
    }
    if let Some(choice) = tool_choice_of(&events) {
        runner.tool_choice = Some(choice);
    }
    runner.final_tool = recorded_final_tool(&runner, &events)?;
    held.drive(async move {
        if let Some(decisions) = commit {
            runner.commit_decisions(decisions, assistant_seq).await?;
        }
        runner.drive(step).await
    })
    .await
}

/// Every call of the batch of the response at `assistant_seq` (a batch
/// can suspend after some of its calls ran: a re-plan on resume), read
/// from the events after it, so nothing runs twice.
fn batch_calls(
    checkpoint: &Checkpoint,
    assistant_seq: u64,
    events: &[Envelope],
    entries: &[crate::session_log::LogEntry],
) -> Result<Vec<ToolRecovery>, RunError> {
    let batch = Checkpoint {
        phase: Phase::ToolBatch {
            decisions: Vec::new(),
            after_seq: assistant_seq,
        },
        ..checkpoint.clone()
    };
    let Recovery::ToolBatch { calls, .. } = reconcile(&batch, events, entries)? else {
        unreachable!("a tool batch checkpoint reconciles to a tool batch");
    };
    Ok(calls)
}

/// The family of `run_id` this run's stream drives from now on, when the
/// agent has subagents: the one the agent still lists, or a new one.
fn drive_family(
    agent: &Arc<AgentInner>,
    driving: &Driving,
    run_id: &RunId,
    session_id: &SessionId,
) -> Option<Arc<Family>> {
    agent.subagents.as_ref()?;
    let family = agent.family_for(run_id, session_id);
    driving.attach(&family);
    Some(family)
}

/// Let the children of `family` charge `budget`.
fn share(family: &Option<Arc<Family>>, budget: &Option<Meter>) {
    if let (Some(family), Some(meter)) = (family, budget) {
        family.share_budget(meter);
    }
}

/// What the tree of the root run `run_id` spent before this process took
/// it on: the receipts in its `events` and in the logs of the child runs
/// its `Subagent` and `SubagentContinue` calls started (each one's id is
/// `<run id>:<tool_use_id>`). A tool start is durable before its child
/// exists, so no child is missed; a child's wrapped events in the run's
/// log are not counted twice.
async fn spent(agent: &AgentInner, run_id: &RunId, events: &[Envelope]) -> Result<Spent, RunError> {
    let mut spent = Spent::of(events);
    let mut seen = HashSet::new();
    for envelope in events {
        let Event::ToolCallStart(start) = &envelope.event else {
            continue;
        };
        let spawns = [SUBAGENT_TOOL, CONTINUE_TOOL].contains(&start.tool_name.as_str());
        if !spawns || !seen.insert(start.tool_use_id.as_str()) {
            continue;
        }
        let child = RunId::new(format!("{run_id}:{}", start.tool_use_id));
        match agent.runs.events(&agent.namespace, &child, 0).await {
            Ok(events) => spent = spent.and(Spent::of(&events)),
            Err(StoreError::NotFound { .. }) => {}
            Err(error) => return Err(error.into()),
        }
    }
    Ok(spent)
}

/// The error for a new run whose id is already taken.
fn duplicate(run_id: &RunId, namespace: &str) -> StoreError {
    let message = format!("run {run_id} already exists in {namespace}");
    StoreError::Conflict { message }
}

/// The run's own output schema, from the `init` event that created it;
/// `None` when it took the agent's.
fn schema_of(events: &[Envelope]) -> Option<OutputSchema> {
    events.iter().find_map(|envelope| match &envelope.event {
        Event::Init(init) => Some(init.output_schema.clone()),
        _ => None,
    })?
}

/// The run's own tool choice, from the `init` event that created it;
/// `None` when it took the agent's.
fn tool_choice_of(events: &[Envelope]) -> Option<ToolChoice> {
    events.iter().find_map(|envelope| match &envelope.event {
        Event::Init(init) => Some(init.tool_choice.clone()),
        _ => None,
    })?
}

/// The final tool of new run `run_id` of `agent`: the agent's, or, when
/// the run brings its own schema `output`, the one for that schema.
///
/// Raises:
///     RunError: InvalidOutputSchema when the tool made from `output`
///         cannot be offered.
fn run_final_tool(
    agent: &AgentInner,
    run_id: &RunId,
    output: Option<&OutputContract>,
) -> Result<Option<FinalTool>, RunError> {
    let Some(output) = output else {
        return Ok(agent.final_tool.clone());
    };
    let named = agent.spec.final_tool_name.as_deref();
    let native = agent.provider.native_output_schema();
    let Some(tool) = FinalTool::of(named, Some(output), native) else {
        return Ok(None);
    };
    let tools = agent.snapshot();
    match tool.problem(|name| tools.resolve(name).is_some()) {
        None => Ok(Some(tool)),
        Some(reason) => Err(RunError::InvalidOutputSchema {
            run_id: run_id.clone(),
            reason,
        }),
    }
}

/// The final tool the run's `init` event recorded, for the runner's
/// schema; `None` when it recorded none.
///
/// Raises:
///     RunError: InvalidOutputSchema when the recorded tool is no longer
///         the agent's, nor made from the run's schema.
fn recorded_final_tool(
    runner: &Runner,
    events: &[Envelope],
) -> Result<Option<FinalTool>, RunError> {
    let recorded = events.iter().find_map(|envelope| match &envelope.event {
        Event::Init(init) => Some(init.final_tool.as_deref()),
        _ => None,
    });
    let Some(Some(name)) = recorded else {
        return Ok(None);
    };
    let named = runner.agent.spec.final_tool_name.as_deref();
    match FinalTool::recorded(name, named, runner.output.as_deref()) {
        Some(tool) => Ok(Some(tool)),
        None => Err(RunError::InvalidOutputSchema {
            run_id: runner.run.run_id.clone(),
            reason: format!("the run's final tool {name:?} is no longer this agent's"),
        }),
    }
}

/// `schema`, compiled for run `run_id`.
fn compile(run_id: &RunId, schema: OutputSchema) -> Result<Arc<OutputContract>, RunError> {
    match OutputContract::compile(schema) {
        Ok(output) => Ok(Arc::new(output)),
        Err(reason) => Err(RunError::InvalidOutputSchema {
            run_id: run_id.clone(),
            reason,
        }),
    }
}

/// The trace id the run's `init` event stored; `None` when it has none.
pub(super) fn trace_of(events: &[Envelope]) -> Option<String> {
    events.iter().find_map(|envelope| match &envelope.event {
        Event::Init(init) => Some(init.trace_id.clone()),
        _ => None,
    })?
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

/// Refuse a terminal run, a run of another contract, an approve on a run
/// that is not awaiting approval, and an answer on one not awaiting input.
/// A run whose approve or answer committed is in its tool batch and may
/// still have a live owner, so a second one (a retry, or a second click) is
/// refused too: the host resumes it. A cancel takes a run awaiting
/// either, or one a cancel left half done, under any contract: it runs
/// nothing of the agent's.
fn check(record: &RunRecord, agent: &AgentInner, reply: Option<&Reply>) -> Result<(), RunError> {
    if record.status.is_terminal() {
        return Err(RunError::Terminal {
            run_id: record.run_id.clone(),
            status: record.status,
        });
    }
    let cancelling = matches!(reply, Some(Reply::Cancel));
    if !cancelling && record.contract != agent.contract {
        return Err(RunError::ContractMismatch {
            run_id: record.run_id.clone(),
            expected: agent.contract,
            found: record.contract,
        });
    }
    let phase = &record.checkpoint.phase;
    let suspended = match reply {
        None => true,
        Some(Reply::Approvals(_)) => matches!(phase, Phase::AwaitingApproval { .. }),
        Some(Reply::Answers(_)) => matches!(phase, Phase::AwaitingInput { .. }),
        Some(Reply::Cancel) => matches!(
            phase,
            Phase::AwaitingApproval { .. } | Phase::AwaitingInput { .. } | Phase::Aborting { .. }
        ),
    };
    if !suspended {
        return Err(RunError::NotSuspended {
            run_id: record.run_id.clone(),
        });
    }
    Ok(())
}

/// Refuse `answers` unless each one answers a `pending` request and fits
/// the questions its latest `input_request` among `events` asked. There
/// must be at least one; a pending request left out is asked again.
fn check_answers(
    run_id: &RunId,
    events: &[Envelope],
    pending: &[String],
    answers: &HashMap<String, AskUserAnswer>,
) -> Result<(), RunError> {
    let invalid = |reason: String| RunError::InvalidAnswer {
        run_id: run_id.clone(),
        reason,
    };
    if answers.is_empty() {
        return Err(invalid("no answers given".into()));
    }
    for (request_id, answer) in answers {
        if !pending.contains(request_id) {
            return Err(invalid(format!("request {request_id:?} is not pending")));
        }
        let asked = events
            .iter()
            .rev()
            .find_map(|envelope| match &envelope.event {
                Event::InputRequest(request) if &request.request_id == request_id => {
                    Some(&request.questions)
                }
                _ => None,
            });
        let Some(questions) = asked else {
            return Err(invalid(format!("request {request_id:?} was never asked")));
        };
        answer
            .check(questions)
            .map_err(|reason| invalid(format!("request {request_id:?}: {reason}")))?;
    }
    Ok(())
}

/// Record the host's answers for the pending calls; also says whether any
/// answer applied.
fn decided(
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
    trace_id: Option<String>,
) -> RunIdentity {
    RunIdentity {
        session_id,
        run_id,
        principal: principal.clone(),
        trace_id,
    }
}
