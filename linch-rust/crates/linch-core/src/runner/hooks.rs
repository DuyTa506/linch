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
    ContextBuildEvent, HookEvent, HookOutcome, UserEvent, UserEventSubtype,
};
use crate::events::{Envelope, Event, WIRE_VERSION, now_ms};
use crate::hooks::{
    ContextBuildRequest, HookRecord, HookStatus, PostTurnRequest, StopOutcome, StopRequest,
    UserPromptOutcome, UserPromptRequest,
};
use crate::ids::{RunId, SessionId};
use crate::json::JsonObject;
use crate::message::{ContentBlock, Message, Role, TextBlock};
use crate::provider::ProviderRequest;
use crate::run::{Phase, StopReason};

/// What a text-only response leads to, after the `stop` chain.
pub(super) enum Stopping {
    /// Every hook let the run stop.
    Stop,
    /// A hook kept the run going: this feedback step is next.
    Continue(Step),
    /// The run already ended: a hook failed closed, or the run cancel
    /// interrupted the chain.
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
        UserPromptOutcome::Submit { content } => Ok(Message::new(Role::User, content)),
        UserPromptOutcome::Blocked { reason } => Err(RunError::PromptBlocked {
            run_id: run_id.clone(),
            reason,
        }),
    }
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

    /// The `context_build` blocks for a request over `messages`; `None`
    /// when the run cancel interrupted the chain.
    pub(super) async fn context(&self, messages: Arc<Vec<Arc<Message>>>) -> Option<Vec<TextBlock>> {
        let chain = &self.agent.hooks.context_build;
        if chain.is_empty() {
            return Some(Vec::new());
        }
        let request = ContextBuildRequest {
            session_id: self.run.session_id.clone(),
            run_id: self.run.run_id.clone(),
            messages,
        };
        let mut records = Vec::new();
        let run = chain.run_into(&request, &mut records);
        let blocks = self.cancel.run_until_cancelled(run).await;
        self.push_records(records, None);
        blocks
    }

    /// Stage the `context_build` event of the primary `request`: what it
    /// was built from. It rides the call's `ProviderPending` barrier. The
    /// kernel keeps no context budget and trims nothing, so `budget` and
    /// `metadata` are empty.
    pub(super) fn stage_context(&mut self, request: &ProviderRequest) {
        let tools = request.tools.iter().map(|tool| tool.name.clone());
        self.journal.stage(Event::ContextBuild(ContextBuildEvent {
            system_blocks: request.system.len() as u64,
            messages: request.messages.len() as u64,
            selected_tools: Some(tools.collect()),
            budget: JsonObject::new(),
            metadata: JsonObject::new(),
        }));
    }

    /// Run `stop` on a text-only response.
    pub(super) async fn stop(&mut self, text: &str) -> Result<Stopping, RunError> {
        let chain = &self.agent.hooks.stop;
        if chain.is_empty() {
            return Ok(Stopping::Stop);
        }
        let request = StopRequest {
            session_id: self.run.session_id.clone(),
            run_id: self.run.run_id.clone(),
            text: text.to_owned(),
        };
        let mut records = Vec::new();
        let run = chain.run_into(&request, &mut records);
        let outcome = self.cancel.run_until_cancelled(run).await;
        self.push_records(records, None);
        let Some(outcome) = outcome else {
            self.abort().await?;
            return Ok(Stopping::Ended);
        };
        let feedback = match outcome {
            StopOutcome::Stop => return Ok(Stopping::Stop),
            StopOutcome::Continue { feedback } => feedback,
            StopOutcome::Failed { reason } => {
                self.hook_failed(reason).await?;
                return Ok(Stopping::Ended);
            }
        };
        // The feedback is a user message to append before the next turn,
        // which is what `ToolResults` already records and recovers.
        let message = Message::user_text(feedback);
        self.journal.stage(Event::User(UserEvent {
            message: message.clone(),
            subtype: UserEventSubtype::Notification,
        }));
        self.save(Phase::ToolResults {
            message: message.clone(),
        })
        .await?;
        Ok(Stopping::Continue(Step::Results {
            message,
            appended: false,
        }))
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
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::message::ToolResultBlock;

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
