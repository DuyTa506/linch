//! A catalog change as the model reads it: Python Linch's notice text in
//! a `<system-reminder>`, with the structured fields the catalog folds.

use std::collections::BTreeMap;

use super::LiveSkill;
use crate::canonical::Digest;
use crate::message::CatalogChangeBlock;
use crate::provider::ToolDefinition;
use crate::skills::system_reminder;

/// The first line of every notice.
const HEADER: &str = "The available catalog changed during this session.";

/// The notice of what changed from `told` to `live`, tools and skills;
/// `None` when nothing did.
///
/// Added and changed tools are listed in `live` order, removed ones in
/// `told` order; skills by name. A changed tool's line shows its new
/// description.
pub(super) fn diff(
    told: &[ToolDefinition],
    live: &[ToolDefinition],
    told_skills: &BTreeMap<&str, Digest>,
    live_skills: &[LiveSkill],
) -> Option<CatalogChangeBlock> {
    let find = |rows: &[ToolDefinition], name: &str| rows.iter().any(|row| row.name == name);
    let fresh = |tool: &ToolDefinition| !find(told, &tool.name);
    let changed =
        |tool: &ToolDefinition| (told.iter()).any(|row| row.name == tool.name && row != tool);
    let added: Vec<String> = (live.iter().filter(|t| fresh(t)))
        .map(|tool| tool.name.clone())
        .collect();
    let removed: Vec<String> = (told.iter().filter(|t| !find(live, &t.name)))
        .map(|tool| tool.name.clone())
        .collect();
    let updated: Vec<&ToolDefinition> = live.iter().filter(|t| changed(t)).collect();
    let definitions: Vec<ToolDefinition> = (live.iter())
        .filter(|tool| fresh(tool) || changed(tool))
        .cloned()
        .collect();

    let mut skills_added = Vec::new();
    let mut skills_changed = Vec::new();
    for skill in live_skills {
        match told_skills.get(skill.stamp.name.as_str()) {
            None => skills_added.push(skill),
            Some(digest) if *digest != skill.stamp.digest => skills_changed.push(skill),
            Some(_) => {}
        }
    }
    let removed_skills: Vec<String> = (told_skills.keys())
        .filter(|name| !live_skills.iter().any(|skill| skill.stamp.name == **name))
        .map(|name| (*name).to_owned())
        .collect();

    let empty = added.is_empty() && removed.is_empty() && updated.is_empty();
    if empty && skills_added.is_empty() && skills_changed.is_empty() && removed_skills.is_empty() {
        return None;
    }
    let line = |name: &str, description: &str| format!("{name}: {description}");
    let tool_lines: Vec<String> = (updated.iter())
        .map(|tool| line(&tool.name, &tool.description))
        .collect();
    let skill_lines = |skills: &[&LiveSkill]| -> Vec<String> {
        (skills.iter())
            .map(|skill| line(&skill.stamp.name, &skill.description))
            .collect()
    };
    let mut lines = vec![HEADER.to_owned()];
    for (label, values) in [
        ("Added tools", added.clone()),
        ("Removed tools", removed.clone()),
        ("Updated tools", tool_lines),
        ("Added skills", skill_lines(&skills_added)),
        ("Removed skills", removed_skills.clone()),
        ("Updated skills", skill_lines(&skills_changed)),
    ] {
        if !values.is_empty() {
            lines.push(format!("{label}: {}", values.join(", ")));
        }
    }
    let mut skills: Vec<_> = (skills_added.iter().chain(&skills_changed))
        .map(|skill| skill.stamp.clone())
        .collect();
    skills.sort_by(|a, b| a.name.cmp(&b.name));
    Some(CatalogChangeBlock {
        text: system_reminder(&lines.join("\n")),
        added_tools: added,
        removed_tools: removed,
        tool_definitions: definitions,
        skills,
        removed_skills,
    })
}
