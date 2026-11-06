//! The runner's writes: checkpoints, session appends and usage accounting.

use std::sync::Arc;

use super::turn::Runner;
use crate::agent::RunError;
use crate::events::Event;
use crate::events::kinds::UsageEvent;
use crate::events::model_call::ModelCallReceiptEvent;
use crate::message::Message;
use crate::run::{Checkpoint, Phase};
use crate::session_log::{LogEntry, MessageEntry};
use crate::usage::Usage;

impl Runner {
    /// Session write: flush the held checkpoint, then append `message`.
    pub(super) async fn append(&mut self, message: Message) -> Result<(), RunError> {
        let entry = LogEntry::Message(MessageEntry {
            message: Arc::new(message.clone()),
            visible: true,
            historical: true,
        });
        self.write_session(entry).await?;
        self.log.append(message, true, true);
        Ok(())
    }

    /// Session write: flush the held checkpoint, then append the
    /// compaction projection `replacement`.
    pub(super) async fn project(&mut self, replacement: Vec<Arc<Message>>) -> Result<(), RunError> {
        let reason = crate::compaction::PROJECTION_REASON;
        let entry = LogEntry::Projection(crate::session_log::ProjectionEntry {
            replacement: replacement.clone(),
            reason: reason.into(),
        });
        self.write_session(entry).await?;
        self.log.record_projection(replacement, reason);
        Ok(())
    }

    /// Flush the held checkpoint, then append `entry` to the stored session.
    async fn write_session(&mut self, entry: LogEntry) -> Result<(), RunError> {
        let ready = self.journal.flush_checkpoint().await?;
        self.outbox.deliver(ready).await;
        let (sessions, namespace) = (&self.agent.sessions, &self.agent.namespace);
        let session = &self.run.session_id;
        self.session_len =
            (sessions.append(namespace, session, self.session_len, vec![entry])).await?;
        Ok(())
    }

    /// Save a checkpoint for `phase` and deliver what a write-through made
    /// durable.
    pub(super) async fn save(&mut self, phase: Phase) -> Result<(), RunError> {
        let checkpoint = Checkpoint {
            turn: self.turn,
            usage: self.usage,
            usage_complete: self.usage_complete,
            session_len: self.session_len,
            phase,
        };
        let ready = self.journal.save(checkpoint).await?;
        self.outbox.deliver(ready).await;
        Ok(())
    }

    /// Count a settled receipt: known usage adds up, unknown usage makes
    /// the total incomplete (never a zero fill).
    pub(super) fn settle(&mut self, receipt: &ModelCallReceiptEvent) {
        match receipt.usage {
            Some(usage) => self.usage += usage,
            None => self.usage_complete = false,
        }
    }

    /// Stage the `Usage` event of a call that reported `usage`.
    pub(super) fn stage_usage(&mut self, usage: Option<Usage>) {
        if let Some(usage) = usage {
            let cumulative = self.usage;
            self.journal
                .stage(Event::Usage(UsageEvent { usage, cumulative }));
        }
    }
}
