from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..events import ReliabilityEvent
from ..reliability import ReliabilityPolicy
from ..types import Message, TextBlock
from .turn_stopping import TurnStopping, TurnStoppingDecision


@dataclass(slots=True)
class ReliabilityRuntime:
    policy: ReliabilityPolicy
    corrections: int = 0
    succeeded: set[str] = field(default_factory=set)
    _batch_snapshot: frozenset[str] = frozenset()

    def begin_batch(self) -> None:
        self._batch_snapshot = frozenset(self.succeeded)

    async def before_permission(self, call: Any) -> str | None:
        tool = getattr(call, "tool", None)
        tool_name = getattr(tool, "name", None)
        for prerequisite in self.policy.prerequisites:
            if prerequisite.tool_name == tool_name:
                missing = [
                    name
                    for name in prerequisite.requires_success
                    if name not in self._batch_snapshot
                ]
                if missing:
                    return (
                        "Prerequisite not satisfied: "
                        + ", ".join(sorted(missing))
                        + ". The tool was not executed."
                    )
        return None

    async def foreground_result(self, call: Any, _decision: Any, _event: Any) -> None:
        name = getattr(getattr(call, "tool", None), "name", None)
        if isinstance(name, str) and name in self.policy.referenced_tools:
            self.succeeded.add(name)

    def missing_completion(self) -> tuple[str, ...]:
        gate = self.policy.completion
        if gate is None:
            return ()
        return tuple(
            name for name in gate.required_successful_tools if name not in self.succeeded
        )

    async def listener(self, event: TurnStopping) -> TurnStoppingDecision | None:
        missing = self.missing_completion()
        invalid = event.reason == "invalid_response"
        if not invalid and not missing:
            return None
        if not self.policy.retry_invalid_responses and invalid:
            return None
        if self.corrections >= self.policy.max_correction_turns:
            return TurnStoppingDecision(
                "fail",
                error=ValueError("reliability correction budget exhausted"),
                event=ReliabilityEvent(
                    "completion" if missing else "invalid_response",
                    "fail",
                    "correction_budget_exhausted",
                    event.turn_index,
                    self.corrections,
                ),
            )
        self.corrections += 1
        text = (
            "[Linch] The provider response was invalid. Nothing was executed. "
            "Please resend the response using native tool-call format."
            if invalid
            else "[Linch] Completion requirements are missing: "
            + ", ".join(missing)
            + ". Nothing was executed; continue with the required tools."
        )
        return TurnStoppingDecision(
            "continue",
            feedback=(Message(role="user", content=[TextBlock(text=text)]),),
            event=ReliabilityEvent(
                "invalid_response" if invalid else "completion",
                "continue",
                "invalid_response" if invalid else "missing_requirements",
                event.turn_index,
                self.corrections,
            ),
        )

    def checkpoint_state(self) -> dict[str, Any]:
        if self.corrections == 0 and not self.succeeded:
            return {}
        return {
            "v": 1,
            "corrections": self.corrections,
            "succeeded": sorted(self.succeeded & self.policy.referenced_tools),
        }

    @classmethod
    def restore(cls, policy: ReliabilityPolicy, raw: Any) -> ReliabilityRuntime:
        runtime = cls(policy)
        if raw is None:
            return runtime
        if not isinstance(raw, dict) or set(raw) != {"v", "corrections", "succeeded"}:
            raise ValueError("invalid linch.reliability checkpoint state")
        if (
            type(raw["v"]) is not int
            or raw["v"] != 1
            or type(raw["corrections"]) is not int
            or raw["corrections"] < 0
        ):
            raise ValueError("invalid linch.reliability checkpoint state")
        if raw["corrections"] > policy.max_correction_turns:
            raise ValueError("invalid linch.reliability checkpoint state")
        succeeded = raw["succeeded"]
        if not isinstance(succeeded, list) or any(not isinstance(name, str) for name in succeeded):
            raise ValueError("invalid linch.reliability checkpoint state")
        if succeeded != sorted(set(succeeded)) or not set(succeeded) <= policy.referenced_tools:
            raise ValueError("invalid linch.reliability checkpoint state")
        runtime.corrections = raw["corrections"]
        runtime.succeeded = set(succeeded)
        return runtime

