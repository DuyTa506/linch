"""Stable v1alpha2 starting points for manual, terminal, and AI-assisted authoring.

Templates return ordinary JSON-compatible dictionaries so the browser, CLI, and
TUI can all begin from the same semantic document. Validation is deliberately a
separate final step: :func:`build_template` passes the data through the active
strict ``Blueprint`` model, while :func:`build_template_data` is useful to
clients that need to serialize or inspect the canonical starting point first.
"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from linch_studio.spec import Blueprint

TEMPLATE_API_VERSION = "studio.linch.dev/v1alpha2"
DEFAULT_TEMPLATE = "agent"

TemplateBuilder = Callable[[str, str], dict[str, Any]]


@dataclass(frozen=True, slots=True)
class BlueprintTemplate:
    """One immutable template descriptor and its deterministic data builder."""

    id: str
    title: str
    summary: str
    builder: TemplateBuilder

    def build_data(self, project_id: str, title: str | None = None) -> dict[str, Any]:
        normalized = _normalize_project_id(project_id)
        display_title = title or _display_title(normalized)
        return deepcopy(self.builder(normalized, display_title))


def _normalize_project_id(value: str) -> str:
    normalized = value.replace("-", "_")
    if not normalized or not normalized[0].isalpha():
        raise ValueError("project id must start with a lowercase letter")
    valid_characters = all(char.islower() or char.isdigit() or char == "_" for char in normalized)
    if not normalized.isascii() or not valid_characters:
        raise ValueError("project id must use lowercase ASCII letters, digits, or underscores")
    return normalized


def _display_title(project_id: str) -> str:
    return " ".join(part.capitalize() for part in project_id.split("_"))


def _base(project_id: str, title: str, *, preset: str = "standard_agent") -> dict[str, Any]:
    return {
        "apiVersion": TEMPLATE_API_VERSION,
        "kind": "LinchProject",
        "metadata": {"name": project_id, "title": title, "description": ""},
        "spec": {
            "package": project_id,
            "runtime": {
                "provider": {},
                "agent": {
                    "preset": preset,
                    "instructions": "",
                    # New Studio projects start with an explicit empty custom-tool
                    # pool so Tool cards become real attachable choices. Migrated
                    # blueprints that omit this field retain legacy "all" semantics.
                    "tools": [],
                    "maxTurns": 20,
                    "budget": {"maxTokens": 100_000},
                    "completion": {
                        "mode": "agent_judged",
                        "maxRetries": 0,
                        "verifiers": [],
                    },
                },
            },
            "tools": [],
            "subagents": [],
            "skills": [],
            "workflows": [],
            "routines": [],
            "triggers": [],
            "capabilities": {},
        },
    }


def _agent(project_id: str, title: str) -> dict[str, Any]:
    return _base(project_id, title)


def _goal_verified(project_id: str, title: str) -> dict[str, Any]:
    document = _base(project_id, title)
    document["spec"]["runtime"]["agent"]["completion"] = {
        "mode": "verifier_gated",
        "maxRetries": 2,
        "verifiers": [
            {
                "kind": "custom_todo",
                "id": "acceptance_gate",
                "description": "TODO: implement the project acceptance verifier.",
            }
        ],
    }
    return document


def _directed_workflow(project_id: str, title: str) -> dict[str, Any]:
    document = _base(project_id, title)
    document["spec"]["workflows"] = [
        {
            "kind": "directed",
            "id": "main_workflow",
            "displayName": "Main Workflow",
            "nodes": [
                {
                    "type": "agent_call",
                    "id": "produce",
                    "label": "Produce",
                    "prompt": "Produce the requested result from the trigger envelope.",
                    "dependsOn": [],
                    "tools": [],
                },
                {
                    "type": "agent_call",
                    "id": "review",
                    "label": "Review",
                    "prompt": "Review the predecessor result and return the final answer.",
                    "dependsOn": ["produce"],
                    "tools": [],
                },
            ],
            "output": "review",
            "maxConcurrency": 2,
        }
    ]
    return document


def _coordinator(project_id: str, title: str) -> dict[str, Any]:
    document = _base(project_id, title, preset="coordinator")
    document["spec"]["runtime"]["agent"]["instructions"] = (
        "Delegate bounded tasks to the declared specialists and synthesize their evidence."
    )
    document["spec"]["subagents"] = [
        {
            "id": "researcher",
            "displayName": "Researcher",
            "description": "Collect focused evidence for one delegated task.",
            "instructions": "Collect relevant evidence and return concise findings.",
            "tools": [],
        },
        {
            "id": "reviewer",
            "displayName": "Reviewer",
            "description": "Independently review evidence and identify gaps.",
            "instructions": "Review the supplied evidence and report concrete gaps.",
            "tools": [],
        },
    ]
    return document


def _routine(project_id: str, title: str) -> dict[str, Any]:
    document = _base(project_id, title)
    document["spec"]["tools"] = [
        {
            "kind": "function",
            "id": "inspect_git_worktree",
            "displayName": "Inspect Git Worktree",
            "description": "Read repository worktree state without modifying files.",
            "scope": "read",
            "parallel": False,
            "retryable": False,
            "resources": [{"resource": ".git", "mode": "read"}],
        }
    ]
    document["spec"]["triggers"] = [
        {
            "kind": "cron",
            "id": "every_two_hours",
            "displayName": "Every Two Hours",
            "cron": "0 */2 * * *",
            "timezone": "UTC",
        }
    ]
    document["spec"]["routines"] = [
        {
            "kind": "agent_tick",
            "id": "git_worktree_check",
            "displayName": "Git Worktree Check",
            "charter": "Inspect the git worktree without changing it.",
            "prompt": "Check the git worktree and report actionable changes. Do not modify files.",
            "target": "runtime_agent",
            "triggers": ["every_two_hours"],
            "maxTurns": 6,
            "budget": {"maxTokens": 20_000},
            "verify": None,
            "doneWhen": None,
        }
    ]
    return document


_TEMPLATES = (
    BlueprintTemplate("agent", "Agent", "A bounded standard model-tool agent loop.", _agent),
    BlueprintTemplate(
        "goal_verified",
        "Goal-verified",
        "A bounded agent loop with a blocking verifier implementation seam.",
        _goal_verified,
    ),
    BlueprintTemplate(
        "directed_workflow",
        "Directed workflow",
        "A replayable two-step workflow whose control flow is owned by code.",
        _directed_workflow,
    ),
    BlueprintTemplate(
        "coordinator",
        "Coordinator",
        "A model-directed coordinator with two inherited-budget specialists.",
        _coordinator,
    ),
    BlueprintTemplate(
        "routine",
        "Routine",
        "A host-owned two-hour read-only worktree inspection tick.",
        _routine,
    ),
)

TEMPLATE_IDS = tuple(item.id for item in _TEMPLATES)
_BY_ID = {item.id: item for item in _TEMPLATES}


def get_template(template_id: str) -> BlueprintTemplate:
    """Return one template or raise a stable, user-facing ``KeyError``."""

    try:
        return _BY_ID[template_id]
    except KeyError:
        raise KeyError(f"unknown blueprint template: {template_id}") from None


def build_template_data(
    template_id: str,
    project_id: str,
    *,
    title: str | None = None,
) -> dict[str, Any]:
    """Build a fresh JSON-compatible v1alpha2 document."""

    return get_template(template_id).build_data(project_id, title)


def build_template(
    template_id: str,
    project_id: str,
    *,
    title: str | None = None,
) -> Blueprint:
    """Build and strictly validate a template against the active Studio schema."""

    return Blueprint.model_validate(
        build_template_data(template_id, project_id, title=title),
        strict=True,
    )


def template_summaries() -> tuple[tuple[str, str, str], ...]:
    """Return stable display metadata without exposing mutable builders."""

    return tuple((item.id, item.title, item.summary) for item in _TEMPLATES)


__all__ = [
    "DEFAULT_TEMPLATE",
    "TEMPLATE_API_VERSION",
    "TEMPLATE_IDS",
    "BlueprintTemplate",
    "build_template",
    "build_template_data",
    "get_template",
    "template_summaries",
]
