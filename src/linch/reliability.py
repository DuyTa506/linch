from __future__ import annotations

from dataclasses import dataclass
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

    def validate(
        self,
        tool_names: Any,
        *,
        final_tool_name: str | None = None,
    ) -> None:
        if type(self.max_correction_turns) is not int or self.max_correction_turns < 0:
            raise ValueError("reliability.max_correction_turns must be a non-negative int")
        if type(self.retry_invalid_responses) is not bool:
            raise ValueError("reliability.retry_invalid_responses must be a bool")
        names = {str(name) for name in tool_names}
        edges: dict[str, set[str]] = {}
        for prerequisite in self.prerequisites:
            if not isinstance(prerequisite, ToolPrerequisite):
                raise ValueError("reliability.prerequisites must contain ToolPrerequisite values")
            if prerequisite.tool_name not in names:
                raise ValueError(f"unknown prerequisite tool {prerequisite.tool_name!r}")
            seen = edges.setdefault(prerequisite.tool_name, set())
            for required in prerequisite.requires_success:
                if required not in names:
                    raise ValueError(f"unknown prerequisite dependency {required!r}")
                if required == prerequisite.tool_name:
                    raise ValueError(f"tool {required!r} cannot depend on itself")
                if required in seen:
                    raise ValueError(
                        f"duplicate prerequisite edge {prerequisite.tool_name!r}->{required!r}"
                    )
                seen.add(required)
        completion = self.completion
        if completion is not None:
            if not isinstance(completion, CompletionGate):
                raise ValueError("reliability.completion must be a CompletionGate or None")
            seen_completion: set[str] = set()
            for required in completion.required_successful_tools:
                if required not in names:
                    raise ValueError(f"unknown completion tool {required!r}")
                if required in seen_completion:
                    raise ValueError(f"duplicate completion tool {required!r}")
                if final_tool_name is not None and required == final_tool_name:
                    raise ValueError("the terminal final tool cannot be completion evidence")
                seen_completion.add(required)
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node: str) -> None:
            if node in visiting:
                raise ValueError("reliability prerequisites contain a cycle")
            if node in visited:
                return
            visiting.add(node)
            for dependency in edges.get(node, ()):
                visit(dependency)
            visiting.remove(node)
            visited.add(node)

        for node in edges:
            visit(node)

    @property
    def referenced_tools(self) -> frozenset[str]:
        values = {dependency for item in self.prerequisites for dependency in item.requires_success}
        if self.completion is not None:
            values.update(self.completion.required_successful_tools)
        return frozenset(values)


def canonical_reliability_policy(policy: ReliabilityPolicy | None) -> dict[str, Any] | None:
    if policy is None:
        return None
    return {
        "max_correction_turns": policy.max_correction_turns,
        "retry_invalid_responses": policy.retry_invalid_responses,
        "prerequisites": [
            {
                "tool_name": item.tool_name,
                "requires_success": sorted(item.requires_success),
            }
            for item in sorted(policy.prerequisites, key=lambda value: value.tool_name)
        ],
        "completion": (
            {
                "required_successful_tools": sorted(policy.completion.required_successful_tools),
            }
            if policy.completion is not None
            else None
        ),
    }
