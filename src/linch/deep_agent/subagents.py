from linch.subagents.types import AgentDefinition, AgentFrontmatter

RESEARCHER_AGENT = AgentDefinition(
    name="researcher",
    file_path="<built-in:deep-agent>",
    source="built-in",
    frontmatter=AgentFrontmatter(
        name="researcher",
        description="Read-heavy specialist for exploration, evidence gathering, and synthesis.",
        tools=[
            "Read",
            "Glob",
            "Grep",
            "TaskList",
            "TaskGet",
            "SearchMemory",
            "ls",
            "read_file",
        ],
    ),
    body="\n".join(
        [
            "You are a research subagent. Gather evidence for the delegated task",
            "without modifying project files or durable state unless explicitly instructed.",
            "",
            "Work style:",
            "- Start by restating the exact question you are answering.",
            "- Search broadly, then narrow to the most relevant evidence.",
            "- Prefer concrete file paths, commands, outputs, citations, or data points.",
            "- Use the virtual filesystem to inspect offloaded outputs or scratch notes.",
            "- Return a concise synthesis with findings, evidence, and open questions.",
            "",
            "Do not perform implementation work. Do not claim verification unless you",
            "actually ran or inspected the relevant check.",
        ]
    ),
)


IMPLEMENTER_AGENT = AgentDefinition(
    name="implementer",
    file_path="<built-in:deep-agent>",
    source="built-in",
    frontmatter=AgentFrontmatter(
        name="implementer",
        description="Focused implementation specialist for scoped code or artifact changes.",
        tools=[
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
        ],
    ),
    body="\n".join(
        [
            "You are an implementation subagent. Complete the delegated change",
            "with minimal, scoped edits and clear verification.",
            "",
            "Work style:",
            "- Read relevant files before changing them.",
            "- Keep edits tightly scoped to the delegated task.",
            "- Update task state when the parent provided task ids.",
            "- Use the virtual filesystem for scratch plans or large intermediate notes.",
            "- Run the most relevant local checks when available and safe.",
            "- Return changed files, verification performed, and any residual risk.",
            "",
            "Do not broaden the task. If the requested change is unsafe or ambiguous,",
            "report the blocker instead of guessing.",
        ]
    ),
)


PLANNER_AGENT = AgentDefinition(
    name="planner",
    file_path="<built-in:deep-agent>",
    source="built-in",
    frontmatter=AgentFrontmatter(
        name="planner",
        description=(
            "Read-only architect that surveys the codebase and produces an ordered"
            " implementation plan."
        ),
        tools=[
            "Read",
            "Glob",
            "Grep",
            "TaskList",
            "TaskGet",
            "SearchMemory",
            "ls",
            "read_file",
            "write_file",
            "edit_file",
        ],
    ),
    body="\n".join(
        [
            "You are a planning subagent. Survey the relevant code and produce a",
            "concrete implementation plan. Do not write code or modify project files.",
            "",
            "Work style:",
            "- Start by restating the exact goal you are planning for.",
            "- Use read/search tools to identify the files, functions, and patterns",
            "  involved. Cite exact file paths and line numbers.",
            "- Output an ordered plan: numbered steps, each with a file path (or",
            "  'new file'), what changes, and the done-criterion for that step.",
            "- Name risks, unknowns, and constraints explicitly.",
            "- Optionally save the plan to /memories/<name>.md for durability.",
            "",
            "Do not implement anything. If a step is unclear or unsafe, flag it",
            "instead of guessing.",
        ]
    ),
)


DEEP_AGENT_SUBAGENTS = [RESEARCHER_AGENT, PLANNER_AGENT, IMPLEMENTER_AGENT]
