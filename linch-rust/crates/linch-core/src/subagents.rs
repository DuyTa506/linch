//! Foreground subagents: the `Subagent` tool runs a focused task as a
//! durable child run and returns its answer as the tool result.
//!
//! A host passes the [`SubagentDefinition`]s it loaded (the `linch` facade
//! reads `.linch/agents/*.md`) to [`crate::AgentBuilder::subagents`]. That
//! turns subagents on: the built-in [`verification`] agent joins them
//! unless one has its name (case-insensitively), a general-purpose agent
//! is used when the model names no type, and the `Subagent` tool is
//! registered.
//!
//! A call runs one child run, recorded with the parent's run as its
//! `parent_run_id`:
//! - its id, and its session's, is the call's
//!   [`idempotency_key`](crate::ToolContext::idempotency_key), so the
//!   parent finds it again;
//! - its session starts empty, and its system prompt is the parent's (with
//!   the skill listing when the child has the `Skill` tool), then the
//!   definition's body;
//! - it sees the parent's tools minus `Subagent` (a child cannot spawn),
//!   only those the definition names when it names any;
//! - it cannot suspend: a call that needs approval is denied, unless a
//!   rule or a `permission_request` hook allows it;
//! - each of its events reaches the parent's log wrapped in a `subagent`
//!   event, before the call's end, and its final text is the tool result.
//!
//! The tool is exec-scoped. A call runs alongside other read calls only
//! when it names a type whose definition lists its tools and every one of
//! them is a registered read-scope tool.
//!
//! When the parent resumes with a call whose start is durable and whose end
//! is not, the call runs again: it resumes the child run (the child's own
//! journal never pays a model call twice), or reads the result of a child
//! that already finished.

mod run;
#[cfg(test)]
mod tests;
mod tool;

use serde::Serialize;

pub(crate) use tool::SubagentTool;

/// Name of the tool that runs a subagent.
pub const SUBAGENT_TOOL: &str = "Subagent";

/// A kind of subagent the model can ask for.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct SubagentDefinition {
    /// Name the model passes as `subagent_type`.
    pub name: String,
    /// One line shown in the `Subagent` tool's description.
    pub description: String,
    /// The tools the child may use; `None` (or a `*` entry) for all of the
    /// parent's.
    pub tools: Option<Vec<String>>,
    /// Instructions appended to the child's system prompt.
    pub body: String,
}

impl SubagentDefinition {
    /// A definition named `name` with access to every tool.
    pub fn new(
        name: impl Into<String>,
        description: impl Into<String>,
        body: impl Into<String>,
    ) -> Self {
        Self {
            name: name.into(),
            description: description.into(),
            tools: None,
            body: body.into(),
        }
    }
}

/// The built-in `verification` agent: tries to break completed work with
/// read-only checks and ends with a `VERDICT:` line. Its tools are `Read`,
/// `Glob`, `Grep` and `Bash`, those of them the parent has.
pub fn verification() -> SubagentDefinition {
    let mut definition = SubagentDefinition::new(
        "verification",
        "Verify non-trivial completed work with evidence and a PASS/FAIL/PARTIAL verdict.",
        VERIFICATION_BODY,
    );
    definition.tools = Some(["Read", "Glob", "Grep", "Bash"].map(String::from).to_vec());
    definition
}

const VERIFICATION_BODY: &str = "You are a verification specialist. Your job is to try to break the
work, not to confirm it by reading instructions or source alone.

=== CRITICAL: DO NOT MODIFY THE PROJECT ===
You are strictly prohibited from creating, modifying, or deleting project files
or workspace artifacts.
Do not install dependencies or packages. Do not run git write operations such as
`git add`, `git commit`, `git push`, `git reset`, `git checkout`, or branch writes.
You may use Bash for read-only commands and project-approved build/test commands.
If an ephemeral harness is required, write it only under /tmp or $TMPDIR
and clean up.

You will receive the original user task, artifacts or files changed, approach
taken, and any specific checks the parent wants verified. Use the actual tools
available to you.

Verification baseline:
1. Read README, docs, or project config to identify build/test commands.
2. Run applicable builds, tests, linters, or type checks.
3. Exercise the changed behavior directly with representative inputs.
4. Include at least one adversarial probe such as a boundary, malformed input,
   idempotency, orphan reference, concurrency, or regression check when applicable.

Every reported check must include the exact command run, relevant output observed,
expected vs actual behavior, and PASS or FAIL for that check. If the environment
prevents a check, explain what blocked it and what was still verified.

End with exactly one final line:
VERDICT: PASS
or
VERDICT: FAIL
or
VERDICT: PARTIAL";

/// The general-purpose agent for a call that names no type.
fn general_purpose() -> SubagentDefinition {
    SubagentDefinition::new(
        "_default",
        "General-purpose subagent with full tool access.",
        "You are a focused subagent. The parent agent delegated a single task to you.
Use the tools available to complete the task fully. When done, respond with a
concise text summary of what you did and any key findings \u{2014} the parent will
relay this to the user, so the response only needs the essentials.",
    )
}

/// The definitions an agent offers: the host's plus the built-ins they do
/// not override, sorted by name, and the general-purpose one.
#[derive(Debug, Serialize)]
pub(crate) struct Roster {
    listed: Vec<SubagentDefinition>,
    general: SubagentDefinition,
}

impl Roster {
    pub(crate) fn new(mut definitions: Vec<SubagentDefinition>) -> Self {
        let builtin = verification();
        if !(definitions.iter()).any(|d| d.name.eq_ignore_ascii_case(&builtin.name)) {
            definitions.push(builtin);
        }
        definitions.sort_by(|a, b| a.name.cmp(&b.name));
        Self {
            listed: definitions,
            general: general_purpose(),
        }
    }

    /// The definitions the tool description lists.
    pub(crate) fn listed(&self) -> &[SubagentDefinition] {
        &self.listed
    }

    /// The definition for a `subagent_type`: the general-purpose one when
    /// it is blank, `general-purpose` or `_default`, else the one of that name
    /// (case-insensitively).
    pub(crate) fn get(&self, requested: Option<&str>) -> Option<&SubagentDefinition> {
        match requested.map(str::trim) {
            None | Some("" | "general-purpose" | "_default") => Some(&self.general),
            Some(name) => self.named(name),
        }
    }

    /// The listed definition named `name` (case-insensitively).
    pub(crate) fn named(&self, name: &str) -> Option<&SubagentDefinition> {
        (self.listed.iter()).find(|d| d.name.eq_ignore_ascii_case(name))
    }
}
