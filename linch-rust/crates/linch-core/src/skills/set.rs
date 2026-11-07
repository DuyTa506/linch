//! [`SkillSet`]: one version of an agent's skills, which a reload
//! replaces as a whole.

use super::{Overlays, Skill, listing, overlays, with_builtins};
use crate::canonical::{self, CanonicalError};
use crate::catalog::LiveSkill;
use crate::json::{JsonObject, JsonValue};
use crate::message::{SkillStamp, SystemBlock};

/// The skills an agent has at one moment, with what is derived from them.
pub(crate) struct SkillSet {
    /// The host's skills and the built-ins they do not override, sorted.
    pub(crate) skills: Vec<Skill>,
    /// The listing block of a new session's system prompt; `None` when no
    /// skill is visible to the model.
    pub(crate) listing: Option<SystemBlock>,
    /// What each skill that sets one sets for the turn after it loads.
    pub(crate) overlays: Overlays,
    /// Each skill the model may invoke, as the catalog tells it.
    pub(crate) listed: Vec<LiveSkill>,
}

impl SkillSet {
    /// The set of `skills` plus the built-ins, listed within the budget of
    /// a `context_window` (see [`listing`]).
    ///
    /// Raises:
    ///     CanonicalError: If a skill cannot be digested.
    pub(crate) fn new(skills: Vec<Skill>, context_window: u64) -> Result<Self, CanonicalError> {
        let skills = with_builtins(skills);
        let mut listed = Vec::new();
        for skill in skills
            .iter()
            .filter(|skill| !skill.disable_model_invocation)
        {
            let value = serde_json::to_value(skill).expect("a skill serializes");
            let stamp = SkillStamp {
                name: skill.name.clone(),
                digest: canonical::digest(&value)?,
            };
            let description = skill.description.clone();
            listed.push(LiveSkill { stamp, description });
        }
        listed.sort_by(|a, b| a.stamp.name.cmp(&b.stamp.name));
        Ok(Self {
            listing: listing_block(&skills, context_window),
            overlays: overlays(&skills),
            listed,
            skills,
        })
    }
}

impl SkillSet {
    /// What a `skills_loaded` event says of each skill, sorted by name as
    /// Python sorts them: its name and description, and its `when_to_use`
    /// and `argument_hint` when set.
    pub(crate) fn descriptors(&self) -> Vec<JsonObject> {
        let mut skills: Vec<&Skill> = self.skills.iter().collect();
        skills.sort_by(|a, b| a.name.cmp(&b.name));
        let descriptor = |skill: &Skill| {
            let mut descriptor = JsonObject::new();
            let text = |value: &str| JsonValue::String(value.to_owned());
            descriptor.insert("name".into(), text(&skill.name));
            descriptor.insert("description".into(), text(&skill.description));
            for (key, value) in [
                ("when_to_use", &skill.when_to_use),
                ("argument_hint", &skill.argument_hint),
            ] {
                if let Some(value) = value.as_deref().filter(|value| !value.is_empty()) {
                    descriptor.insert(key.into(), text(value));
                }
            }
            descriptor
        };
        skills.into_iter().map(descriptor).collect()
    }
}

/// The skill listing as a cacheable system block; `None` when no skill is
/// visible to the model.
fn listing_block(skills: &[Skill], context_window: u64) -> Option<SystemBlock> {
    let listing = listing(skills, Some(context_window));
    (!listing.is_empty()).then(|| block(&listing))
}

/// The listing block when tool search defers the skills: their names only.
pub(crate) fn deferred_listing(names: &[String]) -> SystemBlock {
    let names = names.join(", ");
    block(&format!("Deferred skills (use tool_search first): {names}"))
}

fn block(listing: &str) -> SystemBlock {
    SystemBlock {
        text: format!(
            "The following skills are available for use with the Skill tool:\n\n{listing}"
        ),
        cacheable: true,
    }
}
