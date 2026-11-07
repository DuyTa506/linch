//! What a loaded skill sets for the next model turn: the only tools
//! offered and the model, from its frontmatter.
//!
//! Nothing is stored for it: the overlay of a turn is read from the
//! provider view, so a resumed turn gets the one it had.

use std::collections::HashMap;
use std::sync::Arc;

use super::{SKILL_TOOL, Skill};
use crate::message::{ContentBlock, Message, Role};

/// What one skill sets for the model turn after it loads.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct Overlay {
    /// The only tools offered on that turn.
    pub(crate) allowed_tools: Option<Vec<String>>,
    /// The model of that turn.
    pub(crate) model: Option<String>,
}

/// The overlays of the skills that set one, by skill name.
pub(crate) type Overlays = HashMap<String, Overlay>;

/// The overlays `skills` set: a non-empty `allowed_tools`, a `model`, or
/// both.
pub(crate) fn overlays(skills: &[Skill]) -> Overlays {
    let overlay = |skill: &Skill| {
        let allowed_tools = (skill.allowed_tools.clone()).filter(|tools| !tools.is_empty());
        let model = skill.model.clone().filter(|model| !model.is_empty());
        let set = allowed_tools.is_some() || model.is_some();
        set.then(|| {
            (
                skill.name.clone(),
                Overlay {
                    allowed_tools,
                    model,
                },
            )
        })
    };
    skills.iter().filter_map(overlay).collect()
}

/// The overlay of the model turn that answers the last user message of
/// `view`: that of the last skill with one among the `Skill` calls of the
/// assistant message just before it whose result there is not an error.
///
/// A turn being resumed (its assistant message after that user message)
/// gets the overlay it was dispatched with.
pub(crate) fn overlay<'a>(view: &[Arc<Message>], overlays: &'a Overlays) -> Option<&'a Overlay> {
    let at = view
        .iter()
        .rposition(|message| message.role == Role::User)?;
    let calls = view[..at].last().filter(|m| m.role == Role::Assistant)?;
    let loaded = |id: &str| {
        view[at].content.iter().any(|block| {
            matches!(block, ContentBlock::ToolResult(result)
                if result.tool_use_id == id && !result.is_error)
        })
    };
    let calls = calls.tool_uses();
    let skills = calls.filter(|call| call.name == SKILL_TOOL && loaded(&call.id));
    let named = skills.filter_map(|call| {
        let name = call.input.get("skill")?.as_str()?;
        overlays.get(name.strip_prefix('/').unwrap_or(name))
    });
    named.last()
}

/// The model a skill names, for a run on `run_model`: with the run
/// model's one-million-token `[1m]` suffix when the run model has it.
pub fn model_override(model: &str, run_model: &str) -> String {
    match model.ends_with("[1m]") || !run_model.ends_with("[1m]") {
        true => model.to_owned(),
        false => format!("{model}[1m]"),
    }
}
