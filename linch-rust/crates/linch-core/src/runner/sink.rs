//! [`JournalSink`]: the scheduler's [`ToolSink`] backed by the run journal.
//!
//! A tool that really runs costs two barriers: its start is staged and
//! flushed before it runs, its end is staged and flushed after. A call that
//! runs nothing (served, refused, or aborted before it started) gets its
//! start and end staged without a flush; they reach the store with the next
//! barrier, which keeps a tool turn at `3 + 2k` run writes.
//!
//! An event a running tool puts in the run's log (a subagent's wrapped
//! event) waits in an inbox, which the next start or end stages ahead of
//! itself: it rides that barrier, at the latest the call's own end. A
//! stream-only one goes to the caller at once, like progress.

use std::collections::HashSet;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Mutex, MutexGuard, PoisonError};

use async_trait::async_trait;

use super::Outbox;
use super::hooks::push_records;
use crate::events::kinds::{ToolCallEndEvent, ToolCallStartEvent, ToolProgressEvent};
use crate::events::{Envelope, Event, Persistence, WIRE_VERSION, now_ms};
use crate::ids::RunId;
use crate::journal::{Journal, JournalError};
use crate::scheduler::{CallOutcome, PlannedCall, ToolSink};
use crate::tool::ToolProgress;

/// Makes each tool start and end durable and hands it to the caller.
pub(super) struct JournalSink<'a> {
    /// Held across each flush: concurrent calls of a read group take turns.
    journal: tokio::sync::Mutex<&'a mut Journal>,
    outbox: &'a Outbox,
    run_id: RunId,
    /// Last durable seq, for progress envelopes (sync, so no journal lock).
    durable: AtomicU64,
    /// Calls whose tool started, so `tool_finished` knows which ones ran.
    started: Mutex<HashSet<String>>,
    /// Durable events tools reported, in order, not staged yet (sync, so a
    /// report never waits on a flush).
    inbox: Mutex<Vec<Event>>,
}

/// The guarded value even if a holder panicked; nothing panics while
/// holding these locks.
fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(PoisonError::into_inner)
}

impl<'a> JournalSink<'a> {
    pub(super) fn new(journal: &'a mut Journal, outbox: &'a Outbox, run_id: RunId) -> Self {
        let durable = AtomicU64::new(journal.durable_seq());
        Self {
            journal: tokio::sync::Mutex::new(journal),
            outbox,
            run_id,
            durable,
            started: Mutex::new(HashSet::new()),
            inbox: Mutex::new(Vec::new()),
        }
    }

    /// Stage the inbox, then `events`, then flush when `barrier` is set.
    async fn write(&self, events: Vec<Event>, barrier: bool) -> Result<(), JournalError> {
        let ready = {
            let mut journal = self.journal.lock().await;
            let reported = std::mem::take(&mut *lock(&self.inbox));
            for event in reported.into_iter().chain(events) {
                journal.stage(event);
            }
            if !barrier {
                return Ok(());
            }
            let ready = journal.flush().await?;
            self.durable.store(journal.durable_seq(), Ordering::SeqCst);
            ready
        };
        self.outbox.deliver(ready).await;
        Ok(())
    }

    /// A stream-only envelope at the last durable seq.
    fn now(&self, event: Event) -> Envelope {
        Envelope {
            v: WIRE_VERSION,
            seq: self.durable.load(Ordering::SeqCst),
            run_id: self.run_id.clone(),
            ts_ms: now_ms(),
            ignorable: false,
            event,
        }
    }
}

fn start_event(call: &PlannedCall) -> Event {
    Event::ToolCallStart(ToolCallStartEvent {
        tool_use_id: call.tool_use_id.clone(),
        tool_name: call.tool_name.clone(),
        input: call.input.clone(),
        summary: call.summary.clone(),
    })
}

#[async_trait]
impl ToolSink for JournalSink<'_> {
    type Error = JournalError;

    async fn tool_starting(&self, call: &PlannedCall) -> Result<(), JournalError> {
        lock(&self.started).insert(call.tool_use_id.clone());
        self.write(vec![start_event(call)], true).await
    }

    async fn tool_finished(
        &self,
        call: &PlannedCall,
        outcome: &CallOutcome,
    ) -> Result<(), JournalError> {
        let ran = lock(&self.started).remove(&call.tool_use_id);
        let seq = self.durable.load(Ordering::SeqCst);
        let records = outcome.hook_records.clone();
        push_records(
            self.outbox,
            &self.run_id,
            seq,
            records,
            Some(&call.tool_use_id),
        );
        let end = Event::ToolCallEnd(ToolCallEndEvent {
            tool_name: call.tool_name.clone(),
            result: outcome.block.clone(),
            duration_ms: u64::try_from(outcome.duration.as_millis()).unwrap_or(u64::MAX),
            canonical_output: None,
            metadata: match &outcome.result {
                Ok(output) => output.metadata.clone(),
                Err(_) => Default::default(),
            },
        });
        let events = if ran {
            vec![end]
        } else {
            vec![start_event(call), end]
        };
        self.write(events, ran).await
    }

    fn tool_event(&self, _call: &PlannedCall, event: Event) {
        match event.persistence() {
            Persistence::StreamOnly => self.outbox.push(self.now(event)),
            Persistence::Both | Persistence::JournalOnly => lock(&self.inbox).push(event),
        }
    }

    fn tool_progress(&self, call: &PlannedCall, progress: ToolProgress) {
        self.outbox
            .push(self.now(Event::ToolProgress(ToolProgressEvent {
                tool_use_id: call.tool_use_id.clone(),
                tool_name: call.tool_name.clone(),
                message: progress.message,
                data: progress.data,
            })));
    }
}
