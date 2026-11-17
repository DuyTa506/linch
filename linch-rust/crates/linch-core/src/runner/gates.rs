//! The runner's side of the reliability policy (see [`crate::gates`]): the
//! calls a batch refuses for a missing prerequisite, and the gate a final
//! answer meets before the run completes.
//!
//! A refused call is decided at plan time, before its hooks and before
//! permission, from what earlier batches did; its `policy` event is staged
//! with the batch's `ToolBatch` write, and only when the log does not hold
//! it already (a crash re-plans the batch). A final answer is checked
//! after any background subagent was heard back from and before the
//! schema, the verifiers and the `stop` chain, on both completing routes
//! (text, and a final tool call). A failure stages a `policy` event and
//! goes back to the model through the same held step as a verifier's
//! feedback, so the event, the notification and the checkpoint commit
//! together; once the corrections are spent the stop is one terminal
//! write. The corrections spent are counted from the durable `policy`
//! events.

use std::collections::{HashMap, HashSet};

use serde_json::json;

use super::structured::Checked;
use super::turn::Runner;
use crate::agent::RunError;
use crate::events::Event;
use crate::events::kinds::{ErrorEvent, PolicyEvent, ResultSubtype};
use crate::gates::Refusal;
use crate::message::ToolUseBlock;
use crate::run::{RunStatus, StopReason};

use super::end::Answer;

impl Runner {
    /// Stage `event` and count it for the loop guard and the policy.
    pub(super) fn stage_policy(&mut self, event: PolicyEvent) {
        self.stage_seen(Event::Policy(event));
    }

    /// What each call of `blocks` that may not run yet is refused with,
    /// by `tool_use_id`. Empty without prerequisites.
    pub(super) fn refusals(&self, blocks: &[ToolUseBlock]) -> HashMap<String, Refusal> {
        let Some(gates) = &self.gates else {
            return HashMap::new();
        };
        // The tools this turn offers: a skill's `allowed_tools`, the child's
        // filter and tool search's deferral are already out of the snapshot.
        let offered = |name: &str| self.snapshot.resolve(name).is_some();
        let refused = blocks
            .iter()
            .filter_map(|block| Some((block.id.clone(), gates.refusal(&block.name, &offered)?)));
        refused.collect()
    }

    /// Stage the `policy` event of each of `blocks` the plan refused as
    /// `refusals` says, unless the log holds it already.
    pub(super) fn stage_refusals(
        &mut self,
        blocks: &[ToolUseBlock],
        refusals: &HashMap<String, Refusal>,
        gated: &HashSet<String>,
    ) {
        let Some(gates) = &self.gates else {
            return;
        };
        let events: Vec<PolicyEvent> = blocks
            .iter()
            .filter(|block| gated.contains(&block.id))
            .filter_map(|block| {
                let refusal = refusals.get(&block.id)?;
                gates.refused_event(&block.name, &block.id, refusal)
            })
            .collect();
        for event in events {
            self.stage_policy(event);
        }
    }

    /// Check the final answer `text` against the completion gates.
    /// `None`: it meets them, or the policy has none; otherwise the answer
    /// went back to the model, or the run ended failed.
    pub(super) async fn gate_answer(&mut self, text: &str) -> Result<Option<Checked>, RunError> {
        let Some(gates) = &self.gates else {
            return Ok(None);
        };
        let offered = |name: &str| self.snapshot.resolve(name).is_some();
        let Some(failure) = gates.check(text, &offered) else {
            return Ok(None);
        };
        if gates.can_correct() {
            let event = gates.correction(&failure);
            self.stage_policy(event);
            let step = self.send_back(&failure.feedback).await?;
            return Ok(Some(Checked::Retry(step)));
        }
        let event = gates.stop(&failure);
        self.end_gate(event, text).await.map(Some)
    }

    /// Check `answer`, which the `stop` chain made of `text`, against the
    /// gates again when it is not `text`. A failure ends the run: the hook
    /// that rewrote it would rewrite the next answer too.
    pub(super) async fn gate_rewrite(
        &mut self,
        text: &str,
        answer: &str,
    ) -> Result<Option<Checked>, RunError> {
        let Some(gates) = &self.gates else {
            return Ok(None);
        };
        if answer == text {
            return Ok(None);
        }
        let offered = |name: &str| self.snapshot.resolve(name).is_some();
        let Some(failure) = gates.check(answer, &offered) else {
            return Ok(None);
        };
        let event = gates.rewritten(&failure);
        self.end_gate(event, answer).await.map(Some)
    }

    /// End the run failed on the `stop` `event`, with `text` as its answer.
    async fn end_gate(&mut self, event: PolicyEvent, text: &str) -> Result<Checked, RunError> {
        let message = event.detail.clone();
        self.stage_policy(event);
        let error = json!({"name": "PolicyGateError", "message": message, "retryable": false});
        let error = error.as_object().cloned().unwrap_or_default();
        self.journal.stage(Event::Error(ErrorEvent { error }));
        let answer = Answer {
            text: (!text.is_empty()).then(|| text.to_owned()),
            ..Answer::default()
        };
        let (status, subtype) = (RunStatus::Failed, ResultSubtype::Error);
        self.end_with(status, subtype, StopReason::Error, answer)
            .await?;
        Ok(Checked::Ended)
    }
}
