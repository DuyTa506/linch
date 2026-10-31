from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SkillFrontmatter:
    """Store the validated metadata from a skill's YAML frontmatter.

    Args:
        name: Public skill name used by the ``Skill`` tool.
        description: Short description shown in skill listings.
        disable_model_invocation: Whether the model may invoke the skill directly.
        when_to_use: Optional guidance shown alongside the description.
        allowed_tools: Optional tool names available while the skill runs.
        arguments: Optional positional argument names substituted into the body.
        argument_hint: Optional usage hint for the skill's arguments.
        model: Optional model override for the skill invocation.
        version: Optional skill version metadata.

    Attributes:
        name: Public skill name.
        description: Listing description.
        disable_model_invocation: Model-invocation gate.
        when_to_use: Optional usage guidance.
        allowed_tools: Optional tool restriction.
        arguments: Optional body-substitution slots.
        argument_hint: Optional argument usage hint.
        model: Optional model override.
        version: Optional version metadata.
    """

    name: str
    description: str
    disable_model_invocation: bool = False
    when_to_use: str | None = None
    allowed_tools: list[str] | None = None
    arguments: list[str] | None = None
    argument_hint: str | None = None
    model: str | None = None
    version: str | None = None


@dataclass
class Skill:
    """Represent a loaded skill and its prompt body.

    Args:
        name: Public skill name.
        dir: Directory containing the skill definition.
        frontmatter: Validated metadata parsed from ``SKILL.md``.
        body: Markdown prompt body injected when the skill is invoked.

    Attributes:
        name: Public skill name.
        dir: Skill definition directory.
        frontmatter: Parsed skill metadata.
        body: Prompt body before argument substitution.
    """

    name: str
    dir: str
    frontmatter: SkillFrontmatter
    body: str


@dataclass
class SkippedSkill:
    """Describe a skill definition omitted during directory loading.

    Args:
        dir: Directory containing the skipped definition.
        reason: Stable category explaining why it was skipped.
        detail: Human-readable diagnostic detail.

    Attributes:
        dir: Skipped skill directory.
        reason: Skip category.
        detail: Diagnostic detail.
    """

    dir: str
    reason: str
    detail: str
