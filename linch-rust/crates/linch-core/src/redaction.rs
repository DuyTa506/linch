//! Redaction: regex rules that scrub sensitive text before it is logged,
//! streamed, or shown to a hook.
//!
//! [`RedactionSpec`] is plain serde data on the agent's spec, so it is part
//! of the run contract. [`Redactor`] is its compiled form. Linch ships the
//! mechanism and no patterns, as Python Linch's `RedactionHook` does: the
//! host decides what is sensitive and what replaces it.
//!
//! Three surfaces, each its own switch:
//!
//! - `response`: the text and unsigned reasoning of every assistant
//!   response, before the `after_provider_call` chain sees it and before
//!   it is staged, so the logged message is the redacted one and the model
//!   reads that back; a hook's rewrite is redacted again. The final-answer
//!   text after a `stop` rewrite and a compaction summary are redacted
//!   too. Streamed `partial_assistant` deltas are not sent, since a match
//!   can span two deltas.
//! - `tool_results`: every string of a tool's result (value, metadata,
//!   error message and details) before the `post_tool_use` chain, and
//!   again after it; an `AskUser` answer too. `tool_progress` events are
//!   not sent.
//! - `prompt` (off by default): the text of a new run's prompt, before the
//!   `user_prompt_submit` chain, and again after a rewrite.
//!
//! Never changed: a tool call's input (the tool runs what is logged), a
//! signed thinking block (its signature would no longer verify on replay)
//! and encrypted reasoning.
//!
//! Patterns use the `regex` crate's syntax, which matches in linear time:
//! no look-around and no back-references. A replacement names groups as
//! `$1` or `${name}`; `$$` is a literal `$`.

use std::borrow::Cow;

use regex::{Regex, RegexBuilder};
use serde::{Deserialize, Serialize};

use crate::json::JsonValue;
use crate::message::{ContentBlock, Message, ToolResultBlock, ToolResultPart};
use crate::tool::ToolResult;

/// What a rule puts in place of a match unless it says otherwise.
pub const DEFAULT_REPLACEMENT: &str = "[REDACTED]";

/// Largest compiled program one pattern may take, in bytes.
const SIZE_LIMIT: usize = 1 << 20;

fn yes() -> bool {
    true
}

fn is_true(value: &bool) -> bool {
    *value
}

fn default_replacement() -> String {
    DEFAULT_REPLACEMENT.to_owned()
}

/// Which text the agent scrubs, and with which rules.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RedactionSpec {
    /// Rules applied in order, each to the output of the one before.
    pub rules: Vec<RedactionRule>,
    /// Whether assistant responses are scrubbed. On when absent.
    #[serde(default = "yes", skip_serializing_if = "is_true")]
    pub response: bool,
    /// Whether tool results are scrubbed. On when absent.
    #[serde(default = "yes", skip_serializing_if = "is_true")]
    pub tool_results: bool,
    /// Whether a new run's prompt is scrubbed. Off when absent: the prompt
    /// is the host's own input.
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub prompt: bool,
}

/// Replace each match of `pattern` with `replacement`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RedactionRule {
    /// A regular expression (`regex` crate syntax; `(?i)` for any case).
    pub pattern: String,
    /// What replaces a match; `[REDACTED]` when absent.
    #[serde(default = "default_replacement")]
    pub replacement: String,
}

impl RedactionRule {
    /// Rule replacing `pattern` with `[REDACTED]`.
    pub fn new(pattern: impl Into<String>) -> Self {
        Self {
            pattern: pattern.into(),
            replacement: default_replacement(),
        }
    }
}

/// A [`RedactionSpec`] compiled once, for every run of an agent.
#[derive(Debug)]
pub struct Redactor {
    rules: Vec<(Regex, String)>,
    spec: RedactionSpec,
}

impl Redactor {
    /// Compile `spec`.
    ///
    /// Raises:
    ///     String: Why the spec cannot be used: no rules, every surface
    ///         off, a blank pattern, one that does not compile or exceeds
    ///         the size limit, or one that matches empty text (it would
    ///         insert its replacement between every two characters).
    pub fn new(spec: &RedactionSpec) -> Result<Self, String> {
        if spec.rules.is_empty() {
            return Err("redaction needs at least one rule".into());
        }
        if !(spec.response || spec.tool_results || spec.prompt) {
            return Err("redaction turns every surface off; leave it out instead".into());
        }
        let mut rules = Vec::with_capacity(spec.rules.len());
        for (index, rule) in spec.rules.iter().enumerate() {
            if rule.pattern.trim().is_empty() {
                return Err(format!("redaction rule {index}: the pattern is blank"));
            }
            let regex = RegexBuilder::new(&rule.pattern)
                .size_limit(SIZE_LIMIT)
                .build()
                .map_err(|error| format!("redaction rule {index}: {error}"))?;
            if regex.is_match("") {
                return Err(format!(
                    "redaction rule {index}: the pattern matches empty text"
                ));
            }
            rules.push((regex, rule.replacement.clone()));
        }
        Ok(Self {
            rules,
            spec: spec.clone(),
        })
    }

    /// Whether assistant responses are scrubbed.
    pub fn response(&self) -> bool {
        self.spec.response
    }

    /// Whether tool results are scrubbed.
    pub fn tool_results(&self) -> bool {
        self.spec.tool_results
    }

    /// Whether a new run's prompt is scrubbed.
    pub fn prompt(&self) -> bool {
        self.spec.prompt
    }

    /// `text` with every rule applied in order; borrowed when nothing
    /// matched.
    pub fn text<'t>(&self, text: &'t str) -> Cow<'t, str> {
        let mut out = Cow::Borrowed(text);
        for (regex, replacement) in &self.rules {
            if let Cow::Owned(changed) = regex.replace_all(&out, replacement.as_str()) {
                out = Cow::Owned(changed);
            }
        }
        out
    }

    /// Scrub `text` in place; whether it changed.
    fn scrub(&self, text: &mut String) -> bool {
        match self.text(text) {
            Cow::Borrowed(_) => false,
            Cow::Owned(changed) => {
                *text = changed;
                true
            }
        }
    }

    /// Scrub the text and unsigned reasoning blocks of `message` (a tool
    /// call's input, signed reasoning and encrypted reasoning stay as
    /// they are), and the text parts of its tool results.
    pub fn message(&self, message: &mut Message) {
        for block in &mut message.content {
            match block {
                ContentBlock::Text(text) => {
                    self.scrub(&mut text.text);
                }
                ContentBlock::Thinking(thinking) if thinking.signature.is_none() => {
                    self.scrub(&mut thinking.thinking);
                }
                ContentBlock::ToolResult(result) => self.result_block(result),
                _ => {}
            }
        }
    }

    /// Scrub the text parts of a tool result block.
    pub fn result_block(&self, block: &mut ToolResultBlock) {
        for part in &mut block.content {
            if let ToolResultPart::Text(text) = part {
                self.scrub(&mut text.text);
            }
        }
    }

    /// Scrub every string of `result`: the output's value and metadata, or
    /// the error's message and details.
    pub fn result(&self, result: &mut ToolResult) {
        match result {
            Ok(output) => {
                self.json(&mut output.value);
                for value in output.metadata.values_mut() {
                    self.json(value);
                }
            }
            Err(error) => {
                self.scrub(&mut error.message);
                if let Some(details) = &mut error.details {
                    self.json(details);
                }
            }
        }
    }

    /// Scrub every string in `value`, object keys aside.
    fn json(&self, value: &mut JsonValue) {
        match value {
            JsonValue::String(text) => {
                self.scrub(text);
            }
            JsonValue::Array(items) => items.iter_mut().for_each(|item| self.json(item)),
            JsonValue::Object(object) => object.values_mut().for_each(|item| self.json(item)),
            JsonValue::Null | JsonValue::Bool(_) | JsonValue::Number(_) => {}
        }
    }
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;
    use crate::message::{Role, ThinkingBlock, ToolUseBlock};
    use crate::tool::{ToolOutput, ToolOutputError};

    const KEY: &str = r"sk-[A-Za-z0-9]{8,}";

    fn redactor(rules: Vec<RedactionRule>) -> Redactor {
        Redactor::new(&RedactionSpec {
            rules,
            response: true,
            tool_results: true,
            prompt: false,
        })
        .unwrap()
    }

    #[test]
    fn a_spec_reads_with_python_defaults_and_writes_them_back_out() {
        let spec: RedactionSpec =
            serde_json::from_value(json!({"rules": [{"pattern": "x+"}]})).unwrap();
        assert!(spec.response && spec.tool_results && !spec.prompt);
        assert_eq!(spec.rules[0].replacement, "[REDACTED]");
        assert_eq!(
            serde_json::to_value(&spec).unwrap(),
            json!({"rules": [{"pattern": "x+", "replacement": "[REDACTED]"}]})
        );
        let unknown = json!({"rules": [], "detectors": ["email"]});
        assert!(serde_json::from_value::<RedactionSpec>(unknown).is_err());
    }

    #[test]
    fn rules_apply_in_order_with_groups_in_the_replacement() {
        let redact = redactor(vec![
            RedactionRule {
                pattern: r"(?i)(\w+)@example\.com".into(),
                replacement: "$1@[DOMAIN]".into(),
            },
            RedactionRule::new(KEY),
        ]);
        let text = "mail Bob@EXAMPLE.com with sk-abcdefgh12 now";
        assert_eq!(redact.text(text), "mail Bob@[DOMAIN] with [REDACTED] now");
        assert!(matches!(redact.text("clean"), Cow::Borrowed("clean")));
    }

    #[test]
    fn a_spec_that_cannot_redact_safely_is_refused() {
        let spec = |rules: Vec<RedactionRule>| RedactionSpec {
            rules,
            response: true,
            tool_results: true,
            prompt: false,
        };
        let refused = |spec: RedactionSpec| Redactor::new(&spec).unwrap_err();
        assert!(refused(spec(vec![])).contains("at least one rule"));
        assert!(refused(spec(vec![RedactionRule::new(" ")])).contains("blank"));
        assert!(refused(spec(vec![RedactionRule::new("(")])).contains("rule 0"));
        assert!(refused(spec(vec![RedactionRule::new("a*")])).contains("empty text"));
        assert!(refused(spec(vec![RedactionRule::new(r"(?<=a)b")])).contains("rule 0"));
        let off = RedactionSpec {
            response: false,
            tool_results: false,
            ..spec(vec![RedactionRule::new("x")])
        };
        assert!(refused(off).contains("every surface off"));
        let huge = RedactionRule::new(r"\w{1000}\w{1000}\w{1000}");
        assert!(refused(spec(vec![huge])).contains("rule 0"));
    }

    #[test]
    fn a_message_keeps_its_tool_inputs_and_signed_reasoning() {
        let redact = redactor(vec![RedactionRule::new(KEY)]);
        let secret = "sk-abcdefgh12";
        let call = ToolUseBlock {
            id: "c1".into(),
            name: "Write".into(),
            input: json!({"content": secret}).as_object().cloned().unwrap(),
        };
        let mut message = Message::new(
            Role::Assistant,
            vec![
                ContentBlock::text(format!("key {secret}")),
                ContentBlock::Thinking(ThinkingBlock {
                    thinking: secret.into(),
                    signature: None,
                }),
                ContentBlock::Thinking(ThinkingBlock {
                    thinking: secret.into(),
                    signature: Some("sig".into()),
                }),
                ContentBlock::ToolUse(call.clone()),
            ],
        );
        redact.message(&mut message);
        assert_eq!(message.content[0], ContentBlock::text("key [REDACTED]"));
        let ContentBlock::Thinking(unsigned) = &message.content[1] else {
            panic!("thinking")
        };
        assert_eq!(unsigned.thinking, "[REDACTED]");
        let ContentBlock::Thinking(signed) = &message.content[2] else {
            panic!("thinking")
        };
        assert_eq!(signed.thinking, secret, "a signature must still verify");
        assert_eq!(message.content[3], ContentBlock::ToolUse(call));
    }

    #[test]
    fn every_string_of_a_result_is_scrubbed_but_not_its_keys() {
        let redact = redactor(vec![RedactionRule::new(KEY)]);
        let mut output = ToolOutput::new(json!({"sk-abcdefgh12": ["sk-abcdefgh12", 3]}));
        output.metadata.insert("raw".into(), json!("sk-abcdefgh12"));
        let mut ok: ToolResult = Ok(output);
        redact.result(&mut ok);
        let output = ok.unwrap();
        assert_eq!(output.value, json!({"sk-abcdefgh12": ["[REDACTED]", 3]}));
        assert_eq!(output.metadata["raw"], json!("[REDACTED]"));
        let mut error: ToolResult = Err(ToolOutputError {
            message: "bad sk-abcdefgh12".into(),
            code: Some("sk-abcdefgh12".into()),
            details: Some(json!({"echo": "sk-abcdefgh12"})),
        });
        redact.result(&mut error);
        let error = error.unwrap_err();
        assert_eq!(error.message, "bad [REDACTED]");
        assert_eq!(error.details, Some(json!({"echo": "[REDACTED]"})));
    }
}
