//! Peer messages between agents: a [`Mailbox`], the in-process
//! [`InMemoryMailbox`], and the request/response [`Correlator`].
//!
//! The core subagent model is parent to child: a worker runs and returns a
//! conclusion. A mailbox adds peers that address each other across turns.
//! An agent built with [`crate::AgentBuilder::mailbox`] gets the
//! [`SEND_MESSAGE_TOOL`], and each of its runs drains its session's inbox
//! at the run's start and at the end of each tool batch: every waiting
//! message goes into the logged prompt or tool-results message as a
//! `<peer-message>` text block, after what is already there, so the model
//! sees it in its next request and no run write is added. A drain leases
//! the messages: the run acks them once the event that carries them is
//! durable, and releases them when it ends before that (a prompt that
//! cannot be created, a run that fails first).
//!
//! A mailbox address is a session id: `send_message` sends from the calling
//! session, to the session id the model names. What a message means is the
//! host's choreography, never core policy.
//!
//! Delivery is at least once. A process that dies between drain and ack
//! leaves the lease to run out in a durable mailbox (linch-postgres'
//! `PgMailbox`), whose next drain returns the message again; the
//! in-process mailbox loses everything on exit. Every backend passes
//! [`conformance::mailbox_suite`].

pub mod conformance;
mod correlation;
mod mailbox;
mod send;

pub use correlation::Correlator;
pub use mailbox::{InMemoryMailbox, Mailbox, MailboxError, MailboxMessage};
pub use send::SEND_MESSAGE_TOOL;
pub(crate) use send::SendMessageTool;
