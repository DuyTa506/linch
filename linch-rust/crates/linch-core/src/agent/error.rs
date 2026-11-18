//! Typed errors of the agent API.

use thiserror::Error;

use crate::canonical::{CanonicalError, Digest};
use crate::ids::RunId;
use crate::journal::{JournalError, ReconcileError};
use crate::run::RunStatus;
use crate::store::StoreError;

/// [`super::AgentBuilder::build`] could not build an agent.
#[derive(Debug, Error)]
pub enum BuildError {
    /// A required binding was not set.
    #[error("agent builder is missing its {0}")]
    Missing(&'static str),
    /// The spec or a binding is unusable as given.
    #[error("invalid agent: {0}")]
    Invalid(String),
    /// The tool definitions could not be encoded for the contract digest.
    #[error("agent contract could not be digested: {0}")]
    Contract(#[from] CanonicalError),
}

/// A run cannot continue durably.
///
/// A provider error is not a `RunError`: it ends the run with status
/// `failed` and a `Result` event. Every `RunError` but
/// [`RunError::PromptBlocked`], [`RunError::InvalidOutputSchema`] and
/// [`RunError::InvalidToolChoice`] leaves the run at its last durable state, so
/// [`super::Agent::resume`] can pick it up again unless the run is
/// terminal.
#[derive(Debug, Error)]
pub enum RunError {
    /// A `user_prompt_submit` hook blocked the prompt, or failed closed,
    /// before the run was created. Nothing was written: the run does not
    /// exist, so resume and events report it not found.
    #[error("run {run_id} was not started: the prompt was blocked: {reason}")]
    PromptBlocked {
        /// The run id the stream announced.
        run_id: RunId,
        /// Why the prompt was blocked.
        reason: String,
    },
    /// A store read or write failed, including a lost lease
    /// ([`StoreError::LeaseLost`]), a session append conflict, and a new
    /// run on a session that already has a live run
    /// ([`StoreError::SessionBusy`], nothing written).
    #[error(transparent)]
    Store(#[from] StoreError),
    /// A journal write failed; the journal is poisoned for this attempt.
    #[error(transparent)]
    Journal(#[from] JournalError),
    /// The run was created by an agent with a different contract.
    #[error("run {run_id} has contract {found}, this agent has {expected}")]
    ContractMismatch {
        /// The run.
        run_id: RunId,
        /// This agent's contract digest.
        expected: Digest,
        /// The contract digest stored with the run.
        found: Digest,
    },
    /// Resume or approve on a run that already finished.
    #[error("run {run_id} is {status:?} and cannot continue")]
    Terminal {
        /// The run.
        run_id: RunId,
        /// Its terminal status.
        status: RunStatus,
    },
    /// Approve on a run that is not waiting for an approval, or answer on
    /// one not waiting for input, such as a retry whose answers were
    /// already committed: the host continues that run with a resume.
    #[error(
        "run {run_id} is not awaiting this approval or answer; if an approve or answer \
         already committed, continue the run with resume"
    )]
    NotSuspended {
        /// The run.
        run_id: RunId,
    },
    /// An answer that does not fit the run's pending input requests: no
    /// answers, a request that is not pending, or an answer that does not
    /// fit its questions. Nothing was claimed or written; the run is still
    /// awaiting input.
    #[error("run {run_id} cannot take this answer: {reason}")]
    InvalidAnswer {
        /// The run.
        run_id: RunId,
        /// What is wrong with the answer.
        reason: String,
    },
    /// The run's own output schema ([`super::RunOptions::output_schema`])
    /// does not compile, or its root is not an object. Nothing was
    /// written: the run does not exist.
    #[error("run {run_id} was not started: its output schema is unusable: {reason}")]
    InvalidOutputSchema {
        /// The run id the stream announced.
        run_id: RunId,
        /// What is wrong with the schema.
        reason: String,
    },
    /// The run's own tool choice ([`super::RunOptions::tool_choice`])
    /// names a tool the run does not offer, or its provider refuses it.
    /// Nothing was written: the run does not exist.
    #[error("run {run_id} was not started: its tool_choice is unusable: {reason}")]
    InvalidToolChoice {
        /// The run id the stream announced.
        run_id: RunId,
        /// What is wrong with the choice.
        reason: String,
    },
    /// A steer that cannot be queued: empty, or longer than
    /// [`super::MAX_STEER_BYTES`], or an id that is not usable. Nothing was
    /// written.
    #[error("run {run_id} cannot take this steer: {reason}")]
    InvalidSteer {
        /// The run.
        run_id: RunId,
        /// What is wrong with the steer.
        reason: String,
    },
    /// The run already holds [`super::MAX_PENDING_STEERS`] undelivered
    /// steers. Nothing was written; try again once the run has taken them.
    #[error("run {run_id} already holds {max} undelivered steers")]
    SteerQueueFull {
        /// The run.
        run_id: RunId,
        /// The cap.
        max: usize,
    },
    /// The durable checkpoint, events and session log do not fit together.
    #[error(transparent)]
    Reconcile(#[from] ReconcileError),
}
