//! Human in the loop: an interrupt parks the workflow until someone
//! answers it.

use linch_core::events::kinds::WorkflowKind;
use linch_core::{JsonObject, JsonValue};
use serde::Serialize;
use serde::de::DeserializeOwned;

use super::context::{WorkflowContext, decode, encode_error, event, lock};
use super::error::WorkflowError;
use super::journal::{Interrupt, interrupt_key};

impl WorkflowContext {
    /// Park the workflow until someone answers `key`.
    ///
    /// The first time through there is no answer: an `interrupt_requested`
    /// event carries `payload`, and the call returns
    /// [`WorkflowError::Suspended`]; [`run_workflow`](super::run_workflow)
    /// then stores the workflow as awaiting input, with the interrupt, so
    /// any process can read the question. Calling `run_workflow` again
    /// with the answer in
    /// [`WorkflowOptions::resume`](super::WorkflowOptions::resume) makes
    /// this call return it; the answer is journaled, so later resumes
    /// replay it without a `resume` entry.
    ///
    /// An answer is used once: a second interrupt with the same key in one
    /// run suspends again, so interrupts in a loop need distinct keys.
    ///
    /// Args:
    ///     key: The interrupt's durable identity and the answer's key.
    ///     payload: Context for whoever answers; it does not affect replay.
    ///
    /// Returns:
    ///     The answer, decoded from its journaled JSON.
    ///
    /// Raises:
    ///     WorkflowError: `Suspended` without an answer, `Aborted` when
    ///         the cancel token fired, `Encode` when the payload does not
    ///         encode or the answer does not decode, `Store` when the
    ///         journal write fails.
    pub async fn interrupt<A: DeserializeOwned>(
        &self,
        key: &str,
        payload: impl Serialize,
    ) -> Result<A, WorkflowError> {
        let label = format!("interrupt {key}");
        let (call_key, occurrence, cached) = self.next_occurrence(&interrupt_key(key));
        let event = |kind, result_text: Option<&JsonValue>| {
            let mut event = event(kind, key.to_owned(), call_key.clone(), occurrence);
            event.result_text = result_text.map(JsonValue::to_string);
            event
        };
        if let Some(answer) = cached {
            self.emit(event(WorkflowKind::InterruptReplayed, Some(&answer)));
            return decode(&label, answer);
        }
        self.check_cancel(&label)?;
        let answer = lock(&self.inner.resume).remove(key);
        if let Some(answer) = answer {
            let decoded = decode(&label, answer.clone())?;
            self.record(&call_key, occurrence, &answer).await?;
            self.emit(event(WorkflowKind::InterruptResolved, Some(&answer)));
            return Ok(decoded);
        }
        let payload = serde_json::to_value(payload).map_err(|error| {
            encode_error(&label, format!("its payload does not encode: {error}"))
        })?;
        let mut requested = event(WorkflowKind::InterruptRequested, None);
        let mut structured = JsonObject::new();
        structured.insert("payload".into(), payload.clone());
        requested.structured_output = Some(structured);
        self.emit(requested);
        let interrupt = Interrupt {
            key: key.to_owned(),
            payload,
        };
        lock(&self.inner.suspended).get_or_insert_with(|| interrupt.clone());
        Err(WorkflowError::Suspended(interrupt))
    }
}
