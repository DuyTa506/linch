//! [`AgentFactory`]: opens the agent of a job that carries a stored agent
//! definition, for one attempt, and closes it after.

use std::fmt;

use futures::future::BoxFuture;
use linch_core::Agent;

/// Opens the agent a job's stored definition describes.
///
/// A worker given a factory ([`crate::Worker::definitions`]) calls it once
/// per attempt at a job that names a definition, and closes the result
/// after the attempt settles, so nothing of the agent outlives the job (as
/// Python Linch's worker owns and closes the agent its factory returns).
/// The definition is the text stored with the job; what it holds (for
/// example a serialized agent config) is up to the factory. The agent must
/// store its runs through [`linch_postgres::PgRunStore`] in the worker
/// pool's database and schema, as every worker agent must.
pub trait AgentFactory: Send + Sync + 'static {
    /// Open the agent for a job of `namespace` with `definition`.
    ///
    /// Args:
    ///     namespace: The job's namespace; the agent must use it.
    ///     definition: The stored definition text.
    ///
    /// Returns:
    ///     The agent, with what to run when the attempt is over.
    ///
    /// Raises:
    ///     FactoryError: `Refused` fails the job for good, `Busy` gives it
    ///         back without counting a failure, `Unavailable` retries it.
    fn open<'a>(
        &'a self,
        namespace: &'a str,
        definition: &'a str,
    ) -> BoxFuture<'a, Result<JobAgent, FactoryError>>;
}

/// The agent of one attempt, and what closes it.
pub struct JobAgent {
    pub(crate) agent: Agent,
    pub(crate) close: Option<BoxFuture<'static, ()>>,
}

impl JobAgent {
    /// An agent that needs no closing.
    pub fn new(agent: Agent) -> Self {
        Self { agent, close: None }
    }

    /// Run `close` once the attempt has settled (for example: disconnect
    /// the agent's MCP servers, free a place it held).
    #[must_use]
    pub fn on_close(mut self, close: BoxFuture<'static, ()>) -> Self {
        self.close = Some(close);
        self
    }

    pub(crate) async fn close(self) {
        if let Some(close) = self.close {
            close.await;
        }
    }
}

impl fmt::Debug for JobAgent {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("JobAgent")
            .field("contract", &self.agent.contract())
            .finish_non_exhaustive()
    }
}

/// Why an [`AgentFactory`] opened no agent.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum FactoryError {
    /// The definition may never run (it does not decode, or what it asks
    /// for is not allowed): the job fails for good (its run, if it has one,
    /// stays as it is).
    #[error("agent definition refused: {0}")]
    Refused(String),
    /// Not now (for example, a cap on live runs is reached): the job is
    /// given back for later without counting a failure.
    #[error("agent busy: {0}")]
    Busy(String),
    /// Opening failed (a provider, store or MCP server is unreachable): the
    /// job is retried, counting a failure.
    #[error("agent unavailable: {0}")]
    Unavailable(String),
}
