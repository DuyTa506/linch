"""OpenTelemetry observer adapter (requires ``pip install 'linch[otel]'``).

Maps Linch span hooks to OTel spans in a run → turn → (provider | tool)
nesting.  The caller is responsible for configuring a TracerProvider and
exporter before constructing this observer.  ``aclose()`` is a no-op because
the host owns the TracerProvider lifecycle.

Langfuse, LangSmith, Honeycomb, Datadog, and Jaeger all accept OTel traces,
so this single adapter reaches any of them — no vendor-specific code in core.
"""

from __future__ import annotations

from typing import Any

from ..errors import ProviderError
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


class OpenTelemetryObserver(BaseObserver):
    """Maps Linch lifecycle hooks to OpenTelemetry spans.

    Install the required packages::

        pip install 'linch[otel]'

    Example — wire a console-exporter for local debugging::

        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
        from opentelemetry import trace
        trace.set_tracer_provider(provider)

        from linch.observability import OpenTelemetryObserver
        observer = OpenTelemetryObserver()

    To use Langfuse or LangSmith, configure their OTel endpoint instead of the
    console exporter — no Linch changes required.
    """

    def __init__(
        self,
        tracer: Any | None = None,
        *,
        service_name: str = "linch",
    ) -> None:
        # Heavy import is deferred to __init__ so that
        # `from linch.observability import OpenTelemetryObserver`
        # never pulls opentelemetry at module scope.
        try:
            from opentelemetry import trace  # noqa: F401
        except ModuleNotFoundError as exc:
            raise ProviderError(
                "OpenTelemetryObserver requires the opentelemetry packages. "
                "Install with: pip install 'linch[otel]'"
            ) from exc

        import opentelemetry.trace as _trace

        self._trace = _trace
        self._tracer = tracer or _trace.get_tracer(service_name)

        # Open span registry
        self._run_spans: dict[str, Any] = {}
        self._run_ctx_tokens: dict[str, Any] = {}
        self._turn_spans: dict[tuple[str, int], Any] = {}
        self._turn_ctx_tokens: dict[tuple[str, int], Any] = {}
        self._provider_spans: dict[tuple[str, int], Any] = {}
        self._tool_spans: dict[str, Any] = {}

    # ── Run ──────────────────────────────────────────────────────────────

    def on_run_start(self, info: RunInfo) -> None:
        import opentelemetry.context as _ctx

        span = self._tracer.start_span("agent.run")
        span.set_attribute("linch.run_id", info.run_id)
        span.set_attribute("linch.session_id", info.session_id)
        span.set_attribute("gen_ai.request.model", info.model)
        ctx = self._trace.set_span_in_context(span)
        token = _ctx.attach(ctx)
        self._run_spans[info.run_id] = span
        self._run_ctx_tokens[info.run_id] = token

    def on_run_end(self, info: RunResultInfo) -> None:
        import opentelemetry.context as _ctx
        from opentelemetry.trace import StatusCode

        span = self._run_spans.pop(info.run_id, None)
        token = self._run_ctx_tokens.pop(info.run_id, None)
        if span is None:
            return

        span.set_attribute("linch.subtype", info.subtype)
        span.set_attribute("gen_ai.usage.input_tokens", info.total_usage.input_tokens or 0)
        span.set_attribute("gen_ai.usage.output_tokens", info.total_usage.output_tokens or 0)
        span.set_attribute("linch.duration_ms", info.duration_ms)
        if info.subtype in {"error", "aborted"}:
            span.set_status(StatusCode.ERROR)
            if info.error:
                span.set_attribute("error.type", str(info.error.get("name", "unknown")))
                span.set_attribute("error.message", str(info.error.get("message", "")))

        # Defensively close any still-open child spans for this run.
        for key in [k for k in list(self._turn_spans) if k[0] == info.run_id]:
            child = self._turn_spans.pop(key, None)
            if child:
                child.end()
        for key in [k for k in list(self._provider_spans) if k[0] == info.run_id]:
            child = self._provider_spans.pop(key, None)
            if child:
                child.end()

        span.end()
        if token is not None:
            _ctx.detach(token)

    # ── Turn ─────────────────────────────────────────────────────────────

    def on_turn_start(self, info: TurnInfo) -> None:
        import opentelemetry.context as _ctx

        parent = self._run_spans.get(info.run_id)
        parent_ctx = self._trace.set_span_in_context(parent) if parent else _ctx.get_current()
        span = self._tracer.start_span("agent.turn", context=parent_ctx)
        span.set_attribute("linch.run_id", info.run_id)
        span.set_attribute("linch.turn_index", info.turn_index)
        turn_ctx = self._trace.set_span_in_context(span)
        token = _ctx.attach(turn_ctx)
        self._turn_spans[(info.run_id, info.turn_index)] = span
        self._turn_ctx_tokens[(info.run_id, info.turn_index)] = token

    def on_turn_end(self, info: TurnInfo) -> None:
        import opentelemetry.context as _ctx

        key = (info.run_id, info.turn_index)
        span = self._turn_spans.pop(key, None)
        token = self._turn_ctx_tokens.pop(key, None)
        if span is not None:
            span.end()
        if token is not None:
            _ctx.detach(token)

    # ── Provider call ─────────────────────────────────────────────────────

    def on_provider_call_start(self, info: ProviderCallInfo) -> None:
        import opentelemetry.context as _ctx

        parent = self._turn_spans.get((info.run_id, info.turn_index))
        parent_ctx = self._trace.set_span_in_context(parent) if parent else _ctx.get_current()
        span = self._tracer.start_span("gen_ai.chat", context=parent_ctx)
        span.set_attribute("linch.run_id", info.run_id)
        span.set_attribute("linch.turn_index", info.turn_index)
        span.set_attribute("gen_ai.request.model", info.model)
        self._provider_spans[(info.run_id, info.turn_index)] = span

    def on_provider_call_end(self, info: ProviderCallResult) -> None:

        span = self._provider_spans.pop((info.run_id, info.turn_index), None)
        if span is None:
            return
        span.set_attribute("gen_ai.response.finish_reasons", info.stop_reason)
        span.set_attribute("gen_ai.usage.input_tokens", info.usage.input_tokens or 0)
        span.set_attribute("gen_ai.usage.output_tokens", info.usage.output_tokens or 0)
        span.set_attribute("linch.duration_ms", info.duration_ms)
        span.end()

    # ── Tool ──────────────────────────────────────────────────────────────

    def on_tool_start(self, info: ToolInfo) -> None:
        import opentelemetry.context as _ctx

        parent = self._turn_spans.get((info.run_id, info.turn_index))
        parent_ctx = self._trace.set_span_in_context(parent) if parent else _ctx.get_current()
        span = self._tracer.start_span("execute_tool", context=parent_ctx)
        span.set_attribute("linch.run_id", info.run_id)
        span.set_attribute("linch.turn_index", info.turn_index)
        span.set_attribute("linch.tool.name", info.tool_name)
        span.set_attribute("linch.tool.use_id", info.tool_use_id)
        self._tool_spans[info.tool_use_id] = span

    def on_tool_end(self, info: ToolResultInfo) -> None:
        from opentelemetry.trace import StatusCode

        span = self._tool_spans.pop(info.tool_use_id, None)
        if span is None:
            return
        span.set_attribute("linch.tool.is_error", info.is_error)
        span.set_attribute("linch.duration_ms", info.duration_ms)
        if info.is_error:
            span.set_status(StatusCode.ERROR)
        span.end()

    async def aclose(self) -> None:
        """No-op: the host owns the TracerProvider lifecycle."""
