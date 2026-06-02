"""Stdlib-only reference observers: LoggingObserver and SpanCollector."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from .protocol import (
    BaseObserver,
    ProviderCallInfo,
    ProviderCallResult,
    RunInfo,
    RunResultInfo,
    ToolInfo,
    ToolResultInfo,
    TurnInfo,
)


@dataclass(slots=True)
class Span:
    """A completed span record captured by :class:`SpanCollector`."""

    kind: str  # "run" | "turn" | "provider" | "tool"
    run_id: str
    name: str
    duration_ms: int
    turn_index: int = -1
    tool_use_id: str = ""
    attributes: dict[str, Any] = field(default_factory=dict)


class LoggingObserver(BaseObserver):
    """Emits one stdlib logging line per span.

    Useful for quick local visibility without any extra dependencies.
    Inputs/prompts are never logged at INFO to avoid leaking secrets.
    """

    def __init__(
        self,
        logger: logging.Logger | None = None,
        level: int = logging.INFO,
    ) -> None:
        self._log = logger or logging.getLogger("linch.observability")
        self._level = level

    def on_run_start(self, info: RunInfo) -> None:
        self._log.log(
            self._level,
            "run.start  run_id=%.8s model=%s",
            info.run_id,
            info.model,
        )

    def on_run_end(self, info: RunResultInfo) -> None:
        tokens = (info.total_usage.input_tokens or 0) + (info.total_usage.output_tokens or 0)
        self._log.log(
            self._level,
            "run.end    run_id=%.8s subtype=%s duration_ms=%d total_tokens=%d",
            info.run_id,
            info.subtype,
            info.duration_ms,
            tokens,
        )

    def on_turn_end(self, info: TurnInfo) -> None:
        self._log.log(
            self._level,
            "turn.end   run_id=%.8s turn=%d",
            info.run_id,
            info.turn_index,
        )

    def on_provider_call_end(self, info: ProviderCallResult) -> None:
        self._log.log(
            self._level,
            "llm.end    run_id=%.8s turn=%d model=%s stop=%s duration_ms=%d in=%d out=%d",
            info.run_id,
            info.turn_index,
            info.model,
            info.stop_reason,
            info.duration_ms,
            info.usage.input_tokens or 0,
            info.usage.output_tokens or 0,
        )

    def on_tool_end(self, info: ToolResultInfo) -> None:
        self._log.log(
            self._level,
            "tool.end   run_id=%.8s tool=%s use_id=%.8s error=%s duration_ms=%d",
            info.run_id,
            info.tool_name,
            info.tool_use_id,
            info.is_error,
            info.duration_ms,
        )


class SpanCollector(BaseObserver):
    """Collects completed spans in memory for tests and introspection.

    Use this in tests to assert that the expected spans were emitted::

        collector = SpanCollector()
        agent = Agent(..., observers=[collector])
        session = await agent.session()
        async for _ in session.run("hello"):
            pass

        assert collector.run_span is not None
        assert collector.run_span.duration_ms > 0
        tool_spans = collector.tool_spans
    """

    def __init__(self) -> None:
        self._spans: list[Span] = []
        self._events: list[Any] = []
        # Open-span tracking (start timestamps keyed by ID)
        self._run_start: dict[str, float] = {}
        self._turn_start: dict[tuple[str, int], float] = {}
        self._provider_start: dict[tuple[str, int], float] = {}
        self._tool_start: dict[str, float] = {}

    # ── on_event ──────────────────────────────────────────────────────────

    def on_event(self, event: Any) -> None:
        self._events.append(event)

    # ── Run span ──────────────────────────────────────────────────────────

    def on_run_start(self, info: RunInfo) -> None:
        self._run_start[info.run_id] = time.perf_counter()

    def on_run_end(self, info: RunResultInfo) -> None:
        self._run_start.pop(info.run_id, None)
        self._spans.append(
            Span(
                kind="run",
                run_id=info.run_id,
                name="agent.run",
                duration_ms=info.duration_ms,
                attributes={
                    "subtype": info.subtype,
                    "stop_reason": info.stop_reason,
                    "input_tokens": info.total_usage.input_tokens,
                    "output_tokens": info.total_usage.output_tokens,
                },
            )
        )

    # ── Turn span ─────────────────────────────────────────────────────────

    def on_turn_start(self, info: TurnInfo) -> None:
        self._turn_start[(info.run_id, info.turn_index)] = time.perf_counter()

    def on_turn_end(self, info: TurnInfo) -> None:
        key = (info.run_id, info.turn_index)
        started = self._turn_start.pop(key, None)
        elapsed = int((time.perf_counter() - started) * 1000) if started is not None else 0
        self._spans.append(
            Span(
                kind="turn",
                run_id=info.run_id,
                name="agent.turn",
                duration_ms=elapsed,
                turn_index=info.turn_index,
            )
        )

    # ── Provider span ─────────────────────────────────────────────────────

    def on_provider_call_start(self, info: ProviderCallInfo) -> None:
        self._provider_start[(info.run_id, info.turn_index)] = time.perf_counter()

    def on_provider_call_end(self, info: ProviderCallResult) -> None:
        self._provider_start.pop((info.run_id, info.turn_index), None)
        self._spans.append(
            Span(
                kind="provider",
                run_id=info.run_id,
                name="gen_ai.chat",
                duration_ms=info.duration_ms,
                turn_index=info.turn_index,
                attributes={
                    "model": info.model,
                    "stop_reason": info.stop_reason,
                    "input_tokens": info.usage.input_tokens,
                    "output_tokens": info.usage.output_tokens,
                },
            )
        )

    # ── Tool spans ────────────────────────────────────────────────────────

    def on_tool_start(self, info: ToolInfo) -> None:
        self._tool_start[info.tool_use_id] = time.perf_counter()

    def on_tool_end(self, info: ToolResultInfo) -> None:
        self._tool_start.pop(info.tool_use_id, None)
        self._spans.append(
            Span(
                kind="tool",
                run_id=info.run_id,
                name=f"execute_tool.{info.tool_name}",
                duration_ms=info.duration_ms,
                turn_index=info.turn_index,
                tool_use_id=info.tool_use_id,
                attributes={
                    "tool_name": info.tool_name,
                    "is_error": info.is_error,
                },
            )
        )

    # ── Public API ────────────────────────────────────────────────────────

    @property
    def spans(self) -> list[Span]:
        """All completed spans in order of completion."""
        return list(self._spans)

    @property
    def events(self) -> list[Any]:
        """All events captured via ``on_event``."""
        return list(self._events)

    def spans_of(self, kind: str) -> list[Span]:
        """Return all spans of the given *kind* (``"run"``, ``"turn"``, etc.)."""
        return [s for s in self._spans if s.kind == kind]

    @property
    def run_span(self) -> Span | None:
        """The completed run span, or ``None`` if the run has not ended yet."""
        runs = self.spans_of("run")
        return runs[0] if runs else None

    @property
    def tool_spans(self) -> list[Span]:
        """All completed tool spans."""
        return self.spans_of("tool")
