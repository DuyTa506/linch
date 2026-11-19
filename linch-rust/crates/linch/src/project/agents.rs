//! [`load_subagents`]: each `agents/<name>.md` of a config directory.

use std::collections::HashSet;
use std::fs;

use linch_core::{RunBudget, SubagentDefinition};
use yaml_rust2::Yaml;

use super::frontmatter::{self, get};
use super::{NOT_FOLLOWED, SkipReason, Skipped, entries, name_error, symlink, valid_name};

/// Load the subagent definitions in `config_dir/agents`, one per `.md`
/// file (the extension in any case).
///
/// A definition is named by its frontmatter `name`, else by its file name
/// without the extension; the name must match `^[a-z0-9][a-z0-9_-]*$`,
/// ignoring case. `description` is required. `tools`, a list or a
/// comma-separated string, names the tools the child may use; `Read(...)`
/// counts as `Read`, and a value of another type means every tool. The
/// body is the child's instructions. `max_tokens` (a positive integer)
/// and `max_cost_usd` (a positive number) cap each run of the agent, and
/// `warn_ratio` (0 to 1) sets where its warning fires; a value of another
/// type skips the file, never loads it uncapped.
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
///     When `agents/` is a symlink or cannot be listed, it alone is
///     skipped.
pub fn load_subagents(config_dir: &std::path::Path) -> (Vec<SubagentDefinition>, Vec<Skipped>) {
    let root = config_dir.join("agents");
    if let Some(link) = symlink(&root) {
        return (Vec::new(), vec![link]);
    }
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
                skip(SkipReason::IoError, NOT_FOLLOWED.into());
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
    definition.budget = budget(&doc).map_err(invalid)?;
    Ok(definition)
}

/// The cap the frontmatter keys `max_tokens`, `max_cost_usd` and
/// `warn_ratio` give each run of the agent; `None` when it sets neither
/// cap. A value that is not what the key takes is an error: a cap that was
/// meant must never be dropped, which would run the agent uncapped.
fn budget(doc: &Yaml) -> Result<Option<RunBudget>, String> {
    let number = |key: &str| -> Result<Option<f64>, String> {
        let bad = || format!("frontmatter '{key}' must be a positive number");
        let value = match get(doc, key) {
            None => return Ok(None),
            Some(Yaml::Integer(n)) => *n as f64,
            Some(Yaml::Real(text)) => text.parse::<f64>().map_err(|_| bad())?,
            Some(_) => return Err(bad()),
        };
        (value.is_finite() && value > 0.0)
            .then_some(Some(value))
            .ok_or_else(bad)
    };
    let max_tokens = match get(doc, "max_tokens") {
        None => None,
        Some(Yaml::Integer(n)) if *n > 0 => Some(*n as u64),
        Some(_) => return Err("frontmatter 'max_tokens' must be a positive integer".into()),
    };
    let max_cost_usd = number("max_cost_usd")?;
    let warn_ratio = number("warn_ratio")?;
    if warn_ratio.is_some_and(|ratio| ratio > 1.0) {
        return Err("frontmatter 'warn_ratio' must be at most 1".into());
    }
    if max_tokens.is_none() && max_cost_usd.is_none() {
        return match warn_ratio {
            Some(_) => Err("frontmatter 'warn_ratio' needs 'max_tokens' or 'max_cost_usd'".into()),
            None => Ok(None),
        };
    }
    let defaults = RunBudget::default();
    Ok(Some(RunBudget {
        max_tokens,
        max_cost_usd,
        warn_ratio: warn_ratio.unwrap_or(defaults.warn_ratio),
    }))
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
