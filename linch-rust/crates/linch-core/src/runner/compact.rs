//! Compaction before a turn's primary call (see [`crate::compaction`]).
//!
//! A compacting turn adds two run writes and one session write:
//! 1. the summary delivery is staged and flushed with a held
//!    `CompactionPending` checkpoint before the auxiliary call;
//! 2. an accepted summary writes through: its receipt, `Usage`, the
//!    `compaction` event and a `Compacted` checkpoint holding the summary,
//!    so a paid summary is never lost or bought twice;
//! 3. the projection goes into the session log, after which the turn's
//!    primary call follows as usual.
//!
//! A summary call that fails, or answers with no text, is not fatal: the
//! turn goes on uncompacted. Its receipt and `Usage` are written at once,
//! with a `CompactionPending` checkpoint that counts them (one more run
//! write), because a paid response is durable as soon as it exists: resume
//! then goes on to the turn's primary call and never buys it again.

use crate::agent::RunError;
use crate::compaction::{self, estimate_tokens};
use crate::events::Event;
use crate::events::kinds::CompactionEvent;
use crate::events::model_call::ModelCallReceiptEvent;
use crate::message::Message;
use crate::provider::ProviderError;
use crate::provider::call::ModelCall;
use crate::run::Phase;

use super::turn::{Runner, Step};

/// The turn's primary call, first attempt.
const DISPATCH: Step = Step::Dispatch {
    attempt: 1,
    abandoned: None,
};

impl Runner {
    /// Compact the provider view if it passed the threshold, with summary
    /// delivery `attempt`; then go on to the turn's primary call.
    pub(super) async fn compact(
        &mut self,
        attempt: u32,
        abandoned: Option<ModelCallReceiptEvent>,
    ) -> Result<Option<Step>, RunError> {
        if let Some(receipt) = abandoned {
            self.settle(&receipt);
            self.journal.stage(Event::ModelCallReceipt(receipt));
        }
        if self.cancel.is_cancelled() {
            self.abort().await?;
            return Ok(None);
        }
        let agent = std::sync::Arc::clone(&self.agent);
        let Some(spec) = agent.spec.compaction.as_ref() else {
            return Ok(Some(DISPATCH));
        };
        if self.out_of_turns() {
            return Ok(Some(DISPATCH));
        }
        let view = self.log.provider_view();
        let Some(keep_from) = compaction::keep_from(&view, spec.keep_recent_turns) else {
            return Ok(Some(DISPATCH));
        };
        let tokens_before = estimate_tokens(&view);
        if tokens_before < spec.threshold_tokens {
            return Ok(Some(DISPATCH));
        }
        self.snapshot = agent.tools.snapshot();
        let older = view[..keep_from]
            .iter()
            .map(|m| Message::clone(m))
            .collect();
        let tools = self.snapshot.definitions();
        let request = compaction::summary_request(&agent.spec, spec, tools, older);
        let call =
            ModelCall::prepare_compaction(&self.run.run_id, self.turn, attempt, &request.model);
        self.journal
            .stage(Event::ModelCallDelivery(call.delivery().clone()));
        self.save(Phase::CompactionPending { attempt }).await?;
        let ready = self.journal.flush().await?;
        self.outbox.deliver(ready).await;
        // The summary is not the assistant's answer: no deltas.
        let outcome = (call.drive(&*agent.provider, request, &self.cancel, None)).await;
        self.settle(&outcome.receipt);
        let usage = outcome.receipt.usage;
        self.journal.stage(Event::ModelCallReceipt(outcome.receipt));
        // Paid whether or not it gave a summary.
        self.stage_usage(usage);
        let text = match outcome.result {
            Ok(assembly) => assembly.message.text(),
            Err(failure) if matches!(failure.error, ProviderError::Cancelled) => {
                self.abort().await?;
                return Ok(None);
            }
            Err(_) => String::new(),
        };
        let text = text.trim();
        if text.is_empty() {
            self.save(Phase::CompactionPending { attempt }).await?;
            let ready = self.journal.flush().await?;
            self.outbox.deliver(ready).await;
            return Ok(Some(DISPATCH));
        }
        let summary = compaction::summary_message(text);
        let compacted = compaction::replacement(&view, summary.clone(), keep_from);
        self.journal.stage(Event::Compaction(CompactionEvent {
            messages_before: view.len() as u64,
            messages_after: compacted.len() as u64,
            tokens_before,
            tokens_after: estimate_tokens(&compacted),
            strategy: compaction::STRATEGY.into(),
        }));
        let keep_from = keep_from as u64;
        self.save(Phase::Compacted {
            summary: summary.clone(),
            keep_from,
        })
        .await?;
        Ok(Some(Step::Compacted {
            summary,
            keep_from,
            appended: false,
        }))
    }

    /// Append the projection of a durable summary unless `appended`, then
    /// go on to the turn's primary call. The view is the one the summary
    /// was made from: the checkpoint's session length proves it unchanged.
    pub(super) async fn compacted(
        &mut self,
        summary: Message,
        keep_from: u64,
        appended: bool,
    ) -> Result<Option<Step>, RunError> {
        if !appended {
            let view = self.log.provider_view();
            let keep_from = usize::try_from(keep_from).unwrap_or(usize::MAX);
            self.project(compaction::replacement(&view, summary, keep_from))
                .await?;
        }
        Ok(Some(DISPATCH))
    }
}
