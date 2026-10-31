from __future__ import annotations

from dataclasses import dataclass
from graphlib import CycleError, TopologicalSorter
from typing import Any


@dataclass(frozen=True, slots=True)
class ToolPrerequisite:
    tool_name: str
    requires_success: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CompletionGate:
    required_successful_tools: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReliabilityPolicy:
    max_correction_turns: int = 2
    retry_invalid_responses: bool = True
    prerequisites: tuple[ToolPrerequisite, ...] = ()
    completion: CompletionGate | None = None

    def validate(self, tool_names: Any, *, final_tool_name: str | None = None) -> None:
        if type(self.max_correction_turns) is not int or self.max_correction_turns < 0:
            raise ValueError("reliability.max_correction_turns must be a non-negative int")
        if type(self.retry_invalid_responses) is not bool:
            raise ValueError("reliability.retry_invalid_responses must be a bool")
        edges: dict[str, set[str]] = {}
        for rule in self.prerequisites:
            seen = edges.setdefault(rule.tool_name, set())
            if len(set(rule.requires_success)) != len(rule.requires_success) or seen.intersection(
                rule.requires_success
            ):
                raise ValueError("duplicate prerequisite edge")
            if rule.tool_name in rule.requires_success:
                raise ValueError("tool cannot depend on itself")
            seen.update(rule.requires_success)
        required = self.completion.required_successful_tools if self.completion else ()
        if len(set(required)) != len(required):
            raise ValueError("duplicate completion tool")
        if final_tool_name in required:
            raise ValueError("the terminal final tool cannot be completion evidence")
        if (set(edges) | self.referenced_tools) - set(tool_names):
            raise ValueError("unknown reliability tool name")
        try:
            tuple(TopologicalSorter(edges).static_order())
        except CycleError:
            raise ValueError("reliability prerequisites contain a cycle") from None

    @property
    def referenced_tools(self) -> frozenset[str]:
        values = {dependency for item in self.prerequisites for dependency in item.requires_success}
        if self.completion is not None:
            values.update(self.completion.required_successful_tools)
        return frozenset(values)


def canonical_reliability_policy(policy: ReliabilityPolicy | None) -> dict[str, Any] | None:
    if policy is None:
        return None
    edges: dict[str, set[str]] = {}
    for item in policy.prerequisites:
        edges.setdefault(item.tool_name, set()).update(item.requires_success)
    return {
        "max_correction_turns": policy.max_correction_turns,
        "retry_invalid_responses": policy.retry_invalid_responses,
        "prerequisites": {name: sorted(required) for name, required in sorted(edges.items())},
        "completion": sorted(policy.completion.required_successful_tools)
        if policy.completion
        else None,
    }
