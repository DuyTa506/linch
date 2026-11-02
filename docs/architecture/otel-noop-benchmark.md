# OpenTelemetry fast-path benchmark

Use this page when changing or measuring Linch's OpenTelemetry paths. It records
the current source-backed behavior and reproducible checks; timings are
machine- and environment-dependent and are not a cross-environment performance
target.

## `trace_run`, `in_span`, and `ChatCall`

Core instrumentation lives in `src/linch/_telemetry.py` and depends on
`opentelemetry-api`. The host owns the OpenTelemetry SDK, exporters, readers,
and their lifecycle. Linch resolves tracer and meter services through the
agent's `Context`, falling back to the OpenTelemetry API providers when no
scoped service is registered.

The fast paths are observable in the current implementation:

- `in_span(iterator, span, ...)` returns the exact input iterator when the span
  context is invalid. For a valid context it activates the span around each
  `anext`, closes the underlying iterator, restores the caller's context, and
  ends the span when requested.
- `trace_run` wraps the runner with `invoke_agent` instrumentation. A no-op
  tracer therefore keeps the raw run iterator path, while a valid span uses the
  activation wrapper.
- `ChatCall` owns the provider-call boundary used by the streaming loop. At
  completion a recording span receives the response model, finish reason,
  usage, and optional captured output; enabled metrics record operation
  duration and input/output token histograms.
- `trace_tool` and `trace_compaction` instrument tool execution and compaction.
  Store operations use `store_call` and are metrics-only; they do not create
  spans.

Only a valid span context activates tracing. A valid sampled-out span still
propagates context, while `is_recording()` controls recorded span attributes
and content. Prompt, tool-argument, tool-result, and final-answer content is
captured only when a recording span is active and the host sets
`OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true`.

## Method

The repository benchmark is an offline runtime correctness/performance harness.
Its default provider is an in-memory `DeltaProvider`; each case validates the
typed event stream and successful final result before reporting timing samples.
The script emits JSON with schema version 2 and a named `metrics` map. Run a
small local sample with the current case names as follows:

```bash
PYTHONPATH=src python scripts/benchmark_runtime.py \
  --warmups 2 --samples 10 \
  --case no_tool_stream_200_deltas \
  --case parallel_8_read_tools --json
```

The command reports medians, nearest-rank p95 values, minimums, maximums, the
sample distribution, and event counts. Do not copy its timing output into this
page: the result depends on Python, hardware, scheduler load, and the selected
sample counts. `tests/scripts/test_runtime_benchmark.py` checks the JSON schema,
warmup handling, percentile calculation, event validation, and the two cases;
`tests/scripts/test_runtime_benchmark_scale.py` covers the separate opt-in
memory/Postgres scale harness.

Inject host-owned providers before constructing an agent. The public wiring is
the same for tests and production hosts:

```python
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.trace import TracerProvider

from linch import Agent, Context

host_context = Context()
host_context.register("tracer_provider", TracerProvider())
host_context.register("meter_provider", MeterProvider())
agent = Agent(model="your-model", provider=my_provider, context=host_context)
```

The source-backed verification suite is offline and uses in-memory SDK
providers/readers. Run it with:

```bash
PYTHONPATH=src python -m pytest -q \
  tests/observability/test_otel_fast_path.py \
  tests/observability/test_otel_runtime.py \
  tests/observability/test_telemetry.py
```

The tests cover raw-iterator pass-through for invalid/no-op spans, metrics with
a no-op tracer, propagation from a sampled-out caller, span parentage and
operation names, usage and duration metrics, store metrics, late provider
registration, cancellation/cleanup, and the content-capture switch.

## Non-timing evidence and verification

The source and tests provide the behavior to preserve when changing the fast
path:

- `tests/observability/test_otel_fast_path.py` forbids `trace.use_span` on the
  no-op stream and asserts exact raw-iterator identity for invalid/no-op paths.
- `tests/observability/test_otel_runtime.py` asserts the
  `invoke_agent` → `step` → `chat` tree, `execute_tool` and compaction spans,
  usage/finish attributes, store-duration metrics, and content redaction.
- `tests/observability/test_telemetry.py` exercises context restoration,
  background-tool span ownership, late Context/global provider refresh, and
  disposal/replacement behavior.

These tests are the regression contract. They do not establish an exporter or
backend-specific latency target.

## Surface and scope

The current span operations are:

| Span | Boundary |
| --- | --- |
| `invoke_agent` | One run, including its run id and resume marker. |
| `step` | One turn. |
| `chat <model>` | One provider stream, including usage and finish reason. |
| `execute_tool <name>` | One tool execution. |
| `compaction` | One compaction operation. |

The histograms are `gen_ai.client.token.usage` (`{token}`),
`gen_ai.client.operation.duration` (`s`), and `linch.store.duration` (`s`,
with an `op` attribute). Store calls are not spans. Error telemetry records
error identity (`error.type`) rather than exception text.

The `gen_ai.*` and `linch.*` attribute names remain private to the telemetry
adapter. Hosts configure exporters and OTLP endpoints; Linch does not install a
vendor adapter or a process-global SDK provider.
