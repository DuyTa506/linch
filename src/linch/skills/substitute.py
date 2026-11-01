from __future__ import annotations

from .shell_split import split_shell_args
from .types import Skill


def substitute_skill_body(
    skill: Skill,
    args: str,
    session_id: str,
) -> str:
    """Substitute invocation arguments and runtime placeholders into a skill body.

    Args:
        skill: Loaded skill whose body and frontmatter define the substitutions.
        args: Raw invocation arguments supplied to the skill.
        session_id: Current session identifier for the session placeholder.

    Returns:
        The expanded prompt prefixed with the skill's base directory.
    """
    tokens = split_shell_args(args)
    slots = skill.frontmatter.arguments or []

    body = skill.body
    for i in range(len(slots)):
        name = slots[i]
        value = tokens[i] if i < len(tokens) else ""
        body = body.replace(f"${name}", value)

    body = body.replace("$ARGUMENTS", args)
    body = body.replace("${LINCH_SKILL_DIR}", skill.dir)
    body = body.replace("${LINCH_SESSION_ID}", session_id)

    return f"Skill base directory: {skill.dir}\n\n{body}"
