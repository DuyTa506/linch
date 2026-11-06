//! The run's inbox: what waits for the session goes into the next logged
//! user message, after what is already there (see [`crate::coordination`]).

use super::turn::Runner;
use crate::agent::{AgentInner, RunError};
use crate::coordination::MailboxMessage;
use crate::ids::SessionId;
use crate::message::ContentBlock;
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
    /// session's peer messages.
    pub(super) async fn notes(&mut self) -> Result<Vec<ContentBlock>, RunError> {
        let peers = drain(&self.agent, &self.run.session_id).await?;
        Ok(blocks(&peers).collect())
    }
}
