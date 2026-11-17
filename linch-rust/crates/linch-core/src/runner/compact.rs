//! Compaction of the provider view (see [`crate::compaction`]): at a
//! turn's start, before the primary call, when the view is due; and out of
//! turn, forced, when the provider refused the primary call's prompt as
//! longer than its context window.
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
//! With skills on, the summary message also carries a `<system-reminder>`
//! with the body of each skill the model loaded in the session, since the
//! summary folds their tool results away. Likewise it carries one notice
//! that restates every catalog change of the session (see
//! [`crate::catalog`]); the system blocks and the tools of the request stay
//! byte-identical. The projection logs both, so they are in the provider
//! view and never in the full history.
//!
//! A summary call that fails, or answers with no text, is not fatal at a
//! turn's start: the turn goes on uncompacted. Its receipt and `Usage` are
//! written at once, with a `CompactionPending` checkpoint that counts them
//! (one more run write), because a paid response is durable as soon as it
//! exists: resume then goes on to the turn's primary call and never buys it
//! again. A forced summary that gives none ends the run failed instead: the
//! provider already refused the prompt it would leave.
//!
//! Micro-compaction ([`crate::CompactionSpec::micro`]) makes no call: the
//! `compaction` event rides a held `Cleared` checkpoint, which the session
//! write of its projection flushes first (one run write, one session
//! write).
//!
//! A forced compaction is never a provider retry: the next primary call is
//! the next delivery attempt of the turn, with no `provider_retry` event.
//! Whether a summary was forced, and how many the run began, is read back
//! from the durable deliveries on resume ([`crate::compaction::Forced`]).

use std::sync::Arc;
use std::time::Instant;

use crate::agent::RunError;
use crate::compaction::{self, estimate_tokens};
use crate::events::Event;
use crate::events::kinds::CompactionEvent;
use crate::events::model_call::{CallDisposition, ModelCallReceiptEvent};
use crate::message::{ContentBlock, Message};
use crate::provider::ProviderError;
use crate::provider::call::ModelCall;
use crate::run::Phase;
use crate::skills;

use super::gate::Slot;
use super::turn::{Runner, Step};

/// Delivery `attempt` of the turn's primary call.
fn dispatch(attempt: u32) -> Step {
    Step::Dispatch {
        attempt,
        abandoned: None,
    }
}

impl Runner {
    /// Compact the provider view with summary delivery `attempt`, then go
    /// on to the turn's primary call. At a turn's start (`redispatch`
    /// `None`) only a view that is due compacts, and the primary call's
    /// first attempt follows. A forced compaction (`redispatch` the primary
    /// call's next attempt) summarizes whether due or not, and ends the
    /// run when it cannot.
    pub(super) async fn compact(
        &mut self,
        attempt: u32,
        abandoned: Option<ModelCallReceiptEvent>,
        redispatch: Option<u32>,
    ) -> Result<Option<Step>, RunError> {
        if let Some(mut receipt) = abandoned {
            self.settle(&mut receipt);
            self.journal.stage(Event::ModelCallReceipt(receipt));
        }
        if self.cancel.is_cancelled() {
            self.abort().await?;
            return Ok(None);
        }
        let agent = Arc::clone(&self.agent);
        let next = dispatch(redispatch.unwrap_or(1));
        let Some(spec) = agent.spec.compaction.as_ref() else {
            return Ok(Some(next));
        };
        // The primary call ends the run then, with its terminal.
        if self.out_of_turns() || self.refusal().is_some() {
            return Ok(Some(next));
        }
        let forced = redispatch.is_some();
        let view = self.log.provider_view();
        let keep = spec.keep_recent_turns;
        let tokens_before = estimate_tokens(&view);
        // The trigger's measure (see `Anchor`); the size checks and the hold
        // stay on the estimate, both sides of each in one unit.
        let measured = self.anchor.measure(&view, tokens_before);
        let measured_tokens = (measured > tokens_before).then_some(measured);
        let keep_from = if forced {
            compaction::forced_keep_from(&view, keep)
        } else {
            let window = agent.context_window();
            let limit = agent.spec.max_output_tokens;
            if !compaction::due(spec, limit, window, measured) {
                return Ok(Some(next));
            }
            // The cheaper step first: enough when it brings the view back
            // under the trigger.
            if spec.micro
                && let Some((before, cleared)) = compaction::clear_tool_results(&view, keep)
                && !compaction::due(
                    spec,
                    limit,
                    window,
                    measured
                        .saturating_sub(tokens_before.saturating_sub(estimate_tokens(&cleared))),
                )
            {
                return self.clear(&view, before, cleared, 1, false).await.map(Some);
            }
            // Summaries rejected twice in a row are not asked for again
            // until the view has grown (see `Rejections`).
            if self.rejections.holds(tokens_before, window) {
                return Ok(Some(next));
            }
            compaction::keep_from(&view, keep)
        };
        let Some(keep_from) = keep_from else {
            if forced {
                self.unshortened().await?;
                return Ok(None);
            }
            return Ok(Some(next));
        };
        let strategy = compaction::STRATEGY;
        self.pre_compact(self.turn, view.len() as u64, tokens_before, strategy)
            .await;
        let started = Instant::now();
        self.snapshot = agent.snapshot();
        // With micro on, the summary reads the folded part with its old
        // tool results cleared, as Python's does after its micro step. At a
        // turn's start it reads the whole view, so its prefix is the last
        // primary call's (the kept turns are in it as well as after it); a
        // forced one reads only the folded part, since the view does not fit.
        let source = (spec.micro)
            .then(|| compaction::cleared_before(&view, keep_from))
            .flatten();
        let end = if forced { keep_from } else { view.len() };
        let older = (source.as_deref().unwrap_or(&view)[..end].iter())
            .map(|m| Message::clone(m))
            .collect();
        let mut request = compaction::summary_request(self.request(older), spec);
        if self.standing.fallbacks > 0 {
            // A run that failed over summarizes on the model it now calls.
            request.model = self.model().to_owned();
        }
        let slot = match self.slot().await? {
            Slot::Cancelled => {
                self.abort().await?;
                return Ok(None);
            }
            held => held,
        };
        let call =
            ModelCall::prepare_compaction(&self.run.run_id, self.turn, attempt, &request.model);
        self.summary_calls = self.summary_calls.max(attempt);
        self.journal
            .stage(Event::ModelCallDelivery(call.delivery().clone()));
        self.save(Phase::CompactionPending { attempt }).await?;
        let ready = self.journal.flush().await?;
        self.event_emit(&ready).await;
        self.outbox.deliver(ready).await;
        // The summary is not the assistant's answer: no deltas.
        let outcome = (call.drive(&*agent.provider, request, &self.cancel, None)).await;
        drop(slot);
        let mut receipt = outcome.receipt;
        self.settle(&mut receipt);
        let (usage, cost) = (receipt.usage, receipt.cost_usd);
        let served = receipt.actual_model.clone();
        // Paid whether or not it gave a summary, unless it failed before
        // costing anything.
        let free = (outcome.result.as_ref()).is_err_and(|failure| failure.free);
        let cancelled = (outcome.result.as_ref())
            .is_err_and(|failure| matches!(failure.error, ProviderError::Cancelled));
        // A reply that is not a summary has no text to use, or a tool call.
        let (text, called, answered) = match outcome.result {
            Ok(assembly) => {
                let called = (assembly.message.content.iter())
                    .any(|block| matches!(block, ContentBlock::ToolUse(_)));
                (assembly.message.text(), called, true)
            }
            Err(_) => (String::new(), false, false),
        };
        // A summary is model output the model reads back: redact it too.
        let text = match agent.response_redactor() {
            Some(redactor) => redactor.text(&text).into_owned(),
            None => text,
        };
        let text = text.trim();
        let mut tail = Vec::new();
        if agent.skills.is_some() {
            // The bodies the model loaded, which the summary folds away.
            let reminders = skills::reminders(&self.log.full_history());
            tail.extend(reminders.into_iter().map(ContentBlock::text));
        }
        if let Some(catalog) = &self.catalog {
            let restated = catalog.restatement(&agent.listed_skills());
            tail.extend(restated.map(ContentBlock::CatalogChange));
        }
        let folded = &view[1..keep_from];
        let summary = compaction::checkpoint(text, folded, spec.keep_user_tokens, tail);
        // A reply that called a tool, is empty, or leaves a view (checkpoint
        // with its header, verbatim user messages and reminders, and the
        // kept turns) whose estimate is no smaller than the view's is
        // rejected, by the numbers the event reports: the view stays as it
        // was, and the receipt and the `compaction` event say why.
        let compacted = compaction::replacement(&view, summary.clone(), keep_from);
        let tokens_after = estimate_tokens(&compacted);
        let rejection = answered
            .then(|| compaction::rejection(called, text, tokens_before, tokens_after))
            .flatten();
        if rejection.is_some() {
            receipt.disposition = CallDisposition::Rejected;
        }
        self.journal.stage(Event::ModelCallReceipt(receipt));
        if !free {
            self.stage_usage(usage, cost, served);
        }
        if cancelled {
            self.abort().await?;
            return Ok(None);
        }
        if rejection.is_some() || text.is_empty() {
            if let Some(reason) = rejection {
                let (messages, tokens) = (view.len() as u64, tokens_before);
                let holds = self.rejections.will_hold();
                self.rejections.rejected(tokens);
                self.stage_compaction(CompactionEvent {
                    messages_before: messages,
                    messages_after: messages,
                    tokens_before: tokens,
                    tokens_after: tokens,
                    strategy: strategy.into(),
                    duration_ms: super::end::millis(started.elapsed()),
                    forced,
                    rejected: Some(if forced {
                        format!("{reason}: the prompt still does not fit, so the run ends")
                    } else if holds {
                        let margin = compaction::Rejections::margin(agent.context_window());
                        format!(
                            "{reason}: asks again once the view has grown by {margin} tokens, \
                             or when the provider refuses the prompt"
                        )
                    } else {
                        format!("{reason}: the next due turn asks again")
                    }),
                    measured_tokens,
                });
            }
            if forced {
                // One terminal write: the receipt, usage, error and result.
                self.unshortened().await?;
                return Ok(None);
            }
            self.save(Phase::CompactionPending { attempt }).await?;
            let ready = self.journal.flush().await?;
            self.event_emit(&ready).await;
            self.outbox.deliver(ready).await;
            return Ok(Some(next));
        }
        // A view that is still due after the summary would ask for another
        // at the next turn: that is no help, and counts toward the hold.
        let window = agent.context_window();
        let limit = agent.spec.max_output_tokens;
        let (messages_before, messages_after) = (view.len() as u64, compacted.len() as u64);
        let event = CompactionEvent {
            messages_before,
            messages_after,
            tokens_before,
            tokens_after,
            strategy: strategy.into(),
            duration_ms: super::end::millis(started.elapsed()),
            forced,
            rejected: None,
            measured_tokens,
        };
        // Judged on the trigger's measure less what the summary removed, as
        // a resume reads it back from the event.
        let still_due = compaction::due(spec, limit, window, compaction::measure_after(&event));
        self.rejections.applied(tokens_after, still_due);
        self.stage_compaction(event);
        let keep_from = keep_from as u64;
        self.save(Phase::Compacted {
            summary: summary.clone(),
            keep_from,
        })
        .await?;
        self.post_compact(
            self.turn,
            messages_before,
            messages_after,
            tokens_before,
            tokens_after,
            strategy,
        )
        .await;
        Ok(Some(Step::Compacted {
            summary,
            keep_from,
            appended: false,
            attempt: redispatch.unwrap_or(1),
        }))
    }

    /// Append the projection of a durable summary unless `appended`, then
    /// go on to the turn's primary call `attempt`. The view is the one the
    /// summary was made from: the checkpoint's session length proves it
    /// unchanged.
    pub(super) async fn compacted(
        &mut self,
        summary: Message,
        keep_from: u64,
        appended: bool,
        attempt: u32,
    ) -> Result<Option<Step>, RunError> {
        if !appended {
            let view = self.log.provider_view();
            let keep_from = usize::try_from(keep_from).unwrap_or(usize::MAX);
            let replacement = compaction::replacement(&view, summary, keep_from);
            self.project(replacement, compaction::PROJECTION_REASON)
                .await?;
        }
        Ok(Some(dispatch(attempt)))
    }

    /// What follows primary call `attempt` of this turn, which the provider
    /// refused as longer than the model's context window: micro-compaction
    /// when the spec has it and it clears something, else a forced summary
    /// while the run has one left and the view has something to fold, then
    /// the call's next attempt. `None` when nothing can shorten the view:
    /// the refusal ends the run, as it does without compaction.
    pub(super) async fn overflow(&mut self, attempt: u32) -> Result<Option<Step>, RunError> {
        let agent = Arc::clone(&self.agent);
        let Some(spec) = agent.spec.compaction.as_ref() else {
            return Ok(None);
        };
        let view = self.log.provider_view();
        let next = attempt + 1;
        if spec.micro
            && let Some((before, cleared)) =
                compaction::clear_tool_results(&view, spec.keep_recent_turns)
        {
            return self
                .clear(&view, before, cleared, next, true)
                .await
                .map(Some);
        }
        if self.forced >= spec.max_forced
            || compaction::forced_keep_from(&view, spec.keep_recent_turns).is_none()
        {
            return Ok(None);
        }
        self.forced += 1;
        Ok(Some(Step::Compact {
            attempt: self.summary_calls + 1,
            abandoned: None,
            redispatch: Some(next),
        }))
    }

    /// Stage the `compaction` event, which also moves the anchor.
    fn stage_compaction(&mut self, event: CompactionEvent) {
        let event = Event::Compaction(event);
        self.anchor.observe(&event);
        self.journal.stage(event);
    }

    /// Micro-compaction: record `cleared`, the view with its tool results
    /// before `before` cleared, as a logged projection, then go on to
    /// primary call `attempt`. The `compaction` event rides the held
    /// `Cleared` checkpoint, which the session write flushes first.
    async fn clear(
        &mut self,
        view: &[Arc<Message>],
        before: usize,
        cleared: Vec<Arc<Message>>,
        attempt: u32,
        forced: bool,
    ) -> Result<Step, RunError> {
        let started = Instant::now();
        let strategy = compaction::MICRO_STRATEGY;
        let messages = view.len() as u64;
        let tokens_before = estimate_tokens(view);
        self.pre_compact(self.turn, messages, tokens_before, strategy)
            .await;
        let tokens_after = estimate_tokens(&cleared);
        let measured = self.anchor.measure(view, tokens_before);
        self.stage_compaction(CompactionEvent {
            messages_before: messages,
            messages_after: messages,
            tokens_before,
            tokens_after,
            strategy: strategy.into(),
            duration_ms: super::end::millis(started.elapsed()),
            forced,
            rejected: None,
            measured_tokens: (measured > tokens_before).then_some(measured),
        });
        let before = before as u64;
        self.save(Phase::Cleared { before, attempt }).await?;
        self.project(cleared, compaction::MICRO_REASON).await?;
        self.post_compact(
            self.turn,
            messages,
            messages,
            tokens_before,
            tokens_after,
            strategy,
        )
        .await;
        Ok(dispatch(attempt))
    }

    /// Append the projection of a durable micro-compaction unless
    /// `appended`, then go on to primary call `attempt`. The checkpoint's
    /// session length proves the view unchanged, so clearing it again gives
    /// the same projection.
    pub(super) async fn cleared(
        &mut self,
        before: u64,
        attempt: u32,
        appended: bool,
    ) -> Result<Option<Step>, RunError> {
        if !appended {
            let view = self.log.provider_view();
            let before = usize::try_from(before).unwrap_or(usize::MAX);
            if let Some(cleared) = compaction::cleared_before(&view, before) {
                self.project(cleared, compaction::MICRO_REASON).await?;
            }
        }
        Ok(Some(dispatch(attempt)))
    }
}
