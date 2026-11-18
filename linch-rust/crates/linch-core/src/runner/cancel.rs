//! Cancelling a run that no stream drives (see [`crate::Agent::cancel`]):
//! a run suspended for an approval or an answer, or one a cancel left
//! half done.
//!
//! The cancel claims the run like an approve, so it fences any earlier
//! holder, then ends it from its durable state. Its writes, after the
//! claim:
//!
//! 1. One commit: the `tool_call_start`/`tool_call_end` of each call of
//!    the last response that has no durable end (the aborted result, or the
//!    interrupted placeholder of one that started), the `user` event of the
//!    results, and the `Aborting` checkpoint that holds them. From here on
//!    every continuation of the run finishes the abort: a resume or another
//!    cancel appends the results and ends the run; an approve or an answer
//!    gets `NotSuspended`.
//! 2. The session append of the results, so the session stays paired for
//!    a later run.
//! 3. The terminal commit: the `Result` (subtype `aborted`) and status
//!    `aborted`.
//!
//! A run whose session is already paired (a cancel that won the claim
//! after the run went on) skips the first two.

use super::turn::{Runner, Step};
use crate::agent::RunError;
use crate::events::Event;
use crate::events::kinds::{ToolCallEndEvent, ToolCallStartEvent, UserEvent, UserEventSubtype};
use crate::journal::{INTERRUPTED_TOOL_CODE, Recovery, ToolRecovery};
use crate::message::{ContentBlock, Message, Role, ToolResultBlock};
use crate::run::Phase;
use crate::scheduler::ABORTED;

/// What a cancelled run still owes its session before it ends.
pub(super) enum Unpaired {
    /// Nothing: the session is paired.
    Nothing,
    /// The calls of the last response, appended without results.
    Calls(Vec<ToolRecovery>),
    /// A results message held in `ToolResults`, in the session if
    /// `appended`.
    Message { message: Message, appended: bool },
    /// The results an earlier cancel saved in `Aborting`.
    Saved { message: Message, appended: bool },
}

/// The step a cancel takes from `recovery`. `calls` reads the calls of a
/// suspended batch. Steps that stage an abandoned receipt first (a
/// dispatch, a summary) go as they are: the runner's cancelled token
/// ends them before any call.
pub(super) fn step(
    recovery: Recovery,
    calls: impl FnOnce() -> Result<Vec<ToolRecovery>, RunError>,
) -> Result<Step, RunError> {
    let unpaired = match recovery {
        Recovery::Redispatch { attempt, abandoned } => {
            return Ok(Step::Dispatch { attempt, abandoned });
        }
        Recovery::Recompact {
            attempt,
            abandoned,
            redispatch,
        } => {
            return Ok(Step::Compact {
                attempt,
                abandoned,
                redispatch,
            });
        }
        Recovery::Received {
            message,
            appended: true,
            ..
        } if message.tool_uses().next().is_some() => {
            let calls = message.tool_uses().cloned().map(ToolRecovery::Replan);
            Unpaired::Calls(calls.collect())
        }
        Recovery::ToolBatch { calls, .. } => Unpaired::Calls(calls),
        Recovery::AwaitingApproval { .. } | Recovery::AwaitingInput { .. } => {
            Unpaired::Calls(calls()?)
        }
        Recovery::ToolResults { message, appended } => Unpaired::Message { message, appended },
        // The answer was decided, but the cancel is too: the results go in
        // and the run ends `aborted`.
        Recovery::Finishing {
            message, appended, ..
        } => Unpaired::Message { message, appended },
        Recovery::Aborting { message, appended } => Unpaired::Saved { message, appended },
        // A prompt, a response without calls or a projection not yet in
        // the session is dropped; one that is in leaves it paired.
        Recovery::Started { .. }
        | Recovery::Received { .. }
        | Recovery::Compacted { .. }
        | Recovery::Cleared { .. }
        | Recovery::Unshortened => Unpaired::Nothing,
    };
    Ok(Step::Abort(unpaired))
}

impl Runner {
    /// Pair the session as `unpaired` says, then end the run `aborted`.
    pub(super) async fn cancelled(&mut self, unpaired: Unpaired) -> Result<(), RunError> {
        let owed = match unpaired {
            Unpaired::Nothing => None,
            Unpaired::Calls(calls) => {
                let message = self.abort_calls(calls);
                self.save_aborting(&message).await?;
                Some(message)
            }
            Unpaired::Message { message, appended } => {
                if appended {
                    None
                } else {
                    self.save_aborting(&message).await?;
                    Some(message)
                }
            }
            Unpaired::Saved { message, appended } => (!appended).then_some(message),
        };
        if let Some(message) = owed {
            self.append(message).await?;
        }
        self.abort().await
    }

    async fn save_aborting(&mut self, message: &Message) -> Result<(), RunError> {
        let message = message.clone();
        self.save(Phase::Aborting { message }).await
    }

    /// Stage the end of each call without a durable one, and the `user`
    /// event of the results; return the results message.
    fn abort_calls(&mut self, calls: Vec<ToolRecovery>) -> Message {
        let mut results = Vec::with_capacity(calls.len());
        for call in calls {
            let (tool_name, result, error_code) = match call {
                ToolRecovery::Done(result) => {
                    results.push(ContentBlock::ToolResult(result));
                    continue;
                }
                ToolRecovery::Interrupted { tool_name, result } => {
                    (tool_name, result, Some(INTERRUPTED_TOOL_CODE.to_owned()))
                }
                ToolRecovery::Replan(block) => {
                    let summary = match self.snapshot.resolve(&block.name) {
                        Some(tool) => tool.tool().summarize(&block.input),
                        None => block.name.clone(),
                    };
                    self.journal.stage(Event::ToolCallStart(ToolCallStartEvent {
                        tool_use_id: block.id.clone(),
                        tool_name: block.name.clone(),
                        input: block.input,
                        summary,
                    }));
                    let result = ToolResultBlock::text(&block.id, ABORTED, true);
                    (block.name, result, None)
                }
            };
            self.journal.stage(Event::ToolCallEnd(ToolCallEndEvent {
                tool_name,
                result: result.clone(),
                duration_ms: 0,
                canonical_output: None,
                metadata: None,
                error_code,
                cache_hit: false,
            }));
            results.push(ContentBlock::ToolResult(result));
        }
        let message = Message::new(Role::User, results);
        self.journal.stage(Event::User(UserEvent {
            message: message.clone(),
            subtype: UserEventSubtype::ToolResult,
            steer_id: None,
        }));
        message
    }
}
