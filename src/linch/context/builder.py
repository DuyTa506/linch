from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from ..types import Message, SystemBlock, TextBlock

if TYPE_CHECKING:
    from ..session import Session


@dataclass(slots=True)
class ContextBudget:
    max_tokens: int | None = None
    used_tokens: int = 0
    remaining_tokens: int | None = None
    trimmed: bool = False


@dataclass(slots=True)
class ContextBuildTurn:
    session: Session
    messages: list[Message]
    turn_index: int
    deps: Any
    model: str
    tools: Any
    token_estimator: Any = None


@dataclass(slots=True)
class ContextBuildResult:
    system_blocks: list[SystemBlock] = field(default_factory=list)
    messages: list[Message] = field(default_factory=list)
    selected_tools: Any = None
    budget: ContextBudget = field(default_factory=ContextBudget)
    metadata: dict[str, Any] = field(default_factory=dict)


class ContextBuilder(Protocol):
    async def build(self, turn: ContextBuildTurn) -> ContextBuildResult:
        """Build ephemeral context for one provider call."""
        ...


class ContextBuilderChain:
    def __init__(self, builders: Iterable[ContextBuilder]) -> None:
        self.builders = list(builders)

    async def build(self, turn: ContextBuildTurn) -> ContextBuildResult:
        merged = ContextBuildResult()
        for builder in self.builders:
            result = await builder.build(turn)
            merged.system_blocks.extend(result.system_blocks)
            merged.messages.extend(result.messages)
            if result.selected_tools is not None:
                merged.selected_tools = result.selected_tools
            if result.budget.max_tokens is not None:
                merged.budget.max_tokens = result.budget.max_tokens
            merged.budget.trimmed = merged.budget.trimmed or result.budget.trimmed
            merged.metadata.update(result.metadata)
        return merged


def normalize_context_builder(
    builder: ContextBuilder | Iterable[ContextBuilder] | None,
) -> ContextBuilder | None:
    builders: list[ContextBuilder] = []
    if builder is not None:
        if isinstance(builder, Iterable) and not hasattr(builder, "build"):
            builders.extend(builder)
        else:
            builders.append(builder)

    if not builders:
        return None
    if len(builders) == 1:
        return builders[0]
    return ContextBuilderChain(builders)


def apply_context_budget(
    result: ContextBuildResult,
    *,
    estimator: Any = None,
    model: str,
) -> ContextBuildResult:
    max_tokens = result.budget.max_tokens
    system_blocks = list(result.system_blocks)
    messages = list(result.messages)
    used = _estimate_context_tokens(system_blocks, messages, estimator, model)

    if max_tokens is None:
        result.budget.used_tokens = used
        result.budget.remaining_tokens = None
        return result

    trimmed = result.budget.trimmed
    while messages and used > max_tokens:
        messages.pop(0)
        used = _estimate_context_tokens(system_blocks, messages, estimator, model)
        trimmed = True

    while system_blocks and used > max_tokens:
        system_blocks.pop(0)
        used = _estimate_context_tokens(system_blocks, messages, estimator, model)
        trimmed = True

    result.system_blocks = system_blocks
    result.messages = messages
    result.budget.used_tokens = used
    result.budget.remaining_tokens = max(0, max_tokens - used)
    result.budget.trimmed = trimmed
    return result


def context_budget_to_dict(budget: ContextBudget) -> dict[str, Any]:
    return {
        "max_tokens": budget.max_tokens,
        "used_tokens": budget.used_tokens,
        "remaining_tokens": budget.remaining_tokens,
        "trimmed": budget.trimmed,
    }


def _estimate_context_tokens(
    system_blocks: list[SystemBlock],
    messages: list[Message],
    estimator: Any,
    model: str,
) -> int:
    system_tokens = _estimate_text("\n".join(block.text for block in system_blocks))
    if callable(estimator):
        try:
            return system_tokens + max(0, int(estimator(messages, model)))
        except Exception:
            pass
    return system_tokens + sum(_estimate_message(message) for message in messages)


def _estimate_message(message: Message) -> int:
    text = ""
    for block in message.content:
        if isinstance(block, TextBlock):
            text += block.text
    return _estimate_text(text)


def _estimate_text(text: str) -> int:
    return max(1, len(text) // 4) if text else 0
