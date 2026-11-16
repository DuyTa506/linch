//! The turn hook points and the stream-only `hook` records.
//!
//! Hooks are not barriers: nothing is flushed around a chain, and every
//! [`HookRecord`] goes to the caller at once as a stream-only
//! [`Event::Hook`] carrying the last durable seq. A chain with no hook is
//! skipped before its request is built, so a run without hooks does no
//! hook work. A run cancel drops a chain the run waits on (a dropped RPC
//! callback is cancelled on the client), and the run aborts; the hooks
//! that answered before the cancel still have their records sent.

use std::sync::Arc;

use super::Outbox;
use super::turn::{Runner, Step};
use crate::agent::{AgentInner, RunError};
use crate::events::kinds::{
    ContextBuildEvent, HookEvent, HookOutcome, UserEvent, UserEventSubtype, VerificationAction,
};
use crate::events::model_call::CallDisposition;
use crate::events::{Envelope, Event, WIRE_VERSION, now_ms};
use crate::hooks::{
    AfterProviderCallOutcome, AfterProviderCallRequest, AgentStartRequest, AgentStopRequest,
    BeforeProviderCallOutcome, BeforeProviderCallRequest, ContextBuildRequest,
    ContextBuildResponse, EventEmitRequest, HookRecord, HookStatus, Hooks, PostCompactRequest,
    PostTurnRequest, PreCompactRequest, ProviderCallStartRequest, ProviderCallStopRequest,
    StopOutcome, StopRequest, TurnStartRequest, UserPromptOutcome, UserPromptRequest,
};
use crate::ids::{RunId, SessionId};
use crate::json::JsonObject;
use crate::message::{ContentBlock, Message, Role, TextBlock, ToolResultBlock};
use crate::provider::{AssistantAssembly, ProviderRequest};
use crate::run::{Phase, RunStatus, StopReason};
use crate::skills::system_reminder;
use crate::usage::Usage;

/// How many times one run call honours a `stop` hook's
/// [`StopResponse::Retry`](crate::hooks::StopResponse::Retry); a later
/// retry counts as an allow. Python's `_MAX_FINAL_ANSWER_REENTRIES`, which
/// bounds `before_final_answer` retries the same way and is not
/// checkpointed either: a resume starts afresh. An agent with a
/// `verification` spec bounds them with its `max_retries` instead.
const MAX_FINAL_ANSWER_RETRIES: u32 = 1;

/// What a `before_provider_call` hook decided for the request about to be
/// sent.
pub(super) enum BeforeCall {
    /// Send this request: the original, or the last rewrite.
    Send(Box<ProviderRequest>),
    /// Send nothing: take another turn with this feedback first.
    ForceContinue(String),
    /// A hook blocked the call, or failed closed.
    Blocked(String),
    /// The run already ended: the run cancel interrupted the chain.
    Ended,
}

/// What an `after_provider_call` hook decided for the response a provider
/// call just assembled.
pub(super) enum AfterCall {
    /// Accept this response: the provider's, or the last rewrite.
    Send(Box<AssistantAssembly>),
    /// Accept this response, then send this feedback for another turn
    /// instead of acting on it.
    Retry(Box<AssistantAssembly>, String),
    /// A hook blocked the response, or failed closed.
    Blocked(String),
    /// The run already ended: the run cancel interrupted the chain.
    Ended,
}

/// What a text-only response leads to, after the `stop` chain.
pub(super) enum Stopping {
    /// Every hook let the run stop; this is the final-answer text, the
    /// original or the last rewrite.
    Stop { text: String },
    /// A hook kept the run going: this feedback step is next.
    Continue(Step),
    /// The run already ended: a hook blocked the stop, failed closed, or
    /// the run cancel interrupted the chain.
    Ended,
}

/// Queue each record as a stream-only `hook` envelope at `seq`.
pub(super) fn push_records(
    outbox: &Outbox,
    run_id: &RunId,
    seq: u64,
    records: Vec<HookRecord>,
    tool_use_id: Option<&str>,
) {
    for record in records {
        outbox.push(Envelope {
            v: WIRE_VERSION,
            seq,
            run_id: run_id.clone(),
            ts_ms: now_ms(),
            ignorable: false,
            event: Event::Hook(hook_event(record, tool_use_id)),
        });
    }
}

fn hook_event(record: HookRecord, tool_use_id: Option<&str>) -> HookEvent {
    let outcome = match record.status {
        HookStatus::Answered { verdict } => HookOutcome::Answered {
            verdict: verdict.to_owned(),
        },
        HookStatus::Failed { error, fail_mode } => HookOutcome::Failed { error, fail_mode },
        HookStatus::TimedOut { after, fail_mode } => HookOutcome::TimedOut {
            timeout_ms: u64::try_from(after.as_millis()).unwrap_or(u64::MAX),
            fail_mode,
        },
    };
    HookEvent {
        point: record.point,
        policy_id: record.policy_id.to_string(),
        policy_version: record.policy_version,
        tool_use_id: tool_use_id.map(str::to_owned),
        outcome,
    }
}

/// Run `user_prompt_submit` on a new run's prompt, before anything is
/// written. Its records go out at seq 0.
///
/// Returns:
///     The prompt to start the run with: the user's, or the last rewrite.
///
/// Raises:
///     RunError: `PromptBlocked` when a hook blocks the prompt or fails
///         closed; the run is never created.
pub(super) async fn submit(
    agent: &AgentInner,
    outbox: &Outbox,
    session_id: &SessionId,
    run_id: &RunId,
    prompt: String,
) -> Result<Message, RunError> {
    let redactor = agent.prompt_redactor();
    let prompt = match redactor {
        Some(redactor) => redactor.text(&prompt).into_owned(),
        None => prompt,
    };
    let chain = &agent.hooks.user_prompt_submit;
    if chain.is_empty() {
        return Ok(Message::user_text(prompt));
    }
    let request = UserPromptRequest {
        session_id: session_id.clone(),
        run_id: run_id.clone(),
        content: vec![ContentBlock::text(prompt)],
    };
    let result = chain.run(request).await;
    push_records(outbox, run_id, 0, result.records, None);
    match result.outcome {
        UserPromptOutcome::Submit { content } => {
            let mut prompt = Message::new(Role::User, content);
            if let Some(redactor) = redactor {
                redactor.message(&mut prompt);
            }
            Ok(prompt)
        }
        UserPromptOutcome::Blocked { reason } => Err(RunError::PromptBlocked {
            run_id: run_id.clone(),
            reason,
        }),
    }
}

/// Run `agent_start` for a new run, after `submit` succeeds. Not fired on a
/// resume, and not fired when `submit` blocked the prompt: the run is never
/// created then, so there is nothing to report on.
pub(super) async fn agent_start(
    agent: &AgentInner,
    outbox: &Outbox,
    session_id: &SessionId,
    run_id: &RunId,
    prompt: &Message,
) {
    let chain = &agent.hooks.agent_start;
    if chain.is_empty() {
        return;
    }
    let tools = agent.snapshot().definitions();
    let request = AgentStartRequest {
        session_id: session_id.clone(),
        run_id: run_id.clone(),
        model: agent.spec.model.clone(),
        prompt: prompt.text(),
        tools: tools.into_iter().map(|tool| tool.name).collect(),
    };
    let mut records = Vec::new();
    chain.run_into(&request, &mut records).await;
    push_records(outbox, run_id, 0, records, None);
}

/// Run `event_emit` for each of `ready`'s envelopes, in that order, before
/// they are delivered to the caller. `ready` only ever holds envelopes that
/// are both streamed and durably logged ([`crate::events::Persistence::Both`]):
/// a stream-only delta, tool-progress event or hook record never reaches
/// here, so firing this point cannot recurse into itself. Every hook's
/// records are pushed once, after the whole batch ran, as their own
/// stream-only envelopes at `seq` — not fed back into the chain.
pub(super) async fn event_emit(
    hooks: &Hooks,
    outbox: &Outbox,
    session_id: &SessionId,
    run_id: &RunId,
    seq: u64,
    ready: &[Envelope],
) {
    let chain = &hooks.event_emit;
    if chain.is_empty() || ready.is_empty() {
        return;
    }
    let mut records = Vec::new();
    for envelope in ready {
        let request = EventEmitRequest {
            session_id: session_id.clone(),
            run_id: run_id.clone(),
            event: envelope.event.clone(),
        };
        chain.run_into(&request, &mut records).await;
    }
    push_records(outbox, run_id, seq, records, None);
}

/// Result text of the first tool call of a retried response when the hook
/// gave no feedback (Python's).
const RETRY_TEXT: &str = "Provider hook requested another turn.";

/// Result text of every later tool call of a retried response (Python's).
const NOT_RUN_TEXT: &str = "Tool not executed; revise and resubmit your final answer.";

/// The user message an `after_provider_call` retry sends back after
/// `response`, as Python builds it: with no tool calls, `feedback` in a
/// system reminder; otherwise an error result for every tool call, none of
/// which ran, the first carrying `feedback`, so the provider history stays
/// paired.
pub(super) fn retry_message(response: &Message, feedback: &str) -> Message {
    let calls: Vec<_> = response.tool_uses().collect();
    if calls.is_empty() {
        return Message::user_text(system_reminder(feedback));
    }
    let first = if feedback.is_empty() {
        RETRY_TEXT
    } else {
        feedback
    };
    let results = (calls.iter().enumerate()).map(|(index, call)| {
        let text = if index == 0 { first } else { NOT_RUN_TEXT };
        ContentBlock::ToolResult(ToolResultBlock::text(&call.id, text, true))
    });
    Message::new(Role::User, results.collect())
}

/// Add request-only context to the tail of `messages`: after the blocks
/// of the last user message, or as a new user message. The cached prefix
/// (system, tools, every earlier message) stays byte-identical.
pub(super) fn add_context(messages: &mut Vec<Message>, blocks: Vec<TextBlock>) {
    if blocks.is_empty() {
        return;
    }
    let blocks = blocks.into_iter().map(ContentBlock::Text);
    match messages.last_mut() {
        Some(last) if last.role == Role::User => last.content.extend(blocks),
        _ => messages.push(Message::new(Role::User, blocks.collect())),
    }
}

impl Runner {
    /// Queue `records` as stream-only envelopes at the last durable seq.
    pub(super) fn push_records(&self, records: Vec<HookRecord>, tool_use_id: Option<&str>) {
        let seq = self.journal.durable_seq();
        push_records(&self.outbox, &self.run.run_id, seq, records, tool_use_id);
    }

    /// [`event_emit`] for `ready`, about to be delivered to the caller.
    pub(super) async fn event_emit(&self, ready: &[Envelope]) {
        let seq = self.journal.durable_seq();
        event_emit(
            &self.agent.hooks,
            &self.outbox,
            &self.run.session_id,
            &self.run.run_id,
            seq,
            ready,
        )
        .await;
    }

    /// The `context_build` blocks for a request over `messages`; `None`
    /// when the run cancel interrupted the chain.
    pub(super) async fn context(
        &self,
        messages: Arc<Vec<Arc<Message>>>,
    ) -> Option<ContextBuildResponse> {
        let chain = &self.agent.hooks.context_build;
        if chain.is_empty() {
            return Some(ContextBuildResponse::default());
        }
        let request = ContextBuildRequest {
            session_id: self.run.session_id.clone(),
            run_id: self.run.run_id.clone(),
            messages,
        };
        let mut records = Vec::new();
        let run = chain.run_into(&request, &mut records);
        let context = self.cancel.run_until_cancelled(run).await;
        self.push_records(records, None);
        context
    }

    /// Stage the `context_build` event of the primary `request`: what it
    /// was built from, with the context hooks' `metadata`. It rides the
    /// call's `ProviderPending` barrier. The kernel keeps no context budget
    /// and trims nothing, so `budget` is empty.
    pub(super) fn stage_context(&mut self, request: &ProviderRequest, metadata: JsonObject) {
        let tools = request.tools.iter().map(|tool| tool.name.clone());
        self.journal.stage(Event::ContextBuild(ContextBuildEvent {
            model: request.model.clone(),
            system_blocks: request.system.len() as u64,
            messages: request.messages.len() as u64,
            selected_tools: Some(tools.collect()),
            budget: JsonObject::new(),
            metadata,
        }));
    }

    /// Run `stop` on a text-only response, whose text parsed to
    /// `structured_output` under the run's schema.
    pub(super) async fn stop(
        &mut self,
        text: &str,
        structured_output: Option<&JsonObject>,
    ) -> Result<Stopping, RunError> {
        let chain = &self.agent.hooks.stop;
        if chain.is_empty() {
            return Ok(Stopping::Stop {
                text: text.to_owned(),
            });
        }
        let request = StopRequest {
            session_id: self.run.session_id.clone(),
            run_id: self.run.run_id.clone(),
            text: text.to_owned(),
            structured_output: structured_output.cloned(),
        };
        let mut records = Vec::new();
        // With verifiers, a hook's retry draws on their durable budget and
        // is reported as a `verification` event under its policy id.
        let verifiers = self.agent.verifiers.clone();
        let retry = match &verifiers {
            Some(verifiers) => self.verify_retries < verifiers.max_retries,
            None => self.final_answer_retries < MAX_FINAL_ANSWER_RETRIES,
        };
        let output = self.output.clone();
        let check = move |text: &str| output.as_ref()?.check(text).ok();
        let run = chain.run_into(request, retry, &check, &mut records);
        let outcome = self.cancel.run_until_cancelled(run).await;
        let retried = (records.iter())
            .find(|record| matches!(record.status, HookStatus::Answered { verdict: "retry" }))
            .map(|record| record.policy_id.to_string());
        self.push_records(records, None);
        let Some(outcome) = outcome else {
            self.abort().await?;
            return Ok(Stopping::Ended);
        };
        if let (Some(_), false, Some(hook)) = (&verifiers, retry, &retried) {
            // The hook's retry counted as an allow.
            self.stage_verification(hook, VerificationAction::Exhausted, "");
        }
        let feedback = match outcome {
            // The model's text is redacted already; a rewrite may not be.
            StopOutcome::Stop { text } => {
                let text = match self.agent.response_redactor() {
                    Some(redactor) => redactor.text(&text).into_owned(),
                    None => text,
                };
                return Ok(Stopping::Stop { text });
            }
            StopOutcome::Continue { feedback } => feedback,
            StopOutcome::Retry { feedback } => {
                match (&verifiers, &retried) {
                    (Some(_), Some(hook)) => {
                        self.verify_retries += 1;
                        self.stage_verification(hook, VerificationAction::Retry, &feedback);
                    }
                    _ => self.final_answer_retries += 1,
                }
                feedback
            }
            StopOutcome::Failed { reason } => {
                self.hook_failed(reason).await?;
                return Ok(Stopping::Ended);
            }
        };
        // Python's `_gate_retry_tail`: the feedback in a system reminder.
        let message = Message::user_text(system_reminder(&feedback));
        Ok(Stopping::Continue(self.feedback(message).await?))
    }

    /// Hold `message`, a hook's feedback for the model, as the user message
    /// that ends this turn, with any catalog notice: staged as a
    /// notification and saved as `ToolResults`, which already records and
    /// recovers the message to append before the next turn.
    pub(super) async fn feedback(&mut self, mut message: Message) -> Result<Step, RunError> {
        message.content.extend(self.catalog_change());
        self.journal.stage(Event::User(UserEvent {
            message: message.clone(),
            subtype: UserEventSubtype::Notification,
        }));
        self.save(Phase::ToolResults {
            message: message.clone(),
        })
        .await?;
        Ok(Step::Results {
            message,
            appended: false,
        })
    }

    /// Run `post_turn` for the turn that just finished. Returns false when
    /// the run cancel interrupted the chain.
    pub(super) async fn post_turn(&self, stop_reason: StopReason) -> bool {
        let chain = &self.agent.hooks.post_turn;
        if chain.is_empty() {
            return true;
        }
        let request = PostTurnRequest {
            session_id: self.run.session_id.clone(),
            run_id: self.run.run_id.clone(),
            turn: self.turn,
            stop_reason,
        };
        let mut records = Vec::new();
        let run = chain.run_into(&request, &mut records);
        let ran = self.cancel.run_until_cancelled(run).await.is_some();
        // The hooks that answered before a cancel keep their records.
        self.push_records(records, None);
        ran
    }

    /// Run `agent_stop` for the run's terminal result. `end` is the single
    /// terminal writer for every end path, so this fires exactly once.
    pub(super) async fn agent_stop(&self, status: RunStatus, stop_reason: StopReason) {
        let chain = &self.agent.hooks.agent_stop;
        if chain.is_empty() {
            return;
        }
        let request = AgentStopRequest {
            session_id: self.run.session_id.clone(),
            run_id: self.run.run_id.clone(),
            status,
            stop_reason,
        };
        let mut records = Vec::new();
        chain.run_into(&request, &mut records).await;
        self.push_records(records, None);
    }

    /// Run `turn_start` for the turn about to start, before the
    /// compaction/dispatch decision.
    pub(super) async fn turn_start_hook(&self, turn: u32) {
        let chain = &self.agent.hooks.turn_start;
        if chain.is_empty() {
            return;
        }
        let request = TurnStartRequest {
            session_id: self.run.session_id.clone(),
            run_id: self.run.run_id.clone(),
            turn,
        };
        let mut records = Vec::new();
        chain.run_into(&request, &mut records).await;
        self.push_records(records, None);
    }

    /// Run `before_provider_call` on the request about to be sent for
    /// `turn`. The request `stage_context` and the delivery record is built
    /// from is whatever this returns, so a rewrite is what the audit trail
    /// and a resumed run both see.
    pub(super) async fn before_provider_call(
        &self,
        request: ProviderRequest,
        turn: u32,
    ) -> BeforeCall {
        let chain = &self.agent.hooks.before_provider_call;
        if chain.is_empty() {
            return BeforeCall::Send(Box::new(request));
        }
        let request = BeforeProviderCallRequest {
            session_id: self.run.session_id.clone(),
            run_id: self.run.run_id.clone(),
            turn,
            request,
        };
        let mut records = Vec::new();
        let run = chain.run_into(request, &mut records);
        let outcome = self.cancel.run_until_cancelled(run).await;
        self.push_records(records, None);
        match outcome {
            None => BeforeCall::Ended,
            Some(BeforeProviderCallOutcome::Send { request }) => BeforeCall::Send(request),
            Some(BeforeProviderCallOutcome::ForceContinue { feedback }) => {
                BeforeCall::ForceContinue(feedback)
            }
            Some(BeforeProviderCallOutcome::Blocked { reason }) => BeforeCall::Blocked(reason),
        }
    }

    /// Run `provider_call_start` before dispatching one provider call.
    pub(super) async fn provider_call_start(&self, turn: u32, model: &str) {
        let chain = &self.agent.hooks.provider_call_start;
        if chain.is_empty() {
            return;
        }
        let request = ProviderCallStartRequest {
            session_id: self.run.session_id.clone(),
            run_id: self.run.run_id.clone(),
            turn,
            model: model.to_owned(),
        };
        let mut records = Vec::new();
        chain.run_into(&request, &mut records).await;
        self.push_records(records, None);
    }

    /// Whether an `after_provider_call` chain is attached: a hook then runs
    /// between a provider stream that settled with an answer and the write
    /// of that answer, so the run makes the call's receipt durable first
    /// (see `Runner::dispatch`).
    pub(super) fn checks_responses(&self) -> bool {
        !self.agent.hooks.after_provider_call.is_empty()
    }

    /// Run `provider_call_stop` once the call returns, on every exit path:
    /// accepted, empty, failed or cancelled.
    pub(super) async fn provider_call_stop(
        &self,
        turn: u32,
        model: &str,
        disposition: CallDisposition,
        usage: Option<Usage>,
        duration_ms: u64,
    ) {
        let chain = &self.agent.hooks.provider_call_stop;
        if chain.is_empty() {
            return;
        }
        let request = ProviderCallStopRequest {
            session_id: self.run.session_id.clone(),
            run_id: self.run.run_id.clone(),
            turn,
            model: model.to_owned(),
            disposition,
            usage,
            duration_ms,
        };
        let mut records = Vec::new();
        chain.run_into(&request, &mut records).await;
        self.push_records(records, None);
    }

    /// Run `after_provider_call` on the response a provider call just
    /// assembled for `turn`. Whatever this returns is what `accept` stages
    /// as the `Assistant` event and appends to the session, so a rewrite is
    /// what the audit trail and a resumed run both see.
    ///
    /// With response redaction on, the chain sees the response already
    /// redacted, and a rewrite it makes is redacted again, so neither a
    /// hook nor the log ever holds the raw text.
    pub(super) async fn after_provider_call(
        &self,
        mut assembly: AssistantAssembly,
        turn: u32,
    ) -> AfterCall {
        let redactor = self.agent.response_redactor();
        if let Some(redactor) = redactor {
            redactor.message(&mut assembly.message);
        }
        let chain = &self.agent.hooks.after_provider_call;
        if chain.is_empty() {
            return AfterCall::Send(Box::new(assembly));
        }
        let redacted = redactor.map(|_| assembly.message.clone());
        let request = AfterProviderCallRequest {
            session_id: self.run.session_id.clone(),
            run_id: self.run.run_id.clone(),
            turn,
            assembly,
        };
        let mut records = Vec::new();
        let run = chain.run_into(request, &mut records);
        let outcome = self.cancel.run_until_cancelled(run).await;
        self.push_records(records, None);
        let again = |mut assembly: Box<AssistantAssembly>| {
            if let (Some(redactor), Some(redacted)) = (redactor, &redacted)
                && assembly.message != *redacted
            {
                redactor.message(&mut assembly.message);
            }
            assembly
        };
        match outcome {
            None => AfterCall::Ended,
            Some(AfterProviderCallOutcome::Send { assembly }) => AfterCall::Send(again(assembly)),
            Some(AfterProviderCallOutcome::Retry { assembly, feedback }) => {
                AfterCall::Retry(again(assembly), feedback)
            }
            Some(AfterProviderCallOutcome::Blocked { reason }) => AfterCall::Blocked(reason),
        }
    }

    /// Run `pre_compact` once compaction's due checks passed, before the
    /// summary call.
    pub(super) async fn pre_compact(&self, turn: u32, messages: u64, tokens: u64, strategy: &str) {
        let chain = &self.agent.hooks.pre_compact;
        if chain.is_empty() {
            return;
        }
        let request = PreCompactRequest {
            session_id: self.run.session_id.clone(),
            run_id: self.run.run_id.clone(),
            turn,
            messages,
            tokens,
            strategy: strategy.to_owned(),
        };
        let mut records = Vec::new();
        chain.run_into(&request, &mut records).await;
        self.push_records(records, None);
    }

    /// Run `post_compact` once compaction replaced the view with a summary.
    /// Not fired when the summary call failed or gave no text.
    #[allow(clippy::too_many_arguments)]
    pub(super) async fn post_compact(
        &self,
        turn: u32,
        messages_before: u64,
        messages_after: u64,
        tokens_before: u64,
        tokens_after: u64,
        strategy: &str,
    ) {
        let chain = &self.agent.hooks.post_compact;
        if chain.is_empty() {
            return;
        }
        let request = PostCompactRequest {
            session_id: self.run.session_id.clone(),
            run_id: self.run.run_id.clone(),
            turn,
            messages_before,
            messages_after,
            tokens_before,
            tokens_after,
            strategy: strategy.to_owned(),
        };
        let mut records = Vec::new();
        chain.run_into(&request, &mut records).await;
        self.push_records(records, None);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn block(text: &str) -> TextBlock {
        TextBlock { text: text.into() }
    }

    #[test]
    fn context_joins_the_last_user_message_after_its_blocks() {
        let results = Message::new(
            Role::User,
            vec![ContentBlock::ToolResult(ToolResultBlock::text(
                "c1", "18C", false,
            ))],
        );
        let mut messages = vec![Message::user_text("hi"), results.clone()];
        add_context(&mut messages, vec![block("today is Friday")]);
        assert_eq!(messages[0], Message::user_text("hi"));
        assert_eq!(messages[1].content[0], results.content[0]);
        assert_eq!(
            messages[1].content[1],
            ContentBlock::text("today is Friday")
        );
        assert_eq!(messages.len(), 2);
    }

    #[test]
    fn context_after_an_assistant_message_is_a_new_user_message() {
        let mut messages = vec![Message::assistant_text("hi")];
        add_context(&mut messages, vec![block("a"), block("b")]);
        let added = Message::new(
            Role::User,
            vec![ContentBlock::text("a"), ContentBlock::text("b")],
        );
        assert_eq!(messages, [Message::assistant_text("hi"), added]);
        add_context(&mut messages, Vec::new());
        assert_eq!(messages.len(), 2, "no blocks add nothing");
    }
}
