from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SkillFrontmatter:
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
    name: str
    dir: str
    frontmatter: SkillFrontmatter
    body: str


@dataclass
class SkippedSkill:
    dir: str
    reason: str
    detail: str
