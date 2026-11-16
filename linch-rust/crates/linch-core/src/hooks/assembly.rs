//! The `after_provider_call` point: a hook sees the assembled response a
//! provider call just produced and may replace it or block the turn before
//! it is staged and appended to the session.

use crate::ids::{RunId, SessionId};
use crate::policy::HookPoint;
use crate::provider::AssistantAssembly;

use super::chain::{HookChain, HookRecord, Step};
use super::hook::{Point, sealed::Sealed};

/// Marker for [`HookPoint::AfterProviderCall`].
#[derive(Debug, Clone, Copy)]
pub struct AfterProviderCall;

/// The response a provider call assembled for one turn.
#[derive(Debug, Clone, PartialEq)]
pub struct AfterProviderCallRequest {
    /// Session the call belongs to.
    pub session_id: SessionId,
    /// Run the call belongs to.
    pub run_id: RunId,
    /// Turn the call belongs to.
    pub turn: u32,
    /// The assembled response as it stands: the provider's, or the last
    /// rewrite.
    pub assembly: AssistantAssembly,
}

/// An `after_provider_call` hook's answer.
#[derive(Debug, Clone, PartialEq)]
pub enum AfterProviderCallResponse {
    /// No change.
    Continue,
    /// Use this response from here on; later hooks see it, and it is what
    /// gets staged and appended to the session.
    Rewrite {
        /// The new response.
        assembly: Box<AssistantAssembly>,
    },
    /// Do not accept the response; end the run with this reason.
    Block {
        /// Why the response was blocked.
        reason: String,
    },
}

/// What the `after_provider_call` chain decided.
#[derive(Debug, Clone, PartialEq)]
pub enum AfterProviderCallOutcome {
    /// Accept this response.
    Send {
        /// The response to accept: the original, or the last rewrite.
        assembly: Box<AssistantAssembly>,
    },
    /// A hook blocked the response, or failed closed.
    Blocked {
        /// Why the response was blocked.
        reason: String,
    },
}

impl Sealed for AfterProviderCall {}

impl Point for AfterProviderCall {
    const POINT: HookPoint = HookPoint::AfterProviderCall;
    type Request = AfterProviderCallRequest;
    type Response = AfterProviderCallResponse;

    fn tool_name(_: &Self::Request) -> Option<&str> {
        None
    }

    fn verdict(response: &Self::Response) -> &'static str {
        match response {
            AfterProviderCallResponse::Continue => "continue",
            AfterProviderCallResponse::Rewrite { .. } => "rewrite",
            AfterProviderCallResponse::Block { .. } => "block",
        }
    }
}

impl HookChain<AfterProviderCall> {
    /// Run the hooks in order. A rewrite feeds the next hook; a block or a
    /// closed failure stops the chain.
    pub(crate) async fn run_into(
        &self,
        mut request: AfterProviderCallRequest,
        records: &mut Vec<HookRecord>,
    ) -> AfterProviderCallOutcome {
        for entry in self.entries().iter() {
            match entry.invoke(&request, records).await {
                Step::Skip | Step::Answer(AfterProviderCallResponse::Continue) => {}
                Step::Answer(AfterProviderCallResponse::Rewrite { assembly: next }) => {
                    request.assembly = *next;
                }
                Step::Answer(AfterProviderCallResponse::Block { reason })
                | Step::Closed(reason) => {
                    return AfterProviderCallOutcome::Blocked { reason };
                }
            }
        }
        AfterProviderCallOutcome::Send {
            assembly: Box::new(request.assembly),
        }
    }
}
