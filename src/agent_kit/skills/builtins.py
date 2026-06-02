from __future__ import annotations

from .types import Skill, SkillFrontmatter

VERIFY_SKILL = Skill(
    name="verify",
    dir="<built-in>/verify",
    frontmatter=SkillFrontmatter(
        name="verify",
        description=(
            "Plan and run evidence-based verification for completed work, ending with a verdict."
        ),
        when_to_use=(
            "Use after non-trivial changes or before claiming a task is complete."
        ),
        argument_hint="optional task, artifact, or risk focus",
    ),
    body="\n".join(
        [
            "# Verify",
            "",
            "Verify completed work with runnable evidence. Adapt the checks to the domain:",
            "software changes, data workflows, document/process updates, configuration,",
            "or other concrete deliverables.",
            "",
            "## Inputs",
            "",
            "User focus, if provided:",
            "",
            "$ARGUMENTS",
            "",
            "## Method",
            "",
            "1. Identify what success means from the latest user request and available",
            "   project or workflow documentation.",
            "2. Choose checks that directly exercise the changed behavior or artifact.",
            "3. Run the checks when tools are available. If a check cannot be run, explain",
            "   the environmental blocker instead of implying it passed.",
            "4. Include at least one adversarial probe when applicable: boundary input,",
            "   malformed input, idempotency, concurrency, orphan reference, regression,",
            "   or consistency check.",
            "5. Report commands/actions run, key output or observations, and expected vs.",
            "   actual results.",
            "",
            "End with exactly one line:",
            "",
            "VERDICT: PASS",
            "or",
            "VERDICT: FAIL",
            "or",
            "VERDICT: PARTIAL",
        ]
    ),
)

BUILT_IN_SKILLS = [VERIFY_SKILL]


def merge_builtin_skills(disk_skills: list[Skill]) -> list[Skill]:
    """Return disk skills plus built-ins, with disk skills overriding by name."""

    disk_names = {skill.name.lower() for skill in disk_skills}
    builtins = [skill for skill in BUILT_IN_SKILLS if skill.name.lower() not in disk_names]
    return sorted([*disk_skills, *builtins], key=lambda skill: skill.name.lower())

