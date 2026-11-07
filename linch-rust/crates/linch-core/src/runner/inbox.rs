//! The run's inbox: what waits for the session goes into the next logged
//! user message, after what is already there (see [`crate::coordination`]).
//! The drained messages are acked once the event that carries them is
//! durable, and released when the run ends before that.

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

/// Ack drained `messages` whose delivery is durable. Best effort: a
/// failed ack leaves their lease to run out, so they come again (delivery
/// is at least once).
pub(super) async fn ack(agent: &AgentInner, messages: &[MailboxMessage]) {
    if let (Some(mailbox), false) = (&agent.mailbox, messages.is_empty()) {
        let _ = mailbox.ack(messages).await;
    }
}

/// Give back drained `messages` a run could not log. Best effort: a
/// failed release leaves their lease to run out, and the caller reports
/// its own error.
pub(super) async fn release(agent: &AgentInner, messages: &[MailboxMessage]) {
    if let (Some(mailbox), false) = (&agent.mailbox, messages.is_empty()) {
        let _ = mailbox.release(messages).await;
    }
}

/// Peer messages a run logged in an event that is not durable yet.
pub(super) struct HeldPeers {
    /// Seq of the event that carries them.
    seq: u64,
    messages: Vec<MailboxMessage>,
}

impl Runner {
    /// The blocks to add to the user message this turn ends with: the
    /// notification of each background child that ended (none for one
    /// `TaskStop` stopped), whose `child_run_status` this stages, then the
    /// session's peer messages, which are returned too for
    /// [`Self::hold_peers`].
    pub(super) async fn notes(
        &mut self,
    ) -> Result<(Vec<ContentBlock>, Vec<MailboxMessage>), RunError> {
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
        Ok((notes, peers))
    }

    /// Hold `peers`, which the event staged last carries, until it is
    /// durable (see [`Self::settle_peers`]).
    pub(super) fn hold_peers(&mut self, peers: Vec<MailboxMessage>) {
        if peers.is_empty() {
            return;
        }
        let seq = self.journal.last_seq();
        match &mut self.peers {
            Some(held) => {
                held.seq = seq;
                held.messages.extend(peers);
            }
            None => {
                self.peers = Some(HeldPeers {
                    seq,
                    messages: peers,
                });
            }
        }
    }

    /// Ack the held peer messages once their event is durable; when the
    /// run stops (`ending`) before that, release them.
    pub(super) async fn settle_peers(&mut self, ending: bool) {
        let durable = match &self.peers {
            None => return,
            Some(held) => held.seq <= self.journal.durable_seq(),
        };
        if !durable && !ending {
            return;
        }
        let Some(held) = self.peers.take() else {
            return;
        };
        if durable {
            ack(&self.agent, &held.messages).await;
        } else {
            release(&self.agent, &held.messages).await;
        }
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
        let (mut notes, mut peers) = (Vec::new(), Vec::new());
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
            (notes, peers) = self.notes().await?;
        }
        notes.extend(self.catalog_change());
        let message = Message::new(Role::User, notes);
        self.journal.stage(Event::User(UserEvent {
            message: message.clone(),
            subtype: UserEventSubtype::Notification,
        }));
        self.hold_peers(peers);
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
