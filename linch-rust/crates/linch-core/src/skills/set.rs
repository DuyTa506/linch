//! [`SkillSet`]: one version of an agent's skills, which a reload
//! replaces as a whole.

use super::{Overlays, Skill, listing, overlays, with_builtins};
use crate::canonical::{self, CanonicalError};
use crate::catalog::LiveSkill;
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
    /// The set of `skills` plus the built-ins.
    ///
    /// Raises:
    ///     CanonicalError: If a skill cannot be digested.
    pub(crate) fn new(skills: Vec<Skill>) -> Result<Self, CanonicalError> {
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
            listing: listing_block(&skills),
            overlays: overlays(&skills),
            listed,
            skills,
        })
    }
}

/// The skill listing as a cacheable system block; `None` when no skill is
/// visible to the model.
fn listing_block(skills: &[Skill]) -> Option<SystemBlock> {
    let listing = listing(skills);
    (!listing.is_empty()).then(|| SystemBlock {
        text: format!(
            "The following skills are available for use with the Skill tool:\n\n{listing}"
        ),
        cacheable: true,
    })
}
