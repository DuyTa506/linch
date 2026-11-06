//! [`load_subagents`]: each `agents/<name>.md` of a config directory.

use std::collections::HashSet;
use std::fs;

use linch_core::SubagentDefinition;
use yaml_rust2::Yaml;

use super::frontmatter::{self, get};
use super::{SkipReason, Skipped, entries, name_error, valid_name};

/// Load the subagent definitions in `config_dir/agents`, one per `.md`
/// file (the extension in any case).
///
/// A definition is named by its frontmatter `name`, else by its file name
/// without the extension; the name must match `^[a-z0-9][a-z0-9_-]*$`,
/// ignoring case. `description` is required. `tools`, a list or a
/// comma-separated string, names the tools the child may use; `Read(...)`
/// counts as `Read`, and a value of another type means every tool. The
/// body is the child's instructions.
///
/// Blocking: call it off the async loop.
///
/// Args:
///     config_dir: Directory holding `agents/`; without one nothing loads.
///
/// Returns:
///     The definitions sorted by name, and in file order each file left
///     out: a symlink (never followed), an unreadable one, a bad
///     frontmatter, or a name an earlier file already has (ignoring case).
///     When `agents/` cannot be listed, it alone is skipped.
pub fn load_subagents(config_dir: &std::path::Path) -> (Vec<SubagentDefinition>, Vec<Skipped>) {
    let root = config_dir.join("agents");
    let entries = match entries(&root) {
        None => return (Vec::new(), Vec::new()),
        Some(Ok(entries)) => entries,
        Some(Err(error)) => {
            let skipped = Skipped {
                path: root,
                reason: SkipReason::IoError,
                detail: error.to_string(),
            };
            return (Vec::new(), vec![skipped]);
        }
    };
    let mut definitions: Vec<SubagentDefinition> = Vec::new();
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
                skip(SkipReason::IoError, "symlinks are not followed".into());
                continue;
            }
            Ok(kind) => kind,
            Err(error) => {
                skip(SkipReason::IoError, error.to_string());
                continue;
            }
        };
        let file_name = entry.file_name().to_string_lossy().into_owned();
        if !kind.is_file() || !file_name.to_ascii_lowercase().ends_with(".md") {
            continue;
        }
        let raw = match fs::read_to_string(&path) {
            Ok(raw) => raw,
            Err(error) => {
                skip(SkipReason::IoError, error.to_string());
                continue;
            }
        };
        let base = &file_name[..file_name.len() - ".md".len()];
        let definition = match parse(&raw, base) {
            Ok(definition) => definition,
            Err((reason, detail)) => {
                skip(reason, detail);
                continue;
            }
        };
        if seen.insert(definition.name.to_lowercase()) {
            definitions.push(definition);
        } else {
            let detail = format!("agent name '{}' already loaded", definition.name);
            skip(SkipReason::NameCollision, detail);
        }
    }
    definitions.sort_by(|a, b| a.name.cmp(&b.name));
    (definitions, skipped)
}

/// The definition `raw` holds, or why it is skipped.
fn parse(raw: &str, base: &str) -> Result<SubagentDefinition, (SkipReason, String)> {
    let invalid = |detail: String| (SkipReason::InvalidFrontmatter, detail);
    let (doc, body) = frontmatter::parse(raw).map_err(|error| match error {
        frontmatter::Error::Missing => (
            SkipReason::MissingFrontmatter,
            frontmatter::MISSING.to_owned(),
        ),
        frontmatter::Error::Invalid(detail) => invalid(detail),
    })?;
    let description = match get(&doc, "description") {
        Some(Yaml::String(text)) if !text.trim().is_empty() => text.clone(),
        _ => {
            let detail = "frontmatter 'description' is required and must be a non-empty string";
            return Err(invalid(detail.into()));
        }
    };
    let name = match &doc["name"] {
        Yaml::BadValue => base.to_owned(),
        Yaml::String(text) if !text.trim().is_empty() => text.clone(),
        _ => {
            return Err(invalid(
                "frontmatter 'name' must be a non-empty string".into(),
            ));
        }
    };
    if !valid_name(&name) {
        return Err(invalid(name_error("agent", &name)));
    }
    let mut definition = SubagentDefinition::new(name, description, body);
    definition.tools = get(&doc, "tools").and_then(tools);
    Ok(definition)
}

/// The tool names in a `tools` value, or `None` (every tool) when it is
/// neither a list nor a string. List items that are not strings are
/// dropped.
fn tools(value: &Yaml) -> Option<Vec<String>> {
    let names: Vec<&str> = match value {
        Yaml::Array(items) => items.iter().filter_map(Yaml::as_str).collect(),
        Yaml::String(text) => text.split(',').map(str::trim).collect(),
        _ => return None,
    };
    let names = names
        .into_iter()
        .map(|name| name.split('(').next().unwrap_or_default().trim())
        .filter(|name| !name.is_empty())
        .map(str::to_owned)
        .collect();
    Some(names)
}
