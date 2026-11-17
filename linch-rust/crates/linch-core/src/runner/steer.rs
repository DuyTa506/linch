//! Steers sent to a live run (see [`crate::Agent::steer`]).
//!
//! A steer is a `steer_received` journal event, written by the run that
//! holds the lease and flushed at once, before the caller is answered. The
//! run delivers it as a user message of its own at the next turn boundary
//! (after a whole tool batch's results, before the next provider call), and
//! the `alignment` user event that logs the message names the steer. What
//! waits is rebuilt from the journal: received minus delivered, as the loop
//! guard rebuilds its counts. A run that suspends drops its inbox, and the
//! caller writes the event itself under a claim (`Agent::steer`).

use std::collections::{HashMap, HashSet, VecDeque};
use std::sync::{Arc, Mutex, PoisonError};

use tokio::sync::{mpsc, oneshot};

use super::turn::{Runner, Step};
use crate::agent::{AgentInner, MAX_PENDING_STEERS, RunError};
use crate::events::kinds::{SteerReceivedEvent, UserEvent, UserEventSubtype};
use crate::events::{Envelope, Event};
use crate::hooks::{UserPromptOutcome, UserPromptRequest};
use crate::ids::RunId;
use crate::message::{ContentBlock, Message, Role};
use crate::run::Phase;

/// A steer on its way to the run that drives it.
pub(crate) struct Arrival {
    pub(crate) id: String,
    pub(crate) text: String,
    /// Answered once the steer is durable, or refused.
    pub(crate) reply: oneshot::Sender<Result<(), RunError>>,
}

/// The inbox of each run this process drives.
#[derive(Default)]
pub(crate) struct Steering(Mutex<HashMap<RunId, mpsc::UnboundedSender<Arrival>>>);

impl Steering {
    fn map(&self) -> std::sync::MutexGuard<'_, HashMap<RunId, mpsc::UnboundedSender<Arrival>>> {
        self.0.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// Hand `arrival` to the run `run_id` that this process drives; it comes
    /// back when there is none.
    pub(crate) fn send(&self, run_id: &RunId, arrival: Arrival) -> Result<(), Arrival> {
        let sender = self.map().get(run_id).cloned();
        match sender {
            Some(sender) => sender.send(arrival).map_err(|error| error.0),
            None => Err(arrival),
        }
    }
}

/// A driven run's registration: removed when the run stops being driven.
pub(super) struct Inbox {
    receiver: mpsc::UnboundedReceiver<Arrival>,
    sender: mpsc::UnboundedSender<Arrival>,
    agent: Arc<AgentInner>,
    run_id: RunId,
}

impl Drop for Inbox {
    fn drop(&mut self) {
        let mut map = self.agent.steering.map();
        if map
            .get(&self.run_id)
            .is_some_and(|s| s.same_channel(&self.sender))
        {
            map.remove(&self.run_id);
        }
    }
}

/// What a run knows of its steers.
#[derive(Default)]
pub(super) struct Steers {
    inbox: Option<Inbox>,
    /// Every id the journal holds, delivered or not.
    received: HashSet<String>,
    /// Received and not delivered, oldest first, as `(id, text)`.
    pending: VecDeque<(String, String)>,
}

/// The steers `events` hold: every id received, and those not delivered.
pub(crate) fn steers_of(events: &[Envelope]) -> (HashSet<String>, VecDeque<(String, String)>) {
    let mut received = HashSet::new();
    let mut pending: VecDeque<(String, String)> = VecDeque::new();
    for envelope in events {
        match &envelope.event {
            Event::SteerReceived(steer) => {
                if received.insert(steer.steer_id.clone()) {
                    pending.push_back((steer.steer_id.clone(), steer.text.clone()));
                }
            }
            Event::User(UserEvent {
                steer_id: Some(id), ..
            }) => pending.retain(|(pending, _)| pending != id),
            _ => {}
        }
    }
    (received, pending)
}

impl Runner {
    /// Take steers for this run: register its inbox, and read what its
    /// durable `events` already hold. Called once the run holds its lease.
    pub(super) fn attach_steering(&mut self, events: &[Envelope]) {
        let (sender, receiver) = mpsc::unbounded_channel();
        let run_id = self.run.run_id.clone();
        self.agent
            .steering
            .map()
            .insert(run_id.clone(), sender.clone());
        let agent = Arc::clone(&self.agent);
        let inbox = Inbox {
            receiver,
            sender,
            agent,
            run_id,
        };
        let (received, pending) = steers_of(events);
        self.steers = Steers {
            inbox: Some(inbox),
            received,
            pending,
        };
    }

    /// Write the steers that arrived since the last step: one
    /// `steer_received` each, flushed before any caller is answered. A
    /// resent id and a full queue are answered without a write.
    pub(super) async fn take_steers(&mut self) -> Result<(), RunError> {
        let mut waiting = Vec::new();
        let Some(inbox) = &mut self.steers.inbox else {
            return Ok(());
        };
        while let Ok(arrival) = inbox.receiver.try_recv() {
            waiting.push(arrival);
        }
        if waiting.is_empty() {
            return Ok(());
        }
        let mut replies = Vec::new();
        for Arrival { id, text, reply } in waiting {
            if self.steers.received.contains(&id) {
                let _ = reply.send(Ok(()));
            } else if self.steers.pending.len() >= MAX_PENDING_STEERS {
                let run_id = self.run.run_id.clone();
                let full = RunError::SteerQueueFull {
                    run_id,
                    max: MAX_PENDING_STEERS,
                };
                let _ = reply.send(Err(full));
            } else {
                let steer_id = id.clone();
                let event = SteerReceivedEvent {
                    steer_id: steer_id.clone(),
                    text: text.clone(),
                };
                self.journal.stage(Event::SteerReceived(event));
                self.steers.received.insert(steer_id);
                self.steers.pending.push_back((id, text));
                replies.push(reply);
            }
        }
        if !replies.is_empty() {
            // A failed write drops the replies: the callers learn the run
            // stopped, and a resent id is read back from what landed.
            let ready = self.journal.flush().await?;
            self.event_emit(&ready).await;
            self.outbox.deliver(ready).await;
        }
        for reply in replies {
            let _ = reply.send(Ok(()));
        }
        Ok(())
    }

    /// The next steer as a user message of its own: `alignment` event and
    /// `ToolResults` checkpoint saved, ready for [`Step::Results`] to append
    /// it. `None` when none waits. Each steer passes prompt redaction and the
    /// `user_prompt_submit` chain, as a prompt does, with source `steer`; one
    /// the chain blocks is dropped (its `hook` records are streamed).
    pub(super) async fn steered(&mut self) -> Result<Option<Step>, RunError> {
        self.take_steers().await?;
        while let Some((id, text)) = self.steers.pending.pop_front() {
            let Some(content) = self.screen(&text).await else {
                // The run cancel interrupted a hook: the run aborts next.
                self.steers.pending.push_front((id, text));
                return Ok(None);
            };
            let Some(content) = content else {
                continue;
            };
            let message = Message::new(Role::User, content);
            self.journal.stage(Event::User(UserEvent {
                message: message.clone(),
                subtype: UserEventSubtype::Alignment,
                steer_id: Some(id),
            }));
            self.save(Phase::ToolResults {
                message: message.clone(),
            })
            .await?;
            return Ok(Some(Step::Results {
                message,
                appended: false,
            }));
        }
        Ok(None)
    }

    /// `text` as the model will read it: redacted like a prompt, then
    /// through `user_prompt_submit`. `Some(None)` when a hook blocked it;
    /// `None` when the run cancel interrupted the chain.
    async fn screen(&self, text: &str) -> Option<Option<Vec<ContentBlock>>> {
        let redactor = self.agent.prompt_redactor();
        let text = match redactor {
            Some(redactor) => redactor.text(text).into_owned(),
            None => text.to_owned(),
        };
        let chain = &self.agent.hooks.user_prompt_submit;
        if chain.is_empty() {
            return Some(Some(vec![ContentBlock::text(text)]));
        }
        let request = UserPromptRequest {
            session_id: self.run.session_id.clone(),
            run_id: self.run.run_id.clone(),
            content: vec![ContentBlock::text(text)],
            source: Some("steer"),
        };
        let result = self.cancel.run_until_cancelled(chain.run(request)).await?;
        self.push_records(result.records, None);
        match result.outcome {
            UserPromptOutcome::Submit { content } => {
                let mut message = Message::new(Role::User, content);
                if let Some(redactor) = redactor {
                    redactor.message(&mut message);
                }
                Some(Some(message.content))
            }
            UserPromptOutcome::Blocked { .. } => Some(None),
        }
    }
}
