from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..events import ReliabilityEvent
from ..hooks import HookResult
from ..kernel import Disposable
from ..reliability import ReliabilityPolicy
from ..types import Message, TextBlock, ToolResultBlock, ToolUseBlock
from .turn_stopping import TurnStopping, TurnStoppingDecision


@dataclass(slots=True)
class ReliabilityRuntime:
    policy: ReliabilityPolicy
    corrections: int = 0
    succeeded: set[str] = field(default_factory=set)

    def bind(self, agent: Any, run_id: str) -> Disposable:
        runs = agent.context.get("reliability_runs")
        runs[run_id] = self
        return Disposable(lambda: runs.pop(run_id) if runs.get(run_id) is self else None)

    def observe_results(
        self, blocks: list[ToolUseBlock], results: dict[str, ToolResultBlock]
    ) -> None:
        self.succeeded.update(
            block.name
            for block in blocks
            if block.name in self.policy.referenced_tools
            and not block.input.get("run_in_background")
            and not results[block.id].is_error
        )

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
        reason = "correction_budget_exhausted" if exhausted else mechanism
        return TurnStoppingDecision(
            action,
            feedback=() if exhausted else (Message(role="user", content=[TextBlock(text=text)]),),
            error=ValueError("reliability correction budget exhausted") if exhausted else None,
            event=ReliabilityEvent(mechanism, action, reason, event.turn_index, self.corrections),
        )

    def checkpoint_state(self) -> dict[str, Any]:
        return (
            dict(v=1, corrections=self.corrections, succeeded=sorted(self.succeeded))
            if self.corrections or self.succeeded
            else {}
        )

    def restore(self, raw: Any) -> ReliabilityRuntime:
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


class PrerequisiteHook:
    def __init__(self, agent: Any) -> None:
        self.context = agent.context
        self.runs: dict[str, ReliabilityRuntime] = {}
        agent.context.register("reliability_runs", self.runs)

    def on_pre_tool_use(self, ctx: Any) -> HookResult | None:
        runtime = self.runs.get(ctx.run_id)
        if runtime is None or ctx.session.agent.context is not self.context:
            return None
        missing: set[str] = set()
        for rule in runtime.policy.prerequisites:
            if rule.tool_name == ctx.tool_name:
                missing.update(set(rule.requires_success) - runtime.succeeded)
        if missing:
            names = ", ".join(sorted(missing))
            return HookResult.block(
                f"Prerequisite not satisfied: {names}. The tool was not executed."
            )
        return None

    def install(self, agent: Any) -> Disposable:
        agent.hooks.insert(0, self)
        return Disposable(lambda: agent.hooks.remove(self) if self in agent.hooks else None)
