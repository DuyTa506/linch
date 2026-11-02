from __future__ import annotations

import re

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

    replacements = {
        "$ARGUMENTS": args,
        "${SKILL_DIR}": skill.dir,
        "${LINCH_SKILL_DIR}": skill.dir,
        "${LINCH_SESSION_ID}": session_id,
    }
    replacements.update(
        {f"${name}": tokens[i] if i < len(tokens) else "" for i, name in enumerate(slots)}
    )
    alternatives = "|".join(
        re.escape(placeholder) + ("" if placeholder.endswith("}") else "(?![A-Za-z0-9_])")
        for placeholder in sorted(replacements, key=len, reverse=True)
    )
    body = re.sub(
        alternatives,
        lambda match: replacements[match.group(0)],
        skill.body,
    )

    return f"Skill base directory: {skill.dir}\n\n{body}"
