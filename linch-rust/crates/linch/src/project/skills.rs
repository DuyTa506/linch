//! [`load_skills`]: each `skills/<name>/SKILL.md` of a config directory.

use std::collections::HashSet;
use std::fs;
use std::io::ErrorKind;
use std::path::Path;

use linch_core::Skill;
use yaml_rust2::Yaml;

use super::frontmatter::{self, get};
use super::{NOT_FOLLOWED, SkipReason, Skipped, entries, name_error, symlink, valid_name};

/// Load the skills in `config_dir/skills`, one per directory holding a
/// `SKILL.md`.
///
/// A skill is named by its frontmatter `name` (trimmed), else by its
/// directory; the name must match `^[a-z0-9][a-z0-9_-]*$`, ignoring case.
/// `description` is required and trimmed. The optional fields are
/// `when_to_use`, `argument_hint`, `model` and `version` (non-empty
/// strings), `arguments` (names matching `^[a-z_][a-z0-9_]*$`, ignoring
/// case, none of them `ARGUMENTS`, no duplicates), `allowed_tools` (names
/// without spaces) and `disable_model_invocation` (a boolean). The
/// skill's directory is the path under `config_dir`, as given.
///
/// Blocking: call it off the async loop.
///
/// Args:
///     config_dir: Directory holding `skills/`; without one nothing loads.
///     tool_names: Names of the agent's tools, which no skill may take.
///
/// Returns:
///     The skills sorted by name, and in directory order each directory
///     left out: a symlink or a symlinked `SKILL.md` (never followed), one
///     without a `SKILL.md`, an unreadable one, a bad frontmatter, or a
///     name a tool or an earlier skill already has. A symlinked `skills/`
///     alone is skipped; nothing at all when `skills/` cannot be listed.
pub fn load_skills(config_dir: &Path, tool_names: &HashSet<String>) -> (Vec<Skill>, Vec<Skipped>) {
    let root = config_dir.join("skills");
    if let Some(link) = symlink(&root) {
        return (Vec::new(), vec![link]);
    }
    let Some(Ok(entries)) = entries(&root) else {
        return (Vec::new(), Vec::new());
    };
    let mut skills: Vec<Skill> = Vec::new();
    let mut skipped = Vec::new();
    let mut seen = HashSet::new();
    for entry in entries {
        let path = entry.path();
        let mut skip = |reason, detail: String| {
            skipped.push(Skipped {
                path: path.clone(),
                reason,
                detail,
            });
        };
        let kind = match entry.file_type() {
            Ok(kind) if kind.is_symlink() => {
                skip(SkipReason::IoError, NOT_FOLLOWED.into());
                continue;
            }
            Ok(kind) => kind,
            Err(error) => {
                skip(SkipReason::IoError, error.to_string());
                continue;
            }
        };
        if !kind.is_dir() {
            continue;
        }
        let skill_md = path.join("SKILL.md");
        if symlink(&skill_md).is_some() {
            skip(SkipReason::IoError, NOT_FOLLOWED.into());
            continue;
        }
        let raw = match fs::read_to_string(skill_md) {
            Ok(raw) => raw,
            Err(error) if error.kind() == ErrorKind::NotFound => {
                skip(SkipReason::MissingSkillMd, "SKILL.md not found".into());
                continue;
            }
            Err(error) => {
                skip(SkipReason::IoError, error.to_string());
                continue;
            }
        };
        let dir_name = entry.file_name().to_string_lossy().into_owned();
        let skill = match parse(&raw, &dir_name, &path) {
            Ok(skill) => skill,
            Err(detail) => {
                skip(SkipReason::InvalidFrontmatter, detail);
                continue;
            }
        };
        if tool_names.contains(&skill.name) {
            let detail = format!("skill name '{}' collides with a tool", skill.name);
            skip(SkipReason::NameCollisionTool, detail);
        } else if !seen.insert(skill.name.clone()) {
            let detail = format!("skill name '{}' already loaded", skill.name);
            skip(SkipReason::NameCollisionSkill, detail);
        } else {
            skills.push(skill);
        }
    }
    skills.sort_by(|a, b| a.name.cmp(&b.name));
    (skills, skipped)
}

/// The skill `raw` defines in `dir`, or what is wrong with its frontmatter.
fn parse(raw: &str, dir_name: &str, dir: &Path) -> Result<Skill, String> {
    let (doc, body) = frontmatter::parse(raw).map_err(|error| match error {
        frontmatter::Error::Missing => frontmatter::MISSING.to_owned(),
        frontmatter::Error::Invalid(detail) => detail,
    })?;
    let description = match get(&doc, "description") {
        Some(Yaml::String(text)) if !text.trim().is_empty() => text.trim().to_owned(),
        _ => {
            let detail = "frontmatter 'description' is required and must be a non-empty string";
            return Err(detail.into());
        }
    };
    let name = match get(&doc, "name") {
        None => dir_name.to_owned(),
        Some(Yaml::String(text)) if !text.trim().is_empty() => text.trim().to_owned(),
        Some(_) => return Err("frontmatter 'name' must be a non-empty string".into()),
    };
    if !valid_name(&name) {
        return Err(name_error("skill", &name));
    }
    let arguments = strings(
        &doc,
        "arguments",
        argument_name,
        "must match /^[a-z_][a-z0-9_]*$/i",
    )?;
    let mut seen = HashSet::new();
    for argument in arguments.iter().flatten() {
        if argument == "ARGUMENTS" {
            let detail = "frontmatter 'arguments' entry must not be 'ARGUMENTS' (reserved)";
            return Err(detail.into());
        }
        if !seen.insert(argument) {
            return Err(format!(
                "frontmatter 'arguments' contains duplicate '{argument}'"
            ));
        }
    }
    let disable_model_invocation = match get(&doc, "disable_model_invocation") {
        None => false,
        Some(Yaml::Boolean(value)) => *value,
        Some(_) => return Err("frontmatter 'disable_model_invocation' must be a boolean".into()),
    };
    let tool_name = |name: &str| !name.is_empty() && !name.chars().any(char::is_whitespace);
    let mut skill = Skill::new(name, dir.to_string_lossy(), description, body);
    skill.when_to_use = text(&doc, "when_to_use")?;
    skill.allowed_tools = strings(
        &doc,
        "allowed_tools",
        tool_name,
        "must look like tool names",
    )?;
    skill.arguments = arguments.unwrap_or_default();
    skill.argument_hint = text(&doc, "argument_hint")?;
    skill.model = text(&doc, "model")?;
    skill.version = text(&doc, "version")?;
    skill.disable_model_invocation = disable_model_invocation;
    Ok(skill)
}

/// Whether `name` matches `^[a-z_][a-z0-9_]*$`, ignoring case.
fn argument_name(name: &str) -> bool {
    let mut chars = name.chars();
    chars
        .next()
        .is_some_and(|c| c.is_ascii_alphabetic() || c == '_')
        && chars.all(|c| c.is_ascii_alphanumeric() || c == '_')
}

/// An optional non-empty string field.
fn text(doc: &Yaml, key: &str) -> Result<Option<String>, String> {
    match get(doc, key) {
        None => Ok(None),
        Some(Yaml::String(text)) if !text.is_empty() => Ok(Some(text.clone())),
        Some(_) => Err(format!("frontmatter '{key}' must be a non-empty string")),
    }
}

/// An optional list of strings that each pass `valid`.
fn strings(
    doc: &Yaml,
    key: &str,
    valid: impl Fn(&str) -> bool,
    rule: &str,
) -> Result<Option<Vec<String>>, String> {
    let Some(value) = get(doc, key) else {
        return Ok(None);
    };
    let Yaml::Array(items) = value else {
        return Err(format!("frontmatter '{key}' must be an array of strings"));
    };
    let mut list = Vec::with_capacity(items.len());
    for item in items {
        let Yaml::String(item) = item else {
            return Err(format!("frontmatter '{key}' must contain only strings"));
        };
        if !valid(item) {
            return Err(format!("frontmatter '{key}' entry '{item}' {rule}"));
        }
        list.push(item.clone());
    }
    Ok(Some(list))
}
