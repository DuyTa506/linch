from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..events import ReliabilityEvent
from ..reliability import ReliabilityPolicy
from ..tools.pipeline import ToolExecution, ToolPipeline
from ..types import Message, TextBlock
from .turn_stopping import TurnStopping, TurnStoppingDecision


@dataclass(slots=True)
class ReliabilityRuntime:
    policy: ReliabilityPolicy
    corrections: int = 0
    succeeded: set[str] = field(default_factory=set)
    _batch_snapshot: frozenset[str] = frozenset()
    pre_execute_pipeline: ToolPipeline | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        if self.policy.prerequisites:
            self.pre_execute_pipeline = ToolPipeline()
            self.pre_execute_pipeline.on_pre_execute(self._pre_execute)

    def begin_batch(self) -> None:
        self._batch_snapshot = frozenset(self.succeeded)

    async def _pre_execute(self, execution: ToolExecution, proceed: Any) -> Any:
        missing = sorted(
            {
                name
                for item in self.policy.prerequisites
                if item.tool_name == execution.tool_name
                for name in item.requires_success
            }
            - self._batch_snapshot
        )
        return (
            "Prerequisite not satisfied: " + ", ".join(missing) + ". The tool was not executed."
            if missing
            else await proceed()
        )

    async def foreground_result(self, name: str) -> None:
        if name in self.policy.referenced_tools:
            self.succeeded.add(name)

    async def listener(self, event: TurnStopping) -> TurnStoppingDecision | None:
        gate = self.policy.completion
        missing = sorted(set(gate.required_successful_tools) - self.succeeded) if gate else []
        invalid = event.reason == "invalid_response"
        if (not invalid and not missing) or (invalid and not self.policy.retry_invalid_responses):
            return None
        mechanism = "invalid_response" if invalid else "completion"
        exhausted = self.corrections >= self.policy.max_correction_turns
        if not exhausted:
            self.corrections += 1
        text = (
            "[Linch] The provider response was invalid. Nothing was executed. "
            "Please resend the response using native tool-call format."
            if invalid
            else "[Linch] Completion requirements are missing: "
            + ", ".join(missing)
            + ". Nothing was executed; continue with the required tools."
        )
        action = "fail" if exhausted else "continue"
        return TurnStoppingDecision(
            action,
            feedback=() if exhausted else (Message(role="user", content=[TextBlock(text=text)]),),
            error=ValueError("reliability correction budget exhausted") if exhausted else None,
            event=ReliabilityEvent(
                mechanism,
                action,
                "correction_budget_exhausted" if exhausted else mechanism,
                event.turn_index,
                self.corrections,
            ),
        )

    def checkpoint_state(self) -> dict[str, Any]:
        return (
            dict(v=1, corrections=self.corrections, succeeded=sorted(self.succeeded))
            if self.corrections or self.succeeded
            else {}
        )

    def restore(self, raw: Any) -> ReliabilityRuntime:
        if raw is None:
            return self
        invalid = "invalid linch.reliability checkpoint state"
        if not isinstance(raw, dict) or set(raw) != {"v", "corrections", "succeeded"}:
            raise ValueError(invalid)
        succeeded = raw["succeeded"]
        if (
            type(raw["v"]) is not int
            or raw["v"] != 1
            or type(raw["corrections"]) is not int
            or not 0 <= raw["corrections"] <= self.policy.max_correction_turns
            or not isinstance(succeeded, list)
            or any(not isinstance(name, str) for name in succeeded)
            or succeeded != sorted(set(succeeded))
            or not set(succeeded) <= self.policy.referenced_tools
        ):
            raise ValueError(invalid)
        self.corrections = raw["corrections"]
        self.succeeded = set(succeeded)
        return self
