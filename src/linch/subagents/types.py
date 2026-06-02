from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(slots=True)
class AgentFrontmatter:
    name: str
    description: str
    tools: list[str] | None = None


@dataclass(slots=True)
class AgentDefinition:
    name: str
    file_path: str
    source: Literal["disk", "built-in"]
    frontmatter: AgentFrontmatter
    body: str


@dataclass(slots=True)
class SkippedAgent:
    file_path: str
    reason: Literal[
        "invalid-frontmatter",
        "missing-frontmatter",
        "name-collision",
        "io-error",
    ]
    detail: str


@dataclass(slots=True)
class LoadAgentsResult:
    agents: list[AgentDefinition]
    skipped: list[SkippedAgent]
