//! Skills and subagents defined in a project's config directory (`.linch`
//! by default): `skills/<name>/SKILL.md` and `agents/<name>.md`, each a
//! YAML frontmatter between `---` lines, then a markdown body.
//!
//! [`AgentConfig::open_with`](crate::AgentConfig::open_with) loads them
//! when the config turns skills or subagents on. A host that builds its
//! agent with [`AgentBuilder`](linch_core::AgentBuilder) calls
//! [`load_skills`] and [`load_subagents`] itself and passes the result to
//! [`AgentBuilder::skills`](linch_core::AgentBuilder::skills) and
//! [`AgentBuilder::subagents`](linch_core::AgentBuilder::subagents).
//!
//! A loader never fails: each definition it cannot use is reported as
//! [`Skipped`], and the others load. Both read the file system, so call
//! them off the async loop.

mod agents;
mod frontmatter;
mod skills;

use std::fs::{self, DirEntry};
use std::io;
use std::path::{Path, PathBuf};

use linch_core::{Skill, SubagentDefinition, ToolRegistry};

pub use agents::load_subagents;
pub use skills::load_skills;

/// A definition a loader left out, and why.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Skipped {
    /// The skill directory or subagent file.
    pub path: PathBuf,
    /// Why it was left out.
    pub reason: SkipReason,
    /// What exactly is wrong, for a person to read.
    pub detail: String,
}

/// Why a loader left a definition out.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SkipReason {
    /// A skill directory has no `SKILL.md`.
    MissingSkillMd,
    /// A subagent file does not start with a `---` line.
    MissingFrontmatter,
    /// The frontmatter does not parse, or a field is missing or wrong.
    InvalidFrontmatter,
    /// It is a symlink (never followed) or could not be read.
    IoError,
    /// A skill has the name of one of the agent's tools.
    NameCollisionTool,
    /// A skill has the name of a skill loaded before it.
    NameCollisionSkill,
    /// A subagent has the name of one loaded before it (case-insensitively).
    NameCollision,
}

/// What [`load`] found: the definitions of each kind turned on, and every
/// one skipped.
#[derive(Default)]
pub(crate) struct Loaded {
    pub(crate) skills: Option<Vec<Skill>>,
    pub(crate) subagents: Option<Vec<SubagentDefinition>>,
    pub(crate) skipped: Vec<Skipped>,
}

/// Load the skills and subagents turned on from `dir` off the async loop.
/// `dir` defaults to `.linch`; a relative one resolves against `root` (the
/// workspace), else the process's working directory. No skill may take
/// the name of a tool in `tools` at this point.
pub(crate) async fn load(
    dir: Option<PathBuf>,
    root: Option<&str>,
    skills: bool,
    subagents: bool,
    tools: &ToolRegistry,
) -> Loaded {
    let dir = dir.unwrap_or_else(|| PathBuf::from(".linch"));
    let dir = match root {
        Some(root) => Path::new(root).join(dir),
        None => std::path::absolute(&dir).unwrap_or(dir),
    };
    let names = (tools.snapshot().definitions().into_iter())
        .map(|definition| definition.name)
        .collect();
    let scan = move || {
        let mut skipped = Vec::new();
        let skills = skills.then(|| {
            let (loaded, left) = load_skills(&dir, &names);
            skipped.extend(left);
            loaded
        });
        let subagents = subagents.then(|| {
            let (loaded, left) = load_subagents(&dir);
            skipped.extend(left);
            loaded
        });
        Loaded {
            skills,
            subagents,
            skipped,
        }
    };
    match tokio::task::spawn_blocking(scan).await {
        Ok(loaded) => loaded,
        Err(join) => std::panic::resume_unwind(join.into_panic()),
    }
}

/// The detail of a symlink a loader skipped.
const NOT_FOLLOWED: &str = "symlinks are not followed";

/// `path` skipped as a symlink when it is one. The check reads the link
/// itself, so a file swapped for a link after it remains a window.
fn symlink(path: &Path) -> Option<Skipped> {
    let linked = fs::symlink_metadata(path).is_ok_and(|meta| meta.file_type().is_symlink());
    linked.then(|| Skipped {
        path: path.to_owned(),
        reason: SkipReason::IoError,
        detail: NOT_FOLLOWED.into(),
    })
}

/// The entries of `root` sorted by name, or `None` when `root` is not a
/// directory.
fn entries(root: &Path) -> Option<io::Result<Vec<DirEntry>>> {
    if !root.is_dir() {
        return None;
    }
    let read = fs::read_dir(root).and_then(|dir| {
        let mut entries = dir.collect::<io::Result<Vec<_>>>()?;
        entries.sort_by_key(DirEntry::file_name);
        Ok(entries)
    });
    Some(read)
}

/// Whether `name` matches `^[a-z0-9][a-z0-9_-]*$`, ignoring case.
fn valid_name(name: &str) -> bool {
    let mut chars = name.chars();
    chars.next().is_some_and(|c| c.is_ascii_alphanumeric())
        && chars.all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-')
}

/// The detail for a name [`valid_name`] rejects.
fn name_error(kind: &str, name: &str) -> String {
    format!("{kind} name '{name}' must match /^[a-z0-9][a-z0-9_-]*$/i")
}
