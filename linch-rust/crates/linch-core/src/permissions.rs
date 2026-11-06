//! Permission rules and evaluation.
//!
//! A [`PermissionPolicy`] is plain serde data: a [`PermissionMode`], the
//! project root, and ordered [`PermissionLayer`]s of [`Rule`]s.
//! [`PermissionEngine::new`] validates it, and [`PermissionEngine::evaluate`]
//! turns one [`PermissionCall`] into a
//! [`PermissionDecision`](crate::PermissionDecision). Evaluation is
//! pure and synchronous: it never touches the filesystem.
//!
//! - Within a layer the first matching rule wins. Bash rules are the
//!   exception: from the first applicable one on, they are pooled and each
//!   command segment takes the decision of the first bash rule that matches
//!   it (any segment denied: deny; any asked: ask; all allowed: allow).
//! - Across layers a deny from any layer is final; otherwise the last layer
//!   that decided wins. When every layer abstains the mode default applies.
//! - A command with shell syntax the splitter cannot model (see
//!   [`shell::has_unparsed_construct`]) hits a floor first: a matching deny
//!   rule denies it, any other applicable bash deny rule makes it ask, and
//!   bash rules never allow it.
//! - Path rules match globs against the lexically normalized target. A
//!   target outside the project root never matches an allow rule, and
//!   under `accept_edits` such a write asks instead of being allowed by
//!   default. Symlinks are not resolved here; the execution-world file
//!   layer confines what is really opened.

mod engine;
mod glob;
mod path;
pub mod shell;
#[cfg(test)]
mod tests;
mod validate;

use std::collections::{BTreeMap, BTreeSet};

use serde::{Deserialize, Serialize};
use thiserror::Error;

use crate::canonical::{CanonicalError, to_canonical_string};
use crate::json::{JsonObject, JsonValue};

pub use engine::{PermissionCall, PermissionEngine};

/// Outcome a rule assigns to the calls it matches.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RuleDecision {
    /// Run the call.
    Allow,
    /// Refuse the call.
    Deny,
    /// Suspend until a host approves or denies the call.
    Ask,
}

/// Baseline decision when no rule matches. Read-scope calls and
/// [`PermissionPolicy::auto_allow`] tools are allowed in every mode.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Default, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PermissionMode {
    /// Ask for write and exec calls.
    #[default]
    Default,
    /// Allow write calls, except a write whose path target is outside the
    /// project root, which asks; ask for exec calls.
    AcceptEdits,
    /// Allow every call.
    SkipDangerous,
}

/// One permission rule. `role` admits only principals holding that role;
/// `unless_role` excludes principals holding it. A call without a principal
/// holds no roles.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum Rule {
    /// Match by tool name.
    Tool(ToolRule),
    /// Match by the target path of a file tool.
    Path(PathRule),
    /// Match by the command of a shell tool.
    Bash(BashRule),
}

/// Match a call by tool name.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ToolRule {
    /// A tool name, `*` for every tool, or `mcp__<server>__*` for every tool
    /// of one MCP server.
    pub tool: String,
    /// Decision when the rule matches.
    pub decision: RuleDecision,
    /// Only match the `Skill` tool loading this skill (`*` for any skill).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub skill: Option<String>,
    /// Role the principal must hold.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub role: Option<String>,
    /// Role that disables the rule.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub unless_role: Option<String>,
}

/// Match a file tool call by its target path.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PathRule {
    /// Globs relative to the project root, or absolute when they start with
    /// `/`. `*` and `?` stay inside one segment; a whole `**` segment spans
    /// any number of segments (at least one when it is last). A leading `!`
    /// inverts a pattern. The rule matches when any pattern matches.
    pub paths: Vec<String>,
    /// Decision when the rule matches.
    pub decision: RuleDecision,
    /// Tools the rule covers. When absent: every write-scope tool that has a
    /// path field.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tools: Option<Vec<String>>,
    /// Role the principal must hold.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub role: Option<String>,
    /// Role that disables the rule.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub unless_role: Option<String>,
}

/// Match shell command segments. A pattern matches a segment when it matches
/// the whole segment as a glob (`*`, `?`, `[...]`, with `*` spanning
/// anything) or when its words are a prefix of the segment's words.
///
/// Word-prefix matching accepts any further arguments: `npm install`
/// matches `npm install lodash`, and `rm -rf /tmp` matches
/// `rm -rf /tmp /`. Words are compared after shell quote and escape
/// removal, and redirections in the segment are part of its words, so
/// `echo` also matches `echo x > file`. Write allow patterns with that in
/// mind.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BashRule {
    /// Command patterns; the rule matches when any of them does.
    pub patterns: Vec<String>,
    /// Decision when the rule matches.
    pub decision: RuleDecision,
    /// Role the principal must hold.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub role: Option<String>,
    /// Role that disables the rule.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub unless_role: Option<String>,
}

impl ToolRule {
    /// Rule matching `tool` with no skill or role conditions.
    pub fn new(tool: impl Into<String>, decision: RuleDecision) -> Self {
        Self {
            tool: tool.into(),
            decision,
            skill: None,
            role: None,
            unless_role: None,
        }
    }
}

impl PathRule {
    /// Rule over the default tools with no role conditions.
    pub fn new(paths: impl IntoIterator<Item = impl Into<String>>, decision: RuleDecision) -> Self {
        Self {
            paths: paths.into_iter().map(Into::into).collect(),
            decision,
            tools: None,
            role: None,
            unless_role: None,
        }
    }
}

impl BashRule {
    /// Rule with no role conditions.
    pub fn new(
        patterns: impl IntoIterator<Item = impl Into<String>>,
        decision: RuleDecision,
    ) -> Self {
        Self {
            patterns: patterns.into_iter().map(Into::into).collect(),
            decision,
            role: None,
            unless_role: None,
        }
    }
}

impl From<ToolRule> for Rule {
    fn from(rule: ToolRule) -> Self {
        Self::Tool(rule)
    }
}

impl From<PathRule> for Rule {
    fn from(rule: PathRule) -> Self {
        Self::Path(rule)
    }
}

impl From<BashRule> for Rule {
    fn from(rule: BashRule) -> Self {
        Self::Bash(rule)
    }
}

impl Rule {
    /// The decision this rule assigns.
    pub fn decision(&self) -> RuleDecision {
        match self {
            Self::Tool(r) => r.decision,
            Self::Path(r) => r.decision,
            Self::Bash(r) => r.decision,
        }
    }

    fn roles(&self) -> (Option<&str>, Option<&str>) {
        let (role, unless) = match self {
            Self::Tool(r) => (&r.role, &r.unless_role),
            Self::Path(r) => (&r.role, &r.unless_role),
            Self::Bash(r) => (&r.role, &r.unless_role),
        };
        (role.as_deref(), unless.as_deref())
    }
}

/// One named source of rules, such as `defaults`, `project` or `runtime`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PermissionLayer {
    /// Label used in decision reasons.
    pub name: String,
    /// Rules in first-match order.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub rules: Vec<Rule>,
}

impl PermissionLayer {
    /// Layer named `name` holding `rules`.
    pub fn new(name: impl Into<String>, rules: impl IntoIterator<Item = Rule>) -> Self {
        Self {
            name: name.into(),
            rules: rules.into_iter().collect(),
        }
    }
}

/// Where a file tool keeps its target path.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PathField {
    /// Input field holding the path.
    pub field: String,
    /// Whether a missing or blank field means the working directory.
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub default_to_cwd: bool,
    /// Whether a leading `/` means the working directory (virtual
    /// filesystem paths) rather than the filesystem root.
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub virtual_root: bool,
}

/// Permission configuration for one agent.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PermissionPolicy {
    /// Baseline when no rule matches.
    #[serde(default)]
    pub mode: PermissionMode,
    /// Absolute root that relative paths and path patterns resolve against.
    pub project_root: String,
    /// Layers from least to most specific.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub layers: Vec<PermissionLayer>,
    /// Tools allowed by default in every mode (rules still apply first).
    #[serde(default, skip_serializing_if = "BTreeSet::is_empty")]
    pub auto_allow: BTreeSet<String>,
    /// Path fields by tool name, added to (and overriding) the built-in
    /// table: `Read`/`Write`/`Edit` use `file_path`; `Glob`/`Grep` use
    /// `path`; `read_file`/`write_file`/`edit_file` use virtual `path`; `ls`
    /// uses virtual `prefix`.
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub path_fields: BTreeMap<String, PathField>,
    /// Command fields by tool name, added to the built-in `Bash` ->
    /// `command`. Bash rules and the unparsed floor apply to these tools.
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub command_fields: BTreeMap<String, String>,
}

impl PermissionPolicy {
    /// Policy with `mode`, `project_root`, and nothing else.
    pub fn new(mode: PermissionMode, project_root: impl Into<String>) -> Self {
        Self {
            mode,
            project_root: project_root.into(),
            layers: Vec::new(),
            auto_allow: BTreeSet::new(),
            path_fields: BTreeMap::new(),
            command_fields: BTreeMap::new(),
        }
    }
}

/// A [`PermissionPolicy`] that cannot be evaluated as written.
#[derive(Debug, Clone, PartialEq, Eq, Error)]
pub enum PolicyError {
    /// `project_root` does not start with `/`.
    #[error("project_root must be an absolute path, got {root:?}")]
    RelativeRoot {
        /// The configured root.
        root: String,
    },
    /// A path or command field name is blank.
    #[error("input field for tool {tool:?} must be non-blank")]
    BlankField {
        /// Tool whose field is blank.
        tool: String,
    },
    /// One rule is malformed.
    #[error("layer {layer:?}, rule {index}: {problem}")]
    Rule {
        /// Layer holding the rule.
        layer: String,
        /// Position of the rule in its layer.
        index: usize,
        /// What is wrong with it.
        problem: RuleProblem,
    },
}

/// Why a rule was rejected. Each case is a rule that would otherwise never
/// match, which for a deny rule means a silent hole in the policy.
#[derive(Debug, Clone, PartialEq, Eq, Error)]
pub enum RuleProblem {
    /// A name or pattern is empty or whitespace.
    #[error("{field} must be non-blank")]
    Blank {
        /// The offending field.
        field: &'static str,
    },
    /// A list that needs entries is empty.
    #[error("{field} must not be empty")]
    Empty {
        /// The offending field.
        field: &'static str,
    },
    /// A tool pattern is not a name, `*`, or `mcp__<server>__*`.
    #[error("tool pattern {pattern:?} must be a name, \"*\", or \"mcp__<server>__*\"")]
    ToolPattern {
        /// The rejected pattern.
        pattern: String,
    },
    /// A skill condition on a rule that cannot match the `Skill` tool.
    #[error("a skill condition needs tool \"Skill\" or \"*\"")]
    SkillTool,
    /// A path pattern is empty or has a `..` segment.
    #[error("path pattern {pattern:?} is empty or has a \"..\" segment")]
    PathPattern {
        /// The rejected pattern.
        pattern: String,
    },
    /// A path rule names a tool with no path field.
    #[error("tool {tool:?} has no path field")]
    NoPathField {
        /// The tool without a path field.
        tool: String,
    },
}

/// Stable key for one proposed call: `tool_name:` followed by the input in
/// Linch canonical JSON. It ignores object key order and the per-call
/// `tool_use_id`, so it identifies the same call across provider retries and
/// resumes (durable approvals, LoopGuard, ToolCache).
///
/// # Errors
///
/// [`CanonicalError::DepthExceeded`] when the input nests deeper than
/// [`crate::canonical::MAX_ENCODE_DEPTH`].
pub fn permission_decision_key(
    tool_name: &str,
    input: &JsonObject,
) -> Result<String, CanonicalError> {
    let input = to_canonical_string(&JsonValue::Object(input.clone()))?;
    Ok(format!("{tool_name}:{input}"))
}
