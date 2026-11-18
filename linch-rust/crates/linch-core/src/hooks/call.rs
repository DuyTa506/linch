//! The `before_provider_call` point: a hook sees the request about to be
//! sent and may replace it or block the call.

use crate::ids::{RunId, SessionId};
use crate::policy::HookPoint;
use crate::provider::ProviderRequest;

use super::chain::{HookChain, HookRecord, Step};
use super::hook::{Point, sealed::Sealed};

/// Marker for [`HookPoint::BeforeProviderCall`].
#[derive(Debug, Clone, Copy)]
pub struct BeforeProviderCall;

/// The provider request for one turn, about to be sent.
#[derive(Debug, Clone, PartialEq)]
pub struct BeforeProviderCallRequest {
    /// Session making the call.
    pub session_id: SessionId,
    /// Run making the call.
    pub run_id: RunId,
    /// Turn the call belongs to.
    pub turn: u32,
    /// The request as it stands: built from the session, or the last
    /// rewrite.
    pub request: ProviderRequest,
}

/// A `before_provider_call` hook's answer.
#[derive(Debug, Clone, PartialEq)]
pub enum BeforeProviderCallResponse {
    /// No change.
    Continue,
    /// Use this request from here on; later hooks see it, and it is what
    /// gets sent and what the run's `context_build` event records.
    Rewrite {
        /// The new request.
        request: Box<ProviderRequest>,
    },
    /// Do not send the call; end the run with this reason.
    Block {
        /// Why the call was blocked.
        reason: String,
    },
}

/// What the `before_provider_call` chain decided.
#[derive(Debug, Clone, PartialEq)]
pub enum BeforeProviderCallOutcome {
    /// Send this request.
    Send {
        /// The request to send: the original, or the last rewrite.
        request: Box<ProviderRequest>,
    },
    /// A hook blocked the call, or failed closed.
    Blocked {
        /// Why the call was blocked.
        reason: String,
    },
}

impl Sealed for BeforeProviderCall {}

impl Point for BeforeProviderCall {
    const POINT: HookPoint = HookPoint::BeforeProviderCall;
    type Request = BeforeProviderCallRequest;
    type Response = BeforeProviderCallResponse;

    fn tool_name(_: &Self::Request) -> Option<&str> {
        None
    }

    fn verdict(response: &Self::Response) -> &'static str {
        match response {
            BeforeProviderCallResponse::Continue => "continue",
            BeforeProviderCallResponse::Rewrite { .. } => "rewrite",
            BeforeProviderCallResponse::Block { .. } => "block",
        }
    }
}

impl HookChain<BeforeProviderCall> {
    /// Run the hooks in order. A rewrite feeds the next hook; a block or a
    /// closed failure stops the chain.
    pub(crate) async fn run_into(
        &self,
        mut request: BeforeProviderCallRequest,
        records: &mut Vec<HookRecord>,
    ) -> BeforeProviderCallOutcome {
        for entry in self.entries().iter() {
            match entry.invoke(&request, records).await {
                Step::Skip | Step::Answer(BeforeProviderCallResponse::Continue) => {}
                Step::Answer(BeforeProviderCallResponse::Rewrite { request: next }) => {
                    request.request = *next;
                }
                Step::Answer(BeforeProviderCallResponse::Block { reason })
                | Step::Closed(reason) => {
                    return BeforeProviderCallOutcome::Blocked { reason };
                }
            }
        }
        BeforeProviderCallOutcome::Send {
            request: Box::new(request.request),
        }
    }
}
