//! Final-answer verifiers: checks a run's text-only answer must pass before
//! the run completes (Python Linch's `FinalAnswerVerifierHook`).
//!
//! [`VerificationSpec`] is plain serde data on the agent's spec, so it is
//! part of the run contract. It holds the built-in verifiers, which the
//! core runs itself (a regex that must or must not match, keywords that
//! must appear), and the retry budget they share with the `stop` hooks:
//! with a spec, a `stop` hook's `retry` (a host's callback included) draws
//! on `max_retries` instead of the kernel's one retry per run call, so a
//! client verifier is a `stop` callback.
//!
//! The answer is checked after the output schema, on the text the session
//! logged (so redacted, when redaction is on), and before the `stop`
//! chain. Each failure stages a `verification` event naming the verifier.
//! A `retry` verifier sends its feedback to the model in a system reminder
//! while retries last; past them the answer is accepted as it is, with an
//! `exhausted` event, as in Python. A `stop` verifier fails the run at
//! once. The retries spent are counted from the run's durable events, so a
//! resumed run keeps the bound.

use std::collections::HashSet;

use regex::{Regex, RegexBuilder};
use serde::{Deserialize, Serialize};

use crate::events::kinds::VerificationAction;
use crate::events::{Envelope, Event};
use crate::structured::VERIFIER as SCHEMA_VERIFIER;

/// Retries a [`VerificationSpec`] allows unless it says otherwise
/// (Python's `max_verification_retries`).
pub const DEFAULT_MAX_RETRIES: u32 = 2;

/// Largest compiled program one pattern may take, in bytes.
const SIZE_LIMIT: usize = 1 << 20;

fn default_max_retries() -> u32 {
    DEFAULT_MAX_RETRIES
}

fn is_default_max_retries(value: &u32) -> bool {
    *value == DEFAULT_MAX_RETRIES
}

/// The verifiers of an agent's final answers and their retry budget.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct VerificationSpec {
    /// How many times a failed answer goes back to the model, across the
    /// built-in verifiers and the `stop` hooks' retries, per run. 2 when
    /// absent. Each retry is a turn, so `max_turns` bounds them too.
    #[serde(
        default = "default_max_retries",
        skip_serializing_if = "is_default_max_retries"
    )]
    pub max_retries: u32,
    /// Built-in verifiers, run in order; the first failure decides. Empty
    /// when only `stop` hooks verify.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub verifiers: Vec<Verifier>,
}

/// One built-in verifier.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Verifier {
    /// Names the verifier in `verification` events; unique, not
    /// `output_schema`.
    pub name: String,
    /// What the answer is checked for.
    pub check: Check,
    /// What the model is told on a retry; a sentence naming the check
    /// when absent.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub feedback: Option<String>,
    /// What a failure does. `retry` when absent.
    #[serde(default, skip_serializing_if = "OnFail::is_retry")]
    pub on_fail: OnFail,
}

/// What a built-in verifier checks the answer text for.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
pub enum Check {
    /// `pattern` (`regex` crate syntax) must match somewhere in the
    /// answer.
    Matches {
        /// The regular expression.
        pattern: String,
    },
    /// `pattern` must not match anywhere in the answer.
    Forbids {
        /// The regular expression.
        pattern: String,
    },
    /// The answer must contain every keyword, or one of them with `any`,
    /// as substrings.
    Keywords {
        /// The keywords; none may be blank.
        keywords: Vec<String>,
        /// Whether one keyword is enough.
        #[serde(default, skip_serializing_if = "std::ops::Not::not")]
        any: bool,
        /// Whether case matters.
        #[serde(default, skip_serializing_if = "std::ops::Not::not")]
        case_sensitive: bool,
    },
}

/// What a failed verifier does.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum OnFail {
    /// Send the feedback to the model for another answer while retries
    /// last, then accept the answer.
    #[default]
    Retry,
    /// Fail the run.
    Stop,
}

impl OnFail {
    fn is_retry(&self) -> bool {
        *self == Self::Retry
    }
}

/// A failed check: which verifier, what to tell the model, what to do.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct Failure {
    pub(crate) verifier: String,
    pub(crate) feedback: String,
    pub(crate) on_fail: OnFail,
}

/// A [`VerificationSpec`] compiled once, for every run of an agent.
#[derive(Debug)]
pub(crate) struct Verifiers {
    pub(crate) max_retries: u32,
    checks: Vec<(Verifier, Compiled)>,
}

#[derive(Debug)]
enum Compiled {
    Matches(Regex),
    Forbids(Regex),
    Keywords(Vec<String>),
}

impl Verifiers {
    /// Compile `spec`.
    ///
    /// Raises:
    ///     String: Why the spec cannot be used: a blank, repeated or
    ///         reserved name, a pattern that does not compile, a keyword
    ///         list that is empty or holds a blank keyword.
    pub(crate) fn new(spec: &VerificationSpec) -> Result<Self, String> {
        let mut names = HashSet::new();
        let mut checks = Vec::with_capacity(spec.verifiers.len());
        for verifier in &spec.verifiers {
            let name = verifier.name.trim();
            if name.is_empty() {
                return Err("a verifier's name must not be blank".into());
            }
            if name == SCHEMA_VERIFIER {
                return Err(format!("the verifier name {SCHEMA_VERIFIER:?} is reserved"));
            }
            if !names.insert(name) {
                return Err(format!("two verifiers are named {name:?}"));
            }
            let compiled = match &verifier.check {
                Check::Matches { pattern } => Compiled::Matches(regex(name, pattern)?),
                Check::Forbids { pattern } => Compiled::Forbids(regex(name, pattern)?),
                Check::Keywords {
                    keywords,
                    case_sensitive,
                    ..
                } => {
                    if keywords.is_empty() || keywords.iter().any(|k| k.trim().is_empty()) {
                        return Err(format!(
                            "verifier {name:?}: keywords must be a list of non-blank words"
                        ));
                    }
                    let fold = |k: &String| match case_sensitive {
                        true => k.clone(),
                        false => k.to_lowercase(),
                    };
                    Compiled::Keywords(keywords.iter().map(fold).collect())
                }
            };
            checks.push((verifier.clone(), compiled));
        }
        Ok(Self {
            max_retries: spec.max_retries,
            checks,
        })
    }

    /// The first verifier `text` fails, in order; `None` when it passes
    /// them all.
    pub(crate) fn check(&self, text: &str) -> Option<Failure> {
        let lower = std::cell::OnceCell::new();
        self.checks.iter().find_map(|(verifier, compiled)| {
            let passed = match (compiled, &verifier.check) {
                (Compiled::Matches(regex), _) => regex.is_match(text),
                (Compiled::Forbids(regex), _) => !regex.is_match(text),
                (
                    Compiled::Keywords(keywords),
                    Check::Keywords {
                        any,
                        case_sensitive,
                        ..
                    },
                ) => {
                    let haystack = match case_sensitive {
                        true => text,
                        false => lower.get_or_init(|| text.to_lowercase()).as_str(),
                    };
                    let found = |keyword: &String| haystack.contains(keyword.as_str());
                    match any {
                        true => keywords.iter().any(found),
                        false => keywords.iter().all(found),
                    }
                }
                (Compiled::Keywords(_), _) => unreachable!("compiled from its own check"),
            };
            (!passed).then(|| Failure {
                verifier: verifier.name.trim().to_owned(),
                feedback: (verifier.feedback.clone()).unwrap_or_else(|| feedback(&verifier.check)),
                on_fail: verifier.on_fail,
            })
        })
    }
}

fn regex(name: &str, pattern: &str) -> Result<Regex, String> {
    if pattern.trim().is_empty() {
        return Err(format!("verifier {name:?}: the pattern is blank"));
    }
    let regex = (RegexBuilder::new(pattern).size_limit(SIZE_LIMIT).build())
        .map_err(|error| format!("verifier {name:?}: {error}"))?;
    if regex.is_match("") {
        // It matches every answer, so it decides nothing.
        return Err(format!("verifier {name:?}: the pattern matches empty text"));
    }
    Ok(regex)
}

/// What a verifier with no `feedback` tells the model.
fn feedback(check: &Check) -> String {
    let what = match check {
        Check::Matches { pattern } => format!("must match the pattern `{pattern}`"),
        Check::Forbids { pattern } => format!("must not contain text matching `{pattern}`"),
        Check::Keywords { keywords, any, .. } => {
            let list = keywords.join(", ");
            match any {
                true => format!("must mention at least one of: {list}"),
                false => format!("must mention each of: {list}"),
            }
        }
    };
    format!("Your previous answer failed verification: it {what}. Revise it and answer again.")
}

/// How many verification retries `events`, a run's durable log, already
/// spent: one per `verification` retry of a verifier or `stop` hook (the
/// schema gate counts its own). A resumed run counts them again, so the
/// bound holds across crashes.
pub(crate) fn retries_spent(events: &[Envelope]) -> u32 {
    let spent = events.iter().filter(|envelope| {
        matches!(&envelope.event, Event::Verification(event)
            if event.verifier != SCHEMA_VERIFIER && event.action == VerificationAction::Retry)
    });
    u32::try_from(spent.count()).unwrap_or(u32::MAX)
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    fn verifiers(value: serde_json::Value) -> Result<Verifiers, String> {
        let spec: VerificationSpec = serde_json::from_value(value).unwrap();
        Verifiers::new(&spec)
    }

    #[test]
    fn a_spec_reads_strictly_with_python_defaults() {
        let spec: VerificationSpec = serde_json::from_value(json!({
            "verifiers": [{"name": "cites", "check": {"type": "matches", "pattern": "\\[\\d+\\]"}}]
        }))
        .unwrap();
        assert_eq!(spec.max_retries, 2);
        assert_eq!(spec.verifiers[0].on_fail, OnFail::Retry);
        assert_eq!(
            serde_json::to_value(&spec).unwrap(),
            json!({"verifiers": [{"name": "cites", "check": {"type": "matches", "pattern": "\\[\\d+\\]"}}]})
        );
        for unknown in [
            json!({"verifiers": [], "retries": 1}),
            json!({"verifiers": [{"name": "a", "check": {"type": "matches", "pattern": "x", "flags": 1}}]}),
            json!({"verifiers": [{"name": "a", "check": {"type": "python", "code": "x"}}]}),
        ] {
            assert!(
                serde_json::from_value::<VerificationSpec>(unknown.clone()).is_err(),
                "{unknown}"
            );
        }
    }

    #[test]
    fn each_check_passes_and_fails_as_it_says() {
        let all = verifiers(json!({"verifiers": [
            {"name": "cites", "check": {"type": "matches", "pattern": "\\[\\d+\\]"}},
            {"name": "no_keys", "check": {"type": "forbids", "pattern": "sk-\\w+"}, "on_fail": "stop"},
            {"name": "topic", "check": {"type": "keywords", "keywords": ["Refund", "policy"]},
             "feedback": "Name the refund policy."},
            {"name": "either", "check": {"type": "keywords", "keywords": ["A", "B"], "any": true,
             "case_sensitive": true}},
        ]}))
        .unwrap();
        assert_eq!(all.check("see [1]: the REFUND policy, A"), None);
        let failed = |text: &str| all.check(text).map(|f| (f.verifier, f.on_fail));
        assert_eq!(failed("no cite"), Some(("cites".into(), OnFail::Retry)));
        assert_eq!(failed("[1] sk-abc"), Some(("no_keys".into(), OnFail::Stop)));
        assert_eq!(
            failed("[1] refund only"),
            Some(("topic".into(), OnFail::Retry))
        );
        assert_eq!(
            failed("[1] refund policy a b"),
            Some(("either".into(), OnFail::Retry))
        );
        let topic = all.check("[1] refund").unwrap();
        assert_eq!(topic.feedback, "Name the refund policy.");
        let cites = all.check("none").unwrap();
        assert_eq!(
            cites.feedback,
            "Your previous answer failed verification: it must match the pattern `\\[\\d+\\]`. \
             Revise it and answer again."
        );
    }

    #[test]
    fn a_spec_that_cannot_verify_is_refused() {
        let check = json!({"type": "matches", "pattern": "x"});
        let refused = |verifiers_: serde_json::Value| {
            verifiers(json!({"verifiers": verifiers_})).unwrap_err()
        };
        assert!(refused(json!([{"name": " ", "check": check}])).contains("blank"));
        assert!(refused(json!([{"name": "output_schema", "check": check}])).contains("reserved"));
        let twice = json!([{"name": "a", "check": check}, {"name": "a", "check": check}]);
        assert!(refused(twice).contains("two verifiers"));
        let bad = json!([{"name": "a", "check": {"type": "forbids", "pattern": "("}}]);
        assert!(refused(bad).contains("verifier \"a\""));
        let empty = json!([{"name": "a", "check": {"type": "keywords", "keywords": []}}]);
        assert!(refused(empty).contains("non-blank"));
        assert!(
            verifiers(json!({"max_retries": 0})).is_ok(),
            "stop hooks only"
        );
    }
}
