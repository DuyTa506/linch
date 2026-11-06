//! [`listing`]: the skills the model may invoke, for the system prompt.

use super::Skill;

/// Longest description of one entry, in characters.
const MAX_PER_ENTRY: usize = 250;
/// Shortest description worth keeping when the budget squeezes them.
const MIN_DESC_LEN: usize = 20;
/// Characters the whole listing may take.
const BUDGET: usize = 8000;

/// One `- name: description` line per skill the model may invoke, sorted by
/// lowercase name; empty when there is none.
///
/// A description is the skill's `description`, then ` — when_to_use` and
/// ` (args: argument_hint)` when set, cut at 250 characters. When the lines
/// pass the 8000-character budget, every description is cut to an equal
/// share of it; when that share is under 20 characters, the listing keeps
/// the names only.
pub fn listing(skills: &[Skill]) -> String {
    let mut visible: Vec<&Skill> = (skills.iter())
        .filter(|skill| !skill.disable_model_invocation)
        .collect();
    visible.sort_by_key(|skill| skill.name.to_lowercase());
    if visible.is_empty() {
        return String::new();
    }
    let descriptions: Vec<String> = (visible.iter())
        .map(|skill| cap(&entry(skill), MAX_PER_ENTRY))
        .collect();
    let render = |descriptions: &[String]| -> String {
        let lines = visible.iter().zip(descriptions);
        let lines: Vec<String> = lines
            .map(|(skill, description)| format!("- {}: {description}", skill.name))
            .collect();
        lines.join("\n")
    };
    let full = render(&descriptions);
    if full.chars().count() <= BUDGET {
        return full;
    }
    let overhead: usize = (visible.iter())
        .map(|skill| format!("- {}: ", skill.name).chars().count())
        .sum();
    let newlines = visible.len() - 1;
    let share = BUDGET.saturating_sub(overhead + newlines) / visible.len();
    if share < MIN_DESC_LEN {
        let names: Vec<String> = (visible.iter())
            .map(|skill| format!("- {}", skill.name))
            .collect();
        return names.join("\n");
    }
    let capped: Vec<String> = (descriptions.iter())
        .map(|description| cap(description, share))
        .collect();
    render(&capped)
}

fn entry(skill: &Skill) -> String {
    let mut description = skill.description.clone();
    if let Some(when) = &skill.when_to_use {
        description.push_str(&format!(" \u{2014} {when}"));
    }
    if let Some(hint) = &skill.argument_hint {
        description.push_str(&format!(" (args: {hint})"));
    }
    description
}

/// `text` cut to `limit` characters, the last one an ellipsis.
fn cap(text: &str, limit: usize) -> String {
    if text.chars().count() <= limit {
        return text.to_owned();
    }
    if limit <= 1 {
        return "\u{2026}".into();
    }
    let kept: String = text.chars().take(limit - 1).collect();
    format!("{kept}\u{2026}")
}
