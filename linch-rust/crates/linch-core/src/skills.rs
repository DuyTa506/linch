//! Skills: named prompt workflows the model loads with the `Skill` tool.
//!
//! A host passes the [`Skill`]s it loaded (the `linch` facade reads
//! `.linch/skills/*/SKILL.md`) to [`crate::AgentBuilder::skills`]. That
//! turns skills on: the built-in [`verify`] skill joins them unless one of
//! the host's skills has the same name (case-insensitively), the `Skill`
//! tool is registered, and the [`listing`] of every skill the model may
//! invoke goes into the agent's system prompt as a cacheable block, so it
//! sits in the cached prefix of every request. A bare agent has no skills.
//!
//! The tool returns the skill's [`substitute`]d body as its result. A
//! compaction that folds that result away puts the body back as a
//! `<system-reminder>` in the summary message, which is part of the logged
//! projection: it reaches the provider view and never the full history.
//!
//! A call reports a `skill_invoked` event before its `tool_call_start` and
//! a `skill_completed` one after its `tool_call_end`, as Python does. A
//! skill whose frontmatter names `allowed_tools` or a `model` sets them for
//! the next model turn only: that turn offers only those tools (a call of
//! another is refused as not registered) and uses that model. The overlay
//! only narrows: it never allows a call the permission policy would ask
//! about.

mod listing;
mod overlay;
mod set;
mod substitute;
#[cfg(test)]
mod tests;
mod tool;

use std::sync::Arc;

use serde::Serialize;

use crate::message::{ContentBlock, Message, Role, ToolResultPart};

pub use listing::listing;
pub use overlay::model_override;
pub(crate) use overlay::{Overlays, overlay, overlays};
pub(crate) use set::{SkillSet, deferred_listing};
pub use substitute::{split_args, substitute};
pub(crate) use tool::{SkillTool, requested};

/// Name of the tool that loads a skill.
pub const SKILL_TOOL: &str = "Skill";

/// A loaded skill: its metadata and its markdown body.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Skill {
    /// Name the model passes to the `Skill` tool.
    pub name: String,
    /// Directory of the skill, which `${SKILL_DIR}` expands to.
    pub dir: String,
    /// One line shown in the listing.
    pub description: String,
    /// When to use the skill, appended to the description in the listing.
    pub when_to_use: Option<String>,
    /// How to pass arguments, shown in the listing.
    pub argument_hint: Option<String>,
    /// Names of positional arguments, each substituted as `$name`.
    pub arguments: Vec<String>,
    /// The only tools the model turn after a load of the skill offers;
    /// `None` or empty for all of them. Part of the run contract.
    pub allowed_tools: Option<Vec<String>>,
    /// The model of the turn after a load of the skill (see
    /// [`model_override`]). Part of the run contract.
    pub model: Option<String>,
    /// Version metadata.
    pub version: Option<String>,
    /// Whether the model is refused this skill (it is also left out of the
    /// listing).
    pub disable_model_invocation: bool,
    /// Markdown prompt body, before substitution.
    pub body: String,
}

impl Skill {
    /// A skill named `name` in `dir`, with every optional field empty.
    pub fn new(
        name: impl Into<String>,
        dir: impl Into<String>,
        description: impl Into<String>,
        body: impl Into<String>,
    ) -> Self {
        Self {
            name: name.into(),
            dir: dir.into(),
            description: description.into(),
            when_to_use: None,
            argument_hint: None,
            arguments: Vec::new(),
            allowed_tools: None,
            model: None,
            version: None,
            disable_model_invocation: false,
            body: body.into(),
        }
    }
}

/// The built-in `verify` skill: plan and run evidence-based checks of
/// completed work, ending with a `VERDICT:` line.
pub fn verify() -> Skill {
    let mut skill = Skill::new(
        "verify",
        "<built-in>/verify",
        "Plan and run evidence-based verification for completed work, ending with a verdict.",
        VERIFY_BODY,
    );
    skill.when_to_use =
        Some("Use after non-trivial changes or before claiming a task is complete.".into());
    skill.argument_hint = Some("optional task, artifact, or risk focus".into());
    skill
}

const VERIFY_BODY: &str = "# Verify

Verify completed work with runnable evidence. Adapt the checks to the domain:
software changes, data workflows, document/process updates, configuration,
or other concrete deliverables.

## Inputs

User focus, if provided:

$ARGUMENTS

## Method

1. Identify what success means from the latest user request and available
   project or workflow documentation.
2. Choose checks that directly exercise the changed behavior or artifact.
3. Run the checks when tools are available. If a check cannot be run, explain
   the environmental blocker instead of implying it passed.
4. Include at least one adversarial probe when applicable: boundary input,
   malformed input, idempotency, concurrency, orphan reference, regression,
   or consistency check.
5. Report commands/actions run, key output or observations, and expected vs.
   actual results.

End with exactly one line:

VERDICT: PASS
or
VERDICT: FAIL
or
VERDICT: PARTIAL";

/// The host's skills plus the built-ins they do not override (by name,
/// case-insensitively), sorted by lowercase name.
pub(crate) fn with_builtins(mut skills: Vec<Skill>) -> Vec<Skill> {
    let builtin = verify();
    if !(skills.iter()).any(|skill| skill.name.eq_ignore_ascii_case(&builtin.name)) {
        skills.push(builtin);
    }
    skills.sort_by_key(|skill| skill.name.to_lowercase());
    skills
}

/// Wrap `text` in the `<system-reminder>` envelope.
pub(crate) fn system_reminder(text: &str) -> String {
    format!("<system-reminder>\n{text}\n</system-reminder>")
}

/// The reminders that put back the body of every skill the model loaded in
/// `history`: one per distinct (name, substituted body), in load order.
///
/// A load is a `Skill` call whose result is not an error; its result text
/// is the substituted body the model saw.
pub(crate) fn reminders(history: &[Arc<Message>]) -> Vec<String> {
    let mut texts: Vec<String> = Vec::new();
    for (at, message) in history.iter().enumerate() {
        if message.role != Role::Assistant {
            continue;
        }
        for call in message.tool_uses().filter(|call| call.name == SKILL_TOOL) {
            let Some(name) = call.input.get("skill").and_then(|v| v.as_str()) else {
                continue;
            };
            let name = name.strip_prefix('/').unwrap_or(name);
            let Some(body) = loaded_body(&history[at + 1..], &call.id) else {
                continue;
            };
            let text = system_reminder(&format!(
                "Below is the body of a previously invoked skill named '{name}'.\n\n{body}"
            ));
            if !texts.contains(&text) {
                texts.push(text);
            }
        }
    }
    texts
}

/// The text of the successful result of call `id` among `later` messages.
fn loaded_body(later: &[Arc<Message>], id: &str) -> Option<String> {
    let results = later.iter().take_while(|m| m.role == Role::User);
    let block = results
        .flat_map(|m| &m.content)
        .find_map(|block| match block {
            ContentBlock::ToolResult(result) if result.tool_use_id == id => Some(result),
            _ => None,
        })?;
    let text = block.content.iter().filter_map(|part| match part {
        ToolResultPart::Text(text) => Some(text.text.as_str()),
        ToolResultPart::Image(_) => None,
    });
    (!block.is_error).then(|| text.collect())
}
