"""A ``tools/execute`` wrapper that records per-tool execution duration."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from linch.tools.pipeline import ToolExecution

# Called with (tool_name, duration_ms, error) after each wrapped tool body.
MetricsSink = Callable[[str, int, BaseException | None], None]

logger = logging.getLogger(__name__)


def metrics_wrapper(sink: MetricsSink) -> Callable[..., Any]:
    """Build an around-wrapper that times the wrapped tool body and reports it.

    Args:
        sink: Receives ``(tool_name, duration_ms, error)``; *error* is the
            exception the body raised, or ``None`` on success. The exception is
            re-raised after *sink* is called.

    Returns:
        An ``async def wrapper(execution, next)`` for
        :meth:`ToolPipeline.on_execute`.
    """

    async def wrapper(execution: ToolExecution, next: Callable[[], Any]) -> Any:
        started = time.perf_counter()
        error: BaseException | None = None
        try:
            return await next()
        except BaseException as exc:  # noqa: BLE001 — timed, reported, re-raised
            error = exc
            raise
        finally:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            try:
                sink(execution.tool_name, elapsed_ms, error)
            except BaseException:  # noqa: BLE001 — observers never own execution
                # A metrics backend is observational. Its failure must not
                # replace a tool value, the tool's original error, or task
                # cancellation propagating through ``next()``.
                try:
                    logger.warning("tool pipeline metrics sink failed", exc_info=True)
                except BaseException:  # noqa: B036 — diagnostics are best effort too
                    pass

    wrapper.resume_policy_id = "linch.tools.wrappers.metrics"  # type: ignore[attr-defined]
    wrapper.resume_policy_version = "1"  # type: ignore[attr-defined]
    wrapper.resume_policy_config = {}  # type: ignore[attr-defined]
    return wrapper
