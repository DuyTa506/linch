from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Literal

from agent_kit.types import ModelId, ProviderRequest

EffortLevel = Literal["low", "medium", "high", "xhigh", "max"]


@dataclass(slots=True)
class ThinkingDisabled:
    type: Literal["disabled"] = "disabled"


@dataclass(slots=True)
class ThinkingEnabled:
    budget_tokens: int
    display: Literal["summarized", "omitted"] | None = None
    type: Literal["enabled"] = "enabled"


@dataclass(slots=True)
class ThinkingAdaptive:
    display: Literal["summarized", "omitted"] | None = None
    type: Literal["adaptive"] = "adaptive"


ThinkingConfig = ThinkingDisabled | ThinkingEnabled | ThinkingAdaptive


class BaseProvider(ABC):
    id: str

    @abstractmethod
    def context_window(self, model: ModelId) -> int:
        raise NotImplementedError

    @abstractmethod
    def stream(self, req: ProviderRequest) -> AsyncIterator[dict[str, object]]:
        raise NotImplementedError
