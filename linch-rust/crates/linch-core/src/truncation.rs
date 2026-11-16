//! Truncation recovery (Python's `TruncationRecovery`): a text answer the
//! output limit cut off is not the final answer while attempts remain.
//!
//! The cut response is logged as it is, and the spec's feedback goes back
//! to the model as a `<system-reminder>` user message (a `notification`
//! event), so it continues where it stopped. The run's final answer is
//! every cut part, in order, then the answer that finished: the schema
//! check, the verifiers, `stop` and the `Result` see that whole text; the
//! session keeps each part as the message it was. Off unless the spec sets
//! it, and without it a cut answer is final, as before. Linch never raises
//! the output limit on its own: that is a cost the host decides.
//!
//! Nothing new is checkpointed. The attempts spent and the parts so far
//! are read back from the run's durable `assistant` and `notification`
//! events ([`Truncated::of`]), so a resume neither resets the bound nor
//! loses a part.

use serde::{Deserialize, Serialize};

use crate::events::kinds::UserEventSubtype;
use crate::events::{Envelope, Event};
use crate::message::{ContentBlock, Message};
use crate::run::StopReason;
use crate::skills::system_reminder;

/// The feedback when the spec sets none, as in Python.
pub const DEFAULT_FEEDBACK: &str = "Your previous response was cut off because it reached the \
     output token limit. Continue exactly where you left off \u{2014} do not repeat earlier text.";

fn default_max_attempts() -> u32 {
    1
}

/// How a run continues a text answer the output limit cut off. Off unless
/// an [`AgentSpec`](crate::AgentSpec) sets one.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TruncationRecovery {
    /// Most cut answers one run continues (default 1, at least 1); past
    /// it a cut answer is final. Each is a turn, so `max_turns` bounds
    /// them too.
    #[serde(default = "default_max_attempts")]
    pub max_attempts: u32,
    /// What the model is told to continue; `None` uses
    /// [`DEFAULT_FEEDBACK`].
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub feedback: Option<String>,
}

impl Default for TruncationRecovery {
    fn default() -> Self {
        Self {
            max_attempts: default_max_attempts(),
            feedback: None,
        }
    }
}

impl TruncationRecovery {
    /// Why this spec cannot run, if it cannot.
    pub(crate) fn problem(&self) -> Option<&'static str> {
        if self.max_attempts == 0 {
            return Some("truncation_recovery.max_attempts must be at least 1");
        }
        if self.feedback.as_ref().is_some_and(|f| f.trim().is_empty()) {
            return Some("truncation_recovery.feedback must not be blank");
        }
        None
    }

    /// The feedback, wrapped as Python's `_gate_retry_tail` wraps it.
    pub(crate) fn reminder(&self) -> String {
        system_reminder(self.feedback.as_deref().unwrap_or(DEFAULT_FEEDBACK))
    }
}

/// Whether a response with `text` that stopped for `stop_reason` and calls
/// no tool is a cut answer recovery may continue.
pub(crate) fn cut(stop_reason: StopReason, text: &str) -> bool {
    stop_reason == StopReason::MaxTokens && !text.is_empty()
}

/// What a run spent on truncation recovery, read from its durable events.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub(crate) struct Truncated {
    /// Cut answers the run continued.
    pub(crate) attempts: u32,
    /// The cut parts that come before the latest assistant response.
    pub(crate) before_latest: String,
    /// The cut parts the next response continues.
    pub(crate) chain: String,
}

impl Truncated {
    /// Where a run with these durable `events` stands, for the feedback
    /// `reminder`. A cut answer counts once its feedback is durable; any
    /// other response ends the chain of parts.
    pub(crate) fn of(events: &[Envelope], reminder: &str) -> Self {
        let mut state = Self::default();
        let mut pending: Option<String> = None;
        for envelope in events {
            match &envelope.event {
                Event::Assistant(assistant) => {
                    if pending.take().is_some() {
                        state.chain.clear();
                    }
                    state.before_latest = state.chain.clone();
                    let message = &assistant.message;
                    let text = message.text();
                    if message.tool_uses().next().is_none() && cut(assistant.stop_reason, &text) {
                        pending = Some(text);
                    } else {
                        state.chain.clear();
                    }
                }
                Event::User(user)
                    if user.subtype == UserEventSubtype::Notification
                        && is_feedback(&user.message, reminder) =>
                {
                    if let Some(part) = pending.take() {
                        state.chain.push_str(&part);
                        state.attempts += 1;
                    }
                }
                _ => {}
            }
        }
        state
    }
}

/// Whether `message` is the truncation feedback `reminder` (a catalog
/// notice may ride after it).
fn is_feedback(message: &Message, reminder: &str) -> bool {
    matches!(message.content.first(), Some(ContentBlock::Text(text)) if text.text == reminder)
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;
    use crate::events::WIRE_VERSION;
    use crate::events::kinds::{AssistantEvent, UserEvent};
    use crate::ids::RunId;

    #[test]
    fn a_spec_reads_strictly_with_defaults() {
        let spec: TruncationRecovery = serde_json::from_value(json!({})).unwrap();
        assert_eq!(spec, TruncationRecovery::default());
        assert_eq!(spec.max_attempts, 1);
        assert_eq!(
            serde_json::to_value(&spec).unwrap(),
            json!({"max_attempts": 1})
        );
        assert!(serde_json::from_value::<TruncationRecovery>(json!({"x": 1})).is_err());
        let none = TruncationRecovery {
            max_attempts: 0,
            feedback: None,
        };
        assert!(none.problem().is_some());
        let blank = TruncationRecovery {
            max_attempts: 1,
            feedback: Some(" ".into()),
        };
        assert!(blank.problem().is_some());
        assert!(
            TruncationRecovery::default()
                .reminder()
                .contains("output token limit")
        );
    }

    fn envelopes(events: Vec<Event>) -> Vec<Envelope> {
        (1..)
            .zip(events)
            .map(|(seq, event)| Envelope {
                v: WIRE_VERSION,
                seq,
                run_id: RunId::new("r"),
                ts_ms: 0,
                ignorable: false,
                event,
            })
            .collect()
    }

    fn answer(text: &str, stop_reason: StopReason) -> Event {
        Event::Assistant(AssistantEvent {
            message: Message::assistant_text(text),
            stop_reason,
        })
    }

    fn note(text: &str) -> Event {
        Event::User(UserEvent {
            message: Message::user_text(text),
            subtype: UserEventSubtype::Notification,
        })
    }

    #[test]
    fn continued_parts_chain_until_a_response_that_finished() {
        let reminder = TruncationRecovery::default().reminder();
        let cut_part = |text| answer(text, StopReason::MaxTokens);
        let events = [
            cut_part("a"),
            note(&reminder),
            cut_part("b"),
            note(&reminder),
            answer("c", StopReason::EndTurn),
        ];
        let at = |n: usize| Truncated::of(&envelopes(events[..n].to_vec()), &reminder);
        assert_eq!(at(1).attempts, 0, "not continued yet");
        assert_eq!(at(1).before_latest, "");
        assert_eq!((at(2).attempts, at(2).chain.as_str()), (1, "a"));
        assert_eq!(at(3).before_latest, "a");
        assert_eq!((at(4).attempts, at(4).chain.as_str()), (2, "ab"));
        let done = at(5);
        assert_eq!(done.before_latest, "ab", "the final answer's parts");
        assert_eq!(done.chain, "", "the next answer starts over");
        assert_eq!(done.attempts, 2);
        // Any other feedback does not continue a cut answer.
        let other = Truncated::of(&envelopes(vec![cut_part("a"), note("other")]), &reminder);
        assert_eq!((other.attempts, other.chain.as_str()), (0, ""));
    }
}
