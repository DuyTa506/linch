//! The runner's writes: checkpoints, session appends and usage accounting.

use std::sync::Arc;

use super::turn::Runner;
use crate::agent::RunError;
use crate::catalog::{self, Catalog};
use crate::events::Event;
use crate::events::kinds::UsageEvent;
use crate::events::model_call::ModelCallReceiptEvent;
use crate::journal::JournalError;
use crate::message::Message;
use crate::run::{Checkpoint, Phase};
use crate::session_log::{LogEntry, MessageEntry, SessionLog};
use crate::store::StoreError;
use crate::usage::{Usage, tally};

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
    /// compaction projection `replacement`, recorded for `reason`.
    pub(super) async fn project(
        &mut self,
        replacement: Vec<Arc<Message>>,
        reason: &str,
    ) -> Result<(), RunError> {
        let entry = LogEntry::Projection(crate::session_log::ProjectionEntry {
            replacement: replacement.clone(),
            reason: reason.into(),
        });
        self.write_session(vec![entry]).await?;
        self.log.record_projection(replacement, reason);
        self.agent.compacted(&self.run.session_id);
        Ok(())
    }

    /// Flush the held checkpoint, then append `entries` to the stored
    /// session in one write.
    async fn write_session(&mut self, entries: Vec<LogEntry>) -> Result<(), RunError> {
        let ready = self.journal.flush_checkpoint().await?;
        self.event_emit(&ready).await;
        self.outbox.deliver(ready).await;
        let (sessions, namespace) = (&self.agent.sessions, &self.agent.namespace);
        let session = &self.run.session_id;
        let appended = sessions.append(namespace, session, self.session_len, entries);
        match appended.await {
            Ok(len) => self.session_len = len,
            Err(error @ StoreError::Conflict { .. }) => return Err(self.fenced_or(error).await),
            Err(error) => return Err(error.into()),
        }
        Ok(())
    }

    /// The error of a session append that found the session longer than
    /// this run wrote it: the lost lease when another owner has claimed the
    /// run since (its entries moved the session, and this writer is fenced
    /// out), else `conflict` itself.
    async fn fenced_or(&self, conflict: StoreError) -> RunError {
        let lease = self.journal.lease();
        match self.agent.runs.load(&lease.namespace, &lease.run_id).await {
            Ok(record) if record.epoch != lease.epoch => {
                let (run_id, epoch) = (lease.run_id.clone(), lease.epoch);
                JournalError::Store(StoreError::LeaseLost { run_id, epoch }).into()
            }
            _ => conflict.into(),
        }
    }

    /// Save a checkpoint for `phase` and deliver what a write-through made
    /// durable.
    pub(super) async fn save(&mut self, phase: Phase) -> Result<(), RunError> {
        self.unsaved = None;
        let checkpoint = Checkpoint {
            turn: self.turn,
            usage: self.usage,
            usage_complete: self.usage_complete,
            usage_by_model: self.usage_by_model.clone(),
            cost_usd: self.cost,
            session_len: self.session_len,
            phase,
        };
        let ready = self.journal.save(checkpoint).await?;
        self.event_emit(&ready).await;
        self.outbox.deliver(ready).await;
        Ok(())
    }

    /// Make every staged event durable, for a caller that is not a save
    /// (the wait for the provider gate, a steer). A receipt settled since
    /// the last save is never durable without a checkpoint at the totals
    /// that count it: that phase is saved first, held, so the same write
    /// carries both.
    pub(super) async fn flush_staged(&mut self) -> Result<(), RunError> {
        if let Some(phase) = self.unsaved.take() {
            self.save(phase).await?;
        }
        let ready = self.journal.flush().await?;
        self.event_emit(&ready).await;
        self.outbox.deliver(ready).await;
        Ok(())
    }

    /// Count a settled receipt: known usage adds up, unknown usage makes
    /// the total incomplete (never a zero fill). A receipt with usage is
    /// priced first, by the model that served it (the run's model, the
    /// fallback it failed over to included, when the provider named none),
    /// and a known cost adds up too. The budget is charged with it either
    /// way.
    pub(super) fn settle(&mut self, receipt: &mut ModelCallReceiptEvent) {
        match receipt.usage {
            Some(usage) => {
                self.usage += usage;
                let model = receipt.actual_model.as_deref();
                let model = model.unwrap_or(self.model()).to_owned();
                tally(&mut self.usage_by_model, &model, usage);
                if receipt.cost_usd.is_none() {
                    receipt.cost_usd = self.agent.price(&model, &usage);
                }
                if let Some(cost) = receipt.cost_usd {
                    self.cost = Some(self.cost.unwrap_or(0.0) + cost);
                }
            }
            None => self.usage_complete = false,
        }
        if let Some(meter) = &self.budget {
            meter.charge(receipt);
        }
    }

    /// Stage the `Usage` event of a call that reported `usage`, with the
    /// call's `cost`, the run's so far, and the model that `served` it;
    /// then the budget's warning when this call reached its ratio.
    pub(super) fn stage_usage(
        &mut self,
        usage: Option<Usage>,
        cost: Option<f64>,
        served: Option<String>,
    ) {
        if let Some(usage) = usage {
            self.journal.stage(Event::Usage(UsageEvent {
                usage,
                cumulative: self.usage,
                cumulative_by_model: self.usage_by_model.clone(),
                cost_usd: cost,
                cumulative_cost_usd: self.cost,
                actual_model: served,
            }));
            // The first charge to reach the warning ratio, once per tree.
            if let Some(warning) = self.budget.as_ref().and_then(|meter| meter.take_warning()) {
                self.journal.stage(Event::Budget(warning));
            }
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
