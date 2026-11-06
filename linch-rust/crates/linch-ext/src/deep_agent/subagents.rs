//! The deep agent's subagent roster: `researcher`, `planner`,
//! `implementer` and `verification`, with Python Linch's descriptions,
//! tool lists and instructions.

use linch_core::SubagentDefinition;

/// The four definitions, in Python's order: researcher, planner,
/// implementer, verification. The deep agent's `verification` takes the
/// place of the core's built-in one, which has `Bash`; this one only reads.
pub fn deep_agent_subagents() -> Vec<SubagentDefinition> {
    vec![researcher(), planner(), implementer(), verification()]
}

/// Read-heavy exploration and evidence gathering.
fn researcher() -> SubagentDefinition {
    let mut definition = SubagentDefinition::new(
        "researcher",
        "Read-heavy specialist for exploration, evidence gathering, and synthesis.",
        RESEARCHER_BODY,
    );
    definition.tools = Some(
        [
            "Read",
            "Glob",
            "Grep",
            "TaskList",
            "TaskGet",
            "ls",
            "read_file",
        ]
        .map(String::from)
        .to_vec(),
    );
    definition
}

const RESEARCHER_BODY: &str = r#"You are a research subagent. Gather evidence for the delegated task
without modifying project files or durable state unless explicitly instructed.

Work style:
- Start by restating the exact question you are answering.
- Search broadly, then narrow to the most relevant evidence.
- Prefer concrete file paths, commands, outputs, citations, or data points.
- Use the virtual filesystem to inspect offloaded outputs or scratch notes.
- Return a concise synthesis with findings, evidence, and open questions.

Do not perform implementation work. Do not claim verification unless you
actually ran or inspected the relevant check."#;

/// A read-only architect that writes an ordered plan.
fn planner() -> SubagentDefinition {
    let mut definition = SubagentDefinition::new(
        "planner",
        "Read-only architect that surveys the codebase and produces an ordered implementation plan.",
        PLANNER_BODY,
    );
    definition.tools = Some(
        [
            "Read",
            "Glob",
            "Grep",
            "TaskList",
            "TaskGet",
            "ls",
            "read_file",
        ]
        .map(String::from)
        .to_vec(),
    );
    definition
}

const PLANNER_BODY: &str = r#"You are a planning subagent. Survey the relevant code and produce a
concrete implementation plan. Do not write code or modify project files.

Work style:
- Start by restating the exact goal you are planning for.
- Use read/search tools to identify the files, functions, and patterns
  involved. Cite exact file paths and line numbers.
- Output an ordered plan: numbered steps, each with a file path (or
  'new file'), what changes, and the done-criterion for that step.
- Name risks, unknowns, and constraints explicitly.
- Optionally save the plan to /memories/<name>.md for durability.

Do not implement anything. If a step is unclear or unsafe, flag it
instead of guessing."#;

/// Scoped code or artifact changes.
fn implementer() -> SubagentDefinition {
    let mut definition = SubagentDefinition::new(
        "implementer",
        "Focused implementation specialist for scoped code or artifact changes.",
        IMPLEMENTER_BODY,
    );
    definition.tools = Some(
        [
            "Read",
            "Edit",
            "Write",
            "Glob",
            "Grep",
            "Bash",
            "TaskList",
            "TaskGet",
            "TaskUpdate",
            "ls",
            "read_file",
            "write_file",
            "edit_file",
        ]
        .map(String::from)
        .to_vec(),
    );
    definition
}

const IMPLEMENTER_BODY: &str = r#"You are an implementation subagent. Complete the delegated change
with minimal, scoped edits and clear verification.

Work style:
- Read relevant files before changing them.
- Keep edits tightly scoped to the delegated task.
- Update task state when the parent provided task ids.
- Use the virtual filesystem for scratch plans or large intermediate notes.
- Run the most relevant local checks when available and safe.
- Return changed files, verification performed, and any residual risk.

Do not broaden the task. If the requested change is unsafe or ambiguous,
report the blocker instead of guessing."#;

/// A read-only adversarial reviewer that ends with a verdict.
fn verification() -> SubagentDefinition {
    let mut definition = SubagentDefinition::new(
        "verification",
        "Read-only adversarial reviewer that inspects focused evidence and reports a verdict.",
        VERIFICATION_BODY,
    );
    definition.tools = Some(
        [
            "Read",
            "Glob",
            "Grep",
            "TaskList",
            "TaskGet",
            "ls",
            "read_file",
        ]
        .map(String::from)
        .to_vec(),
    );
    definition
}

const VERIFICATION_BODY: &str = r#"You are a verification subagent. Try to disprove that the delegated
outcome is correct. Do not modify project files or durable state.

Inspect the relevant implementation and available check evidence,
and probe important failure cases. Finish with exactly one
verdict line: VERDICT: PASS, VERDICT: FAIL, or VERDICT: PARTIAL.
Then list evidence and any residual risk. Never claim a check ran
unless you observed its result."#;
