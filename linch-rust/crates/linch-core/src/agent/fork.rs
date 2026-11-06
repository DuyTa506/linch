//! Forking a session: a new session that starts from a copy of a durable
//! prefix of another one's log.

use std::collections::BTreeSet;

use thiserror::Error;

use super::Agent;
use crate::ids::SessionId;
use crate::message::{ContentBlock, Message, Role};
use crate::runner;
use crate::store::StoreError;

/// [`Agent::fork_session`] refused or failed.
#[derive(Debug, Error)]
pub enum ForkError {
    /// The destination is the source.
    #[error("fork destination must differ from the source session")]
    SameSession,
    /// The destination session already exists; nothing was written.
    #[error("fork destination session already exists: {0}")]
    Exists(SessionId),
    /// `before` is not the index of a logged entry.
    #[error("before {before} does not identify a logged entry (the log has {len})")]
    Boundary {
        /// The index asked for.
        before: u64,
        /// How many entries the source log has.
        len: u64,
    },
    /// The prefix would end inside a tool exchange: the provider view would
    /// hold tool calls without their results. Holds the ids of the calls,
    /// comma-separated.
    #[error("unsafe fork boundary leaves assistant tool calls unanswered: {0}")]
    Unpaired(String),
    /// A store read or write failed, or the source does not exist
    /// ([`StoreError::NotFound`]).
    #[error(transparent)]
    Store(#[from] StoreError),
}

impl Agent {
    /// Create session `dest` from a copy of the durable log of `source`,
    /// up to the entry at index `before` (exclusive), or all of it.
    ///
    /// The copy holds the logged messages and compaction projections, so
    /// the next run on `dest` sees what a run on `source` would have seen at
    /// that point, skills loaded so far included. Runs, checkpoints,
    /// workers and pending peer messages stay with the source. The source
    /// is only read; read it at a fixed point by passing `before`, since a
    /// live run on it may append meanwhile.
    ///
    /// Args:
    ///     source: The session to copy from.
    ///     dest: The new session; it must not exist yet.
    ///     before: The index of the first entry left out; it must name a
    ///         logged entry. `None` copies the whole log.
    ///
    /// Returns:
    ///     How many entries were copied: the fork point in the source log.
    ///
    /// Raises:
    ///     ForkError: `SameSession`, `Exists`, `Boundary` or `Unpaired`
    ///         (nothing written) when the fork is refused; `Store` when the
    ///         source does not exist or a store call fails. A failed copy
    ///         after the create leaves `dest` empty, and a run started on
    ///         `dest` meanwhile makes the copy fail with a conflict.
    pub async fn fork_session(
        &self,
        source: &SessionId,
        dest: &SessionId,
        before: Option<u64>,
    ) -> Result<u64, ForkError> {
        if source == dest {
            return Err(ForkError::SameSession);
        }
        let (namespace, sessions) = (&self.inner.namespace, &self.inner.sessions);
        let mut entries = sessions.load(namespace, source).await?;
        let len = entries.len() as u64;
        if let Some(before) = before {
            if before >= len {
                return Err(ForkError::Boundary { before, len });
            }
            entries.truncate(before as usize);
        }
        unanswered(&runner::session_log(entries.clone()).provider_view())?;
        match sessions.create(namespace, dest).await {
            Err(StoreError::Conflict { .. }) => return Err(ForkError::Exists(dest.clone())),
            result => result?,
        }
        let copied = entries.len() as u64;
        if !entries.is_empty() {
            sessions.append(namespace, dest, 0, entries).await?;
        }
        Ok(copied)
    }
}

/// Refuse a view that ends with tool calls whose results it lacks.
fn unanswered(view: &[std::sync::Arc<Message>]) -> Result<(), ForkError> {
    let Some(last) = view.last() else {
        return Ok(());
    };
    let uses = |message: &Message| -> BTreeSet<String> {
        let blocks = message.content.iter();
        (blocks.filter_map(|block| match block {
            ContentBlock::ToolUse(call) => Some(call.id.clone()),
            _ => None,
        }))
        .collect()
    };
    let pending = match last.role {
        Role::Assistant => uses(last),
        Role::User => return Ok(()),
    };
    match pending.is_empty() {
        true => Ok(()),
        false => Err(ForkError::Unpaired(
            pending.into_iter().collect::<Vec<_>>().join(", "),
        )),
    }
}
