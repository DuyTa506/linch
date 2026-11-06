//! Peer messages between agents: a [`Mailbox`], the in-process
//! [`InMemoryMailbox`], and the request/response [`Correlator`].
//!
//! The core subagent model is parent to child: a worker runs and returns a
//! conclusion. A mailbox adds peers that address each other across turns.
//! A mailbox address is a session id. What a message means is the host's
//! choreography, never core policy.

mod correlation;
mod mailbox;

pub use correlation::Correlator;
pub use mailbox::{InMemoryMailbox, Mailbox, MailboxError, MailboxMessage};
