"""Private OpenTelemetry API instrumentation; SDK/exporter lifecycle belongs to hosts.

Keep experimental GenAI attribute names here so semantic convention updates are
local. Services are agent-scoped; nothing here installs a process-global provider.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable
from time import perf_counter
from typing import TYPE_CHECKING, TypeVar

from opentelemetry import metrics, trace

if TYPE_CHECKING:
    from .kernel.context import Context

OPERATION = "gen_ai.operation.name"
PROVIDER = "gen_ai.provider.name"
MODEL = "gen_ai.request.model"
RESPONSE_MODEL = "gen_ai.response.model"
FINISH_REASONS = "gen_ai.response.finish_reasons"
INPUT_TOKENS = "gen_ai.usage.input_tokens"
OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
CACHE_READ_TOKENS = "gen_ai.usage.cache_read.input_tokens"
CACHE_CREATION_TOKENS = "gen_ai.usage.cache_creation.input_tokens"
CONVERSATION_ID = "gen_ai.conversation.id"
TOOL_NAME = "gen_ai.tool.name"
TOOL_CALL_ID = "gen_ai.tool.call.id"
INPUT_MESSAGES = "gen_ai.input.messages"
OUTPUT_MESSAGES = "gen_ai.output.messages"
TOOL_ARGUMENTS = "gen_ai.tool.call.arguments"
TOOL_RESULT = "gen_ai.tool.call.result"
TOKEN_TYPE = "gen_ai.token.type"
RUN_ID = "linch.run_id"
TURN_INDEX = "linch.turn_index"
RESUMED = "linch.resumed"
STOP_REASON = "linch.stop_reason"
COMPACTION_STRATEGY = "linch.compaction.strategy"
ERROR_TYPE = "error.type"
STORE_OP = "op"
TOKEN_USAGE = "gen_ai.client.token.usage"
OPERATION_DURATION = "gen_ai.client.operation.duration"
STORE_DURATION = "linch.store.duration"

_T = TypeVar("_T")


def set_error(span: trace.Span, error: BaseException | str) -> None:
    """Record error identity only: exception messages may contain private content."""
    span.set_attribute(ERROR_TYPE, error if isinstance(error, str) else type(error).__name__)
    span.set_status(trace.StatusCode.ERROR)


async def in_span(iterator: AsyncIterator[_T], span: trace.Span) -> AsyncIterator[_T]:
    """Activate only while advancing/closing, never across a consumer yield."""
    try:
        while True:
            with trace.use_span(span, end_on_exit=False, record_exception=False):
                try:
                    item = await anext(iterator)
                except StopAsyncIteration:
                    return
            yield item
    except BaseException as exc:
        set_error(span, exc)
        raise
    finally:
        try:
            with trace.use_span(span, end_on_exit=False, record_exception=False):
                close = getattr(iterator, "aclose", None)
                if close is not None:
                    await close()
        finally:
            span.end()


class Telemetry:
    """Agent-scoped API instruments resolved through the agent's Context."""

    def __init__(self, context: Context) -> None:
        self.tracer: trace.Tracer = context.get("tracer") or trace.get_tracer(
            "linch", tracer_provider=context.get("tracer_provider")
        )
        meter: metrics.Meter = context.get("meter") or metrics.get_meter(
            "linch", meter_provider=context.get("meter_provider")
        )
        self.tokens = meter.create_histogram(TOKEN_USAGE, unit="{token}")
        self.duration = meter.create_histogram(OPERATION_DURATION, unit="s")
        self.store_duration = meter.create_histogram(STORE_DURATION, unit="s")

    async def store_call(self, op: str, awaitable: Awaitable[_T]) -> _T:
        started = perf_counter()
        attributes = {STORE_OP: op}
        try:
            return await awaitable
        except BaseException as exc:
            attributes[ERROR_TYPE] = type(exc).__name__
            raise
        finally:
            self.store_duration.record(perf_counter() - started, attributes)
