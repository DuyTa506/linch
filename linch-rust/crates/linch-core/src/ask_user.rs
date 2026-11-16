//! `AskUser`: a tool call that suspends the run until the user answers.
//!
//! The model asks one to four questions, each with two to four options. The
//! call does not run: the plan marks it [`PlannedAction::AwaitingInput`], and
//! the run stages one `input_request` event per call (its `request_id` is the
//! call's `tool_use_id`), a `suspended` event with status `awaiting_input`,
//! and writes the [`Phase::AwaitingInput`] checkpoint through, which sets the
//! run's status. [`crate::Agent::answer`] checks the host's
//! [`AskUserAnswer`] against the questions before it claims the run, then
//! commits the call's start and end, the end holding the answer as the
//! model will see it, with a `ToolBatch` checkpoint. From then on the answer
//! is that call's durable result: a crash replays it, and a second answer is
//! refused as not suspended.
//!
//! A subagent's run cannot suspend, so there the call gets an error result.
//!
//! [`PlannedAction::AwaitingInput`]: crate::scheduler::PlannedAction::AwaitingInput
//! [`Phase::AwaitingInput`]: crate::Phase::AwaitingInput

use std::collections::{BTreeMap, HashSet};

use async_trait::async_trait;
use serde::{Deserialize, Serialize};
use serde_json::json;

use crate::json::{JsonObject, JsonValue};
use crate::message::ToolResultBlock;
use crate::tool::{Tool, ToolContext, ToolOutputError, ToolResult, ToolScope};

/// The name of the `AskUser` tool.
pub const ASK_USER_TOOL: &str = "AskUser";

/// The result a subagent's `AskUser` call gets: its run cannot wait.
pub(crate) const CANNOT_WAIT: &str = "AskUser is not available here: this run cannot wait for the user. Proceed with explicit \
     assumptions and state them briefly.";

/// What the model sees when the user declines, as in Python Linch.
const DECLINED: &str = "The user declined to answer. Proceed with explicit assumptions, state \
     them briefly, and continue without asking again.";

/// One option of a question.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AskUserOption {
    /// Short option text shown to the user.
    pub label: String,
    /// What choosing the option means.
    pub description: String,
}

/// One question of an `AskUser` call.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AskUserQuestion {
    /// Names the question in the answer.
    pub id: String,
    /// Short display header, at most 12 characters.
    pub header: String,
    /// The full question.
    pub question: String,
    /// Two to four options.
    pub options: Vec<AskUserOption>,
    /// Whether the user may pick several options.
    #[serde(default)]
    pub multi_select: bool,
}

/// The answer to one question: one choice, or several for a multi-select
/// question. A choice is normally an option's label; the user may also
/// write their own.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(untagged)]
pub enum AnswerValue {
    /// The answer to a single-select question.
    One(String),
    /// The answers to a multi-select question.
    Many(Vec<String>),
}

/// The host's answer to one `input_request`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AskUserAnswer {
    /// Whether the user answered. `false` declines: the model is told to
    /// go on with stated assumptions. Defaults to `true`.
    #[serde(default = "yes")]
    pub accepted: bool,
    /// Answers by question id; every question needs one when accepted, and
    /// a declined answer carries none.
    #[serde(default)]
    pub answers: BTreeMap<String, AnswerValue>,
    /// A free note from the user, passed on to the model.
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub note: String,
}

fn yes() -> bool {
    true
}

impl AskUserAnswer {
    /// An accepted answer with `answers` and no note.
    pub fn new(answers: impl IntoIterator<Item = (String, AnswerValue)>) -> Self {
        Self {
            accepted: true,
            answers: answers.into_iter().collect(),
            note: String::new(),
        }
    }

    /// A declined answer with `note`.
    pub fn declined(note: impl Into<String>) -> Self {
        Self {
            accepted: false,
            answers: BTreeMap::new(),
            note: note.into(),
        }
    }

    /// Check this answer against the `questions` it answers.
    ///
    /// Returns:
    ///     What is wrong with it, when something is.
    pub fn check(&self, questions: &[AskUserQuestion]) -> Result<(), String> {
        if !self.accepted {
            if !self.answers.is_empty() {
                return Err("a declined answer carries no answers".into());
            }
            return Ok(());
        }
        for id in self.answers.keys() {
            if !questions.iter().any(|question| &question.id == id) {
                return Err(format!("there is no question {id:?}"));
            }
        }
        for question in questions {
            let id = &question.id;
            match (self.answers.get(id), question.multi_select) {
                (None, _) => return Err(format!("question {id:?} has no answer")),
                (Some(AnswerValue::One(choice)), false) if !choice.is_empty() => {}
                (Some(AnswerValue::Many(choices)), true)
                    if !choices.is_empty() && choices.iter().all(|c| !c.is_empty()) => {}
                (Some(_), false) => {
                    return Err(format!("question {id:?} takes one non-empty string"));
                }
                (Some(_), true) => {
                    return Err(format!(
                        "question {id:?} is multi-select: it takes a non-empty list of \
                         non-empty strings"
                    ));
                }
            }
        }
        Ok(())
    }

    /// The call's result as the model sees it: the answers as JSON (and the
    /// note), or the decline text, as in Python Linch.
    pub fn render(&self, tool_use_id: &str) -> ToolResultBlock {
        let text = match self.accepted {
            true => {
                let mut body = json!({ "answers": self.answers });
                if !self.note.is_empty() {
                    body["note"] = self.note.clone().into();
                }
                body.to_string()
            }
            false if self.note.is_empty() => DECLINED.to_owned(),
            false => format!("{DECLINED}\nUser note: {}", self.note),
        };
        ToolResultBlock::text(tool_use_id, text, false)
    }
}

/// The questions of a validated `AskUser` input.
///
/// Raises:
///     ToolOutputError: If the input does not hold well-formed questions.
pub fn questions(input: &JsonObject) -> Result<Vec<AskUserQuestion>, ToolOutputError> {
    let raw = input.get("questions").cloned().unwrap_or(JsonValue::Null);
    let questions: Vec<AskUserQuestion> = serde_json::from_value(raw)
        .map_err(|error| ToolOutputError::new(format!("invalid questions: {error}")))?;
    let mut ids = HashSet::new();
    for question in &questions {
        if !ids.insert(question.id.as_str()) {
            let id = &question.id;
            return Err(ToolOutputError::new(format!(
                "question ids must be unique; {id:?} repeats"
            )));
        }
        let mut labels = HashSet::new();
        if !question.options.iter().all(|o| labels.insert(&o.label)) {
            let id = &question.id;
            return Err(ToolOutputError::new(format!(
                "the option labels of question {id:?} must be unique"
            )));
        }
    }
    Ok(questions)
}

/// The text of an `input_request` for `questions`: each question on its
/// own line.
pub(crate) fn prompt(questions: &[AskUserQuestion]) -> String {
    let lines: Vec<&str> = questions.iter().map(|q| q.question.as_str()).collect();
    lines.join("\n")
}

/// The one-line summary of an `AskUser` call.
pub(crate) fn summary(input: &JsonObject) -> String {
    let count = (input.get("questions").and_then(JsonValue::as_array)).map_or(0, Vec::len);
    format!("AskUser: {count} question(s)")
}

/// Ask the user one to four questions with explicit options and wait for
/// the answer. Scope `read`, never parallel, off unless registered.
///
/// See the [module docs](self) for how the run suspends and resumes.
pub struct AskUser {
    schema: JsonObject,
}

impl AskUser {
    /// The tool, with Python Linch's input schema.
    pub fn new() -> Self {
        let text = |max: Option<u64>| match max {
            Some(max) => json!({"type": "string", "minLength": 1, "maxLength": max}),
            None => json!({"type": "string", "minLength": 1}),
        };
        let option = json!({
            "type": "object",
            "properties": {"label": text(None), "description": text(None)},
            "required": ["label", "description"],
            "additionalProperties": false
        });
        let question = json!({
            "type": "object",
            "properties": {
                "id": text(None),
                "header": text(Some(12)),
                "question": text(None),
                "multi_select": {"type": "boolean"},
                "options": {"type": "array", "minItems": 2, "maxItems": 4, "items": option}
            },
            "required": ["id", "header", "question", "options"],
            "additionalProperties": false
        });
        let schema = json!({
            "type": "object",
            "properties": {
                "questions": {"type": "array", "minItems": 1, "maxItems": 4, "items": question}
            },
            "required": ["questions"],
            "additionalProperties": false
        });
        let JsonValue::Object(schema) = schema else {
            unreachable!("the schema is an object")
        };
        Self { schema }
    }
}

impl Default for AskUser {
    fn default() -> Self {
        Self::new()
    }
}

#[async_trait]
impl Tool for AskUser {
    fn name(&self) -> &str {
        ASK_USER_TOOL
    }

    fn description(&self) -> &str {
        "Ask the user to choose from short, explicit options before proceeding. The run \
         waits for the answer. The user may also decline, or answer in their own words."
    }

    fn input_schema(&self) -> &JsonObject {
        &self.schema
    }

    fn scope(&self) -> ToolScope {
        ToolScope::Read
    }

    fn parallel(&self, _input: &JsonObject) -> bool {
        false
    }

    fn asks_user(&self) -> bool {
        true
    }

    fn validate(&self, input: JsonObject) -> Result<JsonObject, ToolOutputError> {
        questions(&input)?;
        Ok(input)
    }

    fn summarize(&self, input: &JsonObject) -> String {
        summary(input)
    }

    async fn execute(&self, _input: JsonObject, _ctx: ToolContext) -> ToolResult {
        Err(ToolOutputError::new(CANNOT_WAIT))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn input(value: JsonValue) -> JsonObject {
        let JsonValue::Object(object) = value else {
            panic!("not an object")
        };
        object
    }

    fn two_questions() -> Vec<AskUserQuestion> {
        let raw = json!({"questions": [
            {"id": "db", "header": "Database", "question": "Which database?",
             "options": [{"label": "Postgres", "description": "durable"},
                         {"label": "Memory", "description": "for tests"}]},
            {"id": "langs", "header": "Languages", "question": "Which languages?",
             "multi_select": true,
             "options": [{"label": "Rust", "description": "core"},
                         {"label": "Python", "description": "client"}]}
        ]});
        questions(&input(raw)).unwrap()
    }

    #[test]
    fn repeated_ids_or_labels_are_refused() {
        let option = json!({"label": "A", "description": "a"});
        let question = json!({"id": "q", "header": "H", "question": "?",
                              "options": [option, {"label": "B", "description": "b"}]});
        let twice = input(json!({"questions": [question.clone(), question]}));
        assert!(questions(&twice).unwrap_err().message.contains("unique"));
        let same = json!({"id": "q", "header": "H", "question": "?",
                          "options": [option, option]});
        let labels = input(json!({"questions": [same]}));
        assert!(questions(&labels).unwrap_err().message.contains("labels"));
    }

    #[test]
    fn an_answer_must_answer_every_question_in_its_shape() {
        let questions = two_questions();
        let one = |s: &str| AnswerValue::One(s.into());
        let many = |s: &[&str]| AnswerValue::Many(s.iter().map(|s| s.to_string()).collect());
        let good = AskUserAnswer::new([
            ("db".to_owned(), one("Postgres")),
            ("langs".to_owned(), many(&["Rust", "Go"])),
        ]);
        assert_eq!(good.check(&questions), Ok(()));
        let cases = [
            (vec![("db", one("Postgres"))], "has no answer"),
            (
                vec![("db", many(&["Postgres"])), ("langs", many(&["Rust"]))],
                "takes one",
            ),
            (
                vec![("db", one("Postgres")), ("langs", one("Rust"))],
                "multi-select",
            ),
            (
                vec![("db", one("")), ("langs", many(&["Rust"]))],
                "takes one",
            ),
            (
                vec![
                    ("db", one("x")),
                    ("langs", many(&["Rust"])),
                    ("zz", one("x")),
                ],
                "no question",
            ),
        ];
        for (answers, want) in cases {
            let answer = AskUserAnswer::new(answers.into_iter().map(|(k, v)| (k.to_owned(), v)));
            let error = answer.check(&questions).unwrap_err();
            assert!(error.contains(want), "{error}");
        }
        assert_eq!(AskUserAnswer::declined("later").check(&questions), Ok(()));
        let mut declined = good.clone();
        declined.accepted = false;
        assert!(declined.check(&questions).is_err());
    }

    #[test]
    fn answers_render_as_python_does() {
        let answer = AskUserAnswer::new([("db".to_owned(), AnswerValue::One("Postgres".into()))]);
        let block = answer.render("c1");
        assert_eq!(
            block,
            ToolResultBlock::text("c1", r#"{"answers":{"db":"Postgres"}}"#, false)
        );
        let declined = AskUserAnswer::declined("busy").render("c1");
        let want = format!("{DECLINED}\nUser note: busy");
        assert_eq!(declined, ToolResultBlock::text("c1", want, false));
    }

    #[test]
    fn the_wire_answer_defaults_to_accepted() {
        let answer: AskUserAnswer =
            serde_json::from_value(json!({"answers": {"db": "Postgres", "langs": ["Rust"]}}))
                .unwrap();
        assert!(answer.accepted && answer.note.is_empty());
        assert_eq!(
            answer.answers["langs"],
            AnswerValue::Many(vec!["Rust".into()])
        );
        let unknown = json!({"answers": {}, "extra": 1});
        assert!(serde_json::from_value::<AskUserAnswer>(unknown).is_err());
    }
}
