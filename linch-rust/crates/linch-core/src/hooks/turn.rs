//! The turn points: `user_prompt_submit`, `context_build`, `stop`,
//! `post_turn`.

use std::sync::Arc;

use crate::ids::{RunId, SessionId};
use crate::message::{ContentBlock, Message, TextBlock};
use crate::policy::HookPoint;
use crate::run::StopReason;

use super::chain::{ChainResult, HookChain, Step};
use super::hook::{Point, sealed::Sealed};

/// Marker for [`HookPoint::UserPromptSubmit`].
#[derive(Debug, Clone, Copy)]
pub struct UserPromptSubmit;

/// A user prompt about to enter the session.
#[derive(Debug, Clone, PartialEq)]
pub struct UserPromptRequest {
    /// Session the prompt is for.
    pub session_id: SessionId,
    /// Run the prompt starts.
    pub run_id: RunId,
    /// Prompt content as it stands: the user's, or the last rewrite.
    pub content: Vec<ContentBlock>,
}

/// A `user_prompt_submit` hook's answer.
#[derive(Debug, Clone, PartialEq)]
pub enum UserPromptResponse {
    /// No change.
    Continue,
    /// Use this content from here on; later hooks see it.
    Rewrite {
        /// The new prompt content.
        content: Vec<ContentBlock>,
    },
    /// Reject the prompt. Stops the chain.
    Block {
        /// Why the prompt was rejected, for the host.
        reason: String,
    },
}

/// What the `user_prompt_submit` chain decided.
#[derive(Debug, Clone, PartialEq)]
pub enum UserPromptOutcome {
    /// Append this content as the user message.
    Submit {
        /// Final prompt content.
        content: Vec<ContentBlock>,
    },
    /// A hook rejected the prompt, or failed closed.
    Blocked {
        /// Why the prompt was rejected.
        reason: String,
    },
}

impl Sealed for UserPromptSubmit {}

impl Point for UserPromptSubmit {
    const POINT: HookPoint = HookPoint::UserPromptSubmit;
    type Request = UserPromptRequest;
    type Response = UserPromptResponse;

    fn tool_name(_: &Self::Request) -> Option<&str> {
        None
    }

    fn verdict(response: &Self::Response) -> &'static str {
        match response {
            UserPromptResponse::Continue => "continue",
            UserPromptResponse::Rewrite { .. } => "rewrite",
            UserPromptResponse::Block { .. } => "block",
        }
    }
}

impl HookChain<UserPromptSubmit> {
    /// Run the hooks in order. A rewrite feeds the next hook; a block or a
    /// closed failure stops the chain.
    pub async fn run(&self, mut request: UserPromptRequest) -> ChainResult<UserPromptOutcome> {
        let mut records = Vec::new();
        for entry in self.entries().iter() {
            match entry.invoke(&request, &mut records).await {
                Step::Skip | Step::Answer(UserPromptResponse::Continue) => {}
                Step::Answer(UserPromptResponse::Rewrite { content }) => request.content = content,
                Step::Answer(UserPromptResponse::Block { reason }) | Step::Closed(reason) => {
                    return ChainResult {
                        outcome: UserPromptOutcome::Blocked { reason },
                        records,
                    };
                }
            }
        }
        ChainResult {
            outcome: UserPromptOutcome::Submit {
                content: request.content,
            },
            records,
        }
    }
}

/// Marker for [`HookPoint::ContextBuild`].
#[derive(Debug, Clone, Copy)]
pub struct ContextBuild;

/// The provider request for one turn is being assembled.
#[derive(Debug, Clone, PartialEq)]
pub struct ContextBuildRequest {
    /// Session being served.
    pub session_id: SessionId,
    /// Run making the request.
    pub run_id: RunId,
    /// The session's provider view at this point.
    pub messages: Arc<Vec<Arc<Message>>>,
}

/// A `context_build` hook's answer: context to add to this request only.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct ContextBuildResponse {
    /// Blocks to add; empty adds nothing.
    pub blocks: Vec<TextBlock>,
}

impl Sealed for ContextBuild {}

impl Point for ContextBuild {
    const POINT: HookPoint = HookPoint::ContextBuild;
    type Request = ContextBuildRequest;
    type Response = ContextBuildResponse;

    fn tool_name(_: &Self::Request) -> Option<&str> {
        None
    }

    fn verdict(_: &Self::Response) -> &'static str {
        "add_context"
    }
}

impl HookChain<ContextBuild> {
    /// Run every hook in order and return their blocks in that order. A
    /// failing hook adds nothing; this point cannot fail closed.
    pub async fn run(&self, request: &ContextBuildRequest) -> ChainResult<Vec<TextBlock>> {
        let mut records = Vec::new();
        let mut blocks = Vec::new();
        for entry in self.entries().iter() {
            // `Closed` cannot occur: registration rejects a closed fail mode
            // here.
            if let Step::Answer(response) = entry.invoke(request, &mut records).await {
                blocks.extend(response.blocks);
            }
        }
        ChainResult {
            outcome: blocks,
            records,
        }
    }
}

/// Marker for [`HookPoint::Stop`].
#[derive(Debug, Clone, Copy)]
pub struct Stop;

/// The model gave a text-only response, so the run may stop.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StopRequest {
    /// Session being served.
    pub session_id: SessionId,
    /// Run that may stop.
    pub run_id: RunId,
    /// The final response text.
    pub text: String,
}

/// A `stop` hook's answer.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum StopResponse {
    /// Let the run stop.
    Allow,
    /// Keep the run going. Stops the chain; the remaining hooks see the
    /// next stop candidate instead.
    Continue {
        /// Shown to the model as the reason to keep going.
        feedback: String,
    },
}

/// What the `stop` chain decided.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum StopOutcome {
    /// Every hook allowed the stop.
    Stop,
    /// A hook kept the run going, or failed closed.
    Continue {
        /// Shown to the model.
        feedback: String,
    },
}

impl Sealed for Stop {}

impl Point for Stop {
    const POINT: HookPoint = HookPoint::Stop;
    type Request = StopRequest;
    type Response = StopResponse;

    fn tool_name(_: &Self::Request) -> Option<&str> {
        None
    }

    fn verdict(response: &Self::Response) -> &'static str {
        match response {
            StopResponse::Allow => "allow",
            StopResponse::Continue { .. } => "continue",
        }
    }
}

impl HookChain<Stop> {
    /// Run the hooks in order. The first hook that keeps the run going
    /// stops the chain. A closed failure blocks the stop the same way, so
    /// the answer is not accepted unchecked; the turn limit bounds the
    /// extra turns.
    pub async fn run(&self, request: &StopRequest) -> ChainResult<StopOutcome> {
        let mut records = Vec::new();
        for entry in self.entries().iter() {
            match entry.invoke(request, &mut records).await {
                Step::Skip | Step::Answer(StopResponse::Allow) => {}
                Step::Answer(StopResponse::Continue { feedback }) | Step::Closed(feedback) => {
                    return ChainResult {
                        outcome: StopOutcome::Continue { feedback },
                        records,
                    };
                }
            }
        }
        ChainResult {
            outcome: StopOutcome::Stop,
            records,
        }
    }
}

/// Marker for [`HookPoint::PostTurn`].
#[derive(Debug, Clone, Copy)]
pub struct PostTurn;

/// A turn finished. Hooks here observe only.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PostTurnRequest {
    /// Session being served.
    pub session_id: SessionId,
    /// Run the turn belongs to.
    pub run_id: RunId,
    /// Zero-based turn index within the run.
    pub turn: u32,
    /// Why the provider ended the turn's response.
    pub stop_reason: StopReason,
}

impl Sealed for PostTurn {}

impl Point for PostTurn {
    const POINT: HookPoint = HookPoint::PostTurn;
    type Request = PostTurnRequest;
    type Response = ();

    fn tool_name(_: &Self::Request) -> Option<&str> {
        None
    }

    fn verdict(_: &Self::Response) -> &'static str {
        "observe"
    }
}

impl HookChain<PostTurn> {
    /// Run every hook in order. This point cannot fail closed; failures are
    /// only recorded.
    pub async fn run(&self, request: &PostTurnRequest) -> ChainResult<()> {
        let mut records = Vec::new();
        for entry in self.entries().iter() {
            // `Closed` cannot occur: registration rejects a closed fail mode
            // here.
            let _ = entry.invoke(request, &mut records).await;
        }
        ChainResult {
            outcome: (),
            records,
        }
    }
}
