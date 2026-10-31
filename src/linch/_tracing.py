"""Small trace-correlation helpers shared by runtime boundaries."""

from __future__ import annotations

import logging

from opentelemetry import trace


def trace_id_for(run_id: str | None) -> str | None:
    """Return the active trace ID or the run's stable correlation fallback."""
    return span_trace_id(trace.get_current_span()) or run_id


def span_trace_id(span: trace.Span) -> str | None:
    """Return a formatted trace ID for *span* when its context is valid."""
    context = span.get_span_context()
    if not context.is_valid:
        return None
    return f"{context.trace_id:032x}"


class TraceIdFilter(logging.Filter):
    """Attach a trace ID to log records without installing global logging state.

    Hosts opt in by adding this filter to their handler or logger. An explicit
    ``record.trace_id`` wins, followed by the active OpenTelemetry span,
    ``record.run_id``, and finally the configured fallback.

    Args:
        fallback: Correlation value used when no record or active span provides one.
    """

    def __init__(self, fallback: str | None = None) -> None:
        super().__init__()
        self._fallback = fallback

    def filter(self, record: logging.LogRecord) -> bool:
        """Populate ``record.trace_id`` while allowing the record through."""
        if getattr(record, "trace_id", None):
            return True
        trace_id = span_trace_id(trace.get_current_span())
        if trace_id is None:
            run_id = getattr(record, "run_id", None)
            trace_id = str(run_id) if run_id else self._fallback
        record.trace_id = trace_id
        return True
