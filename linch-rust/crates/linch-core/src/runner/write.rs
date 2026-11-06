//! The runner's writes: checkpoints, session appends and usage accounting.

use std::sync::Arc;

use super::turn::Runner;
use crate::agent::RunError;
use crate::catalog::{self, Catalog};
use crate::events::Event;
use crate::events::kinds::UsageEvent;
use crate::events::model_call::ModelCallReceiptEvent;
use crate::message::Message;
use crate::run::{Checkpoint, Phase};
use crate::session_log::{LogEntry, MessageEntry, SessionLog};
use crate::store::StoreError;
use crate::usage::Usage;

impl Runner {
    /// Session write: flush the held checkpoint, then append `message`.
    pub(super) async fn append(&mut self, message: Message) -> Result<(), RunError> {
        self.write_session(vec![entry(&message)]).await?;
        if let Some(catalog) = &mut self.catalog {
            catalog.apply(&message);
        }
        self.log.append(message, true, true);
        Ok(())
    }

    /// Session write: append the run's `prompt`, after the session's
    /// frozen prefix when the session has none yet (its first prompt).
    async fn append_opening(&mut self, prompt: Message) -> Result<(), RunError> {
        if self.catalog.is_some() {
            return self.append(prompt).await;
        }
        let version = catalog::version(&self.agent);
        let prefix = catalog::prefix(&self.agent);
        let entries = vec![LogEntry::Prefix(prefix.clone()), entry(&prompt)];
        self.write_session(entries).await?;
        self.log.record_prefix(prefix);
        self.log.append(prompt, true, true);
        self.catalog = Catalog::of(&self.log);
        self.seen = Some(version);
        Ok(())
    }

    /// Session write: append the run's prompt at the session's end.
    ///
    /// The store admits one live run per session, so once this run exists
    /// no other run appends. The session may still have grown between this
    /// run's load and its create, by a run that ended in between: the first
    /// append then conflicts, and the run reads the session again, saves
    /// `Started` at its length and appends there. `moved`: resume found the
    /// session grown already. Either way the prompt lands exactly at the
    /// checkpoint's `session_len`, so a crash after it reconciles. The
    /// session's first prompt carries the frozen prefix in the same write.
    pub(super) async fn append_prompt(
        &mut self,
        prompt: Message,
        moved: bool,
    ) -> Result<(), RunError> {
        if moved {
            self.save_started(&prompt).await?;
        }
        match self.append_opening(prompt.clone()).await {
            Err(RunError::Store(StoreError::Conflict { .. })) => {}
            done => return done,
        }
        let (sessions, namespace) = (&self.agent.sessions, &self.agent.namespace);
        let entries = sessions.load(namespace, &self.run.session_id).await?;
        self.session_len = entries.len() as u64;
        self.log = session_log(entries);
        self.catalog = Catalog::of(&self.log);
        self.save_started(&prompt).await?;
        self.append_opening(prompt).await
    }

    async fn save_started(&mut self, prompt: &Message) -> Result<(), RunError> {
        let prompt = prompt.clone();
        self.save(Phase::Started { prompt }).await
    }

    /// Session write: flush the held checkpoint, then append the
    /// compaction projection `replacement`.
    pub(super) async fn project(&mut self, replacement: Vec<Arc<Message>>) -> Result<(), RunError> {
        let reason = crate::compaction::PROJECTION_REASON;
        let entry = LogEntry::Projection(crate::session_log::ProjectionEntry {
            replacement: replacement.clone(),
            reason: reason.into(),
        });
        self.write_session(vec![entry]).await?;
        self.log.record_projection(replacement, reason);
        Ok(())
    }

    /// Flush the held checkpoint, then append `entries` to the stored
    /// session in one write.
    async fn write_session(&mut self, entries: Vec<LogEntry>) -> Result<(), RunError> {
        let ready = self.journal.flush_checkpoint().await?;
        self.outbox.deliver(ready).await;
        let (sessions, namespace) = (&self.agent.sessions, &self.agent.namespace);
        let session = &self.run.session_id;
        self.session_len = (sessions.append(namespace, session, self.session_len, entries)).await?;
        Ok(())
    }

    /// Save a checkpoint for `phase` and deliver what a write-through made
    /// durable.
    pub(super) async fn save(&mut self, phase: Phase) -> Result<(), RunError> {
        let checkpoint = Checkpoint {
            turn: self.turn,
            usage: self.usage,
            usage_complete: self.usage_complete,
            cost_usd: self.cost,
            session_len: self.session_len,
            phase,
        };
        let ready = self.journal.save(checkpoint).await?;
        self.outbox.deliver(ready).await;
        Ok(())
    }

    /// Count a settled receipt: known usage adds up, unknown usage makes
    /// the total incomplete (never a zero fill). A receipt with usage is
    /// priced first, by the model that served it (the agent's model when
    /// the provider named none), and a known cost adds up too.
    pub(super) fn settle(&mut self, receipt: &mut ModelCallReceiptEvent) {
        let Some(usage) = receipt.usage else {
            self.usage_complete = false;
            return;
        };
        self.usage += usage;
        if receipt.cost_usd.is_none() {
            let model = receipt.actual_model.as_deref();
            let model = model.unwrap_or(&self.agent.spec.model);
            receipt.cost_usd = self.agent.price(model, &usage);
        }
        if let Some(cost) = receipt.cost_usd {
            self.cost = Some(self.cost.unwrap_or(0.0) + cost);
        }
    }

    /// Stage the `Usage` event of a call that reported `usage`, with the
    /// call's `cost` and the run's so far.
    pub(super) fn stage_usage(&mut self, usage: Option<Usage>, cost: Option<f64>) {
        if let Some(usage) = usage {
            self.journal.stage(Event::Usage(UsageEvent {
                usage,
                cumulative: self.usage,
                cost_usd: cost,
                cumulative_cost_usd: self.cost,
            }));
        }
    }
}

/// The log entry of `message`, in both projections.
fn entry(message: &Message) -> LogEntry {
    LogEntry::Message(MessageEntry {
        message: Arc::new(message.clone()),
        visible: true,
        historical: true,
    })
}

/// The in-memory log of the stored session `entries`.
pub(crate) fn session_log(entries: Vec<LogEntry>) -> SessionLog {
    let mut log = SessionLog::new();
    for entry in entries {
        match entry {
            LogEntry::Message(entry) => {
                let message = Arc::unwrap_or_clone(entry.message);
                log.append(message, entry.visible, entry.historical);
            }
            LogEntry::Projection(entry) => {
                log.record_projection(entry.replacement, entry.reason);
            }
            LogEntry::Prefix(prefix) => log.record_prefix(prefix),
        }
    }
    log
}
