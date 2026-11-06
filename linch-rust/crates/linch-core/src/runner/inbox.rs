//! The run's inbox: what waits for the session goes into the next logged
//! user message, after what is already there (see [`crate::coordination`]).

use super::turn::{Runner, Step};
use crate::agent::{AgentInner, RunError};
use crate::coordination::MailboxMessage;
use crate::events::Event;
use crate::events::kinds::{ChildRunStatusEvent, UserEvent, UserEventSubtype};
use crate::ids::SessionId;
use crate::message::{ContentBlock, Message, Role};
use crate::run::Phase;
use crate::store::StoreError;

/// Take every message waiting in the agent's mailbox for `session`; none
/// without a mailbox.
///
/// Raises:
///     RunError: `Store(Backend)` holding the mailbox error.
pub(super) async fn drain(
    agent: &AgentInner,
    session: &SessionId,
) -> Result<Vec<MailboxMessage>, RunError> {
    let Some(mailbox) = &agent.mailbox else {
        return Ok(Vec::new());
    };
    let drained = mailbox.drain(session.as_str()).await;
    drained.map_err(|error| StoreError::Backend(Box::new(error)).into())
}

/// The model-facing blocks of drained peer messages.
pub(super) fn blocks(messages: &[MailboxMessage]) -> impl Iterator<Item = ContentBlock> + '_ {
    messages
        .iter()
        .map(|message| ContentBlock::text(message.render()))
}

/// Send back what a run drained but could not log. Best effort: a
/// mailbox that fails here loses them, and the caller reports its own
/// error.
pub(super) async fn redeliver(agent: &AgentInner, messages: Vec<MailboxMessage>) {
    let Some(mailbox) = &agent.mailbox else {
        return;
    };
    for message in messages {
        let _ = mailbox.send(message).await;
    }
}

impl Runner {
    /// The blocks to add to the user message this turn ends with: the
    /// notification of each background child that ended (none for one
    /// `TaskStop` stopped), whose `child_run_status` this stages, then the
    /// session's peer messages.
    pub(super) async fn notes(&mut self) -> Result<Vec<ContentBlock>, RunError> {
        let mut notes = Vec::new();
        let done = (self.family.as_ref()).map(|family| family.take_done());
        for completion in done.into_iter().flatten() {
            let mark = ChildRunStatusEvent {
                child_run_id: completion.child.clone(),
                status: completion.status,
            };
            self.journal.stage(Event::ChildRunStatus(mark));
            if !completion.stopped {
                notes.push(ContentBlock::text(completion.notification()));
            }
        }
        let peers = drain(&self.agent, &self.run.session_id).await?;
        notes.extend(blocks(&peers));
        Ok(notes)
    }

    /// After a text answer: while a background child runs or ended
    /// undelivered, wait for an end and log what came in as a user message
    /// for another turn, the way stop feedback is. An end with nothing to
    /// report (a child `TaskStop` stopped) is recorded, and the wait goes
    /// on for the others. `None` when nothing waits; a cancel while
    /// waiting aborts the run.
    pub(super) async fn hear_back(&mut self) -> Result<Option<Option<Step>>, RunError> {
        let Some(family) = self.family.clone() else {
            return Ok(None);
        };
        let mut notes = Vec::new();
        while notes.is_empty() {
            if !family.has_background() {
                return Ok(None);
            }
            // A cancel aborts the run, even one whose children ended of it.
            let settled = self.cancel.run_until_cancelled(family.settled()).await;
            if settled.is_none() || self.cancel.is_cancelled() {
                self.abort().await?;
                return Ok(Some(None));
            }
            notes = self.notes().await?;
        }
        notes.extend(self.catalog_change());
        let message = Message::new(Role::User, notes);
        self.journal.stage(Event::User(UserEvent {
            message: message.clone(),
            subtype: UserEventSubtype::Notification,
        }));
        self.save(Phase::ToolResults {
            message: message.clone(),
        })
        .await?;
        Ok(Some(Some(Step::Results {
            message,
            appended: false,
        })))
    }

    /// Stage the wrapped events the background children left since the
    /// last time, for the next write.
    pub(super) fn stage_children(&mut self) {
        let Some(family) = &self.family else {
            return;
        };
        for event in family.take_log() {
            // Only durable events are kept, so none comes back.
            let _ = self.journal.stage(event);
        }
    }
}
