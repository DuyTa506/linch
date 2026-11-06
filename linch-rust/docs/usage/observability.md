# Observability

Use OpenTelemetry when you want traces and metrics of your agents' runs in
Langfuse, Tempo, Datadog or another collector.

[← Usage guide](./README.md)

The event stream stays the way your application renders progress and results
(see [Events](./events.md)). For operations, the client turns each run's
stream into OpenTelemetry spans and metrics with the names Python Linch uses.
It depends on `opentelemetry-api` only: your host owns the SDK and its
exporters.

---

## Install

```bash
pip install 'linch-rust[otel]'          # adds opentelemetry-api
pip install opentelemetry-sdk           # the host's SDK
pip install opentelemetry-exporter-otlp # or the exporter your collector needs
```

With the extra installed, every agent `Server.open_agent` opens is traced.
Without it, `linch_rust.otel` is never imported and runs cost nothing extra.
With the extra but no SDK provider set, nothing is built either: the API's
default providers count as off.

---

## Trace an agent

Pass your providers with `telemetry=`, so different agents can export to
different places. This snippet keeps spans and metrics in memory to print
them; install an exporter for real use.

```python
import asyncio

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from linch_rust import AgentConfig, Server, tool
from linch_rust.otel import Telemetry
from linch_rust.scripted import MODEL, text_turn, tool_turn

exporter = InMemorySpanExporter()
tracer_provider = TracerProvider()
tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
reader = InMemoryMetricReader()
meter_provider = MeterProvider(metric_readers=[reader])
telemetry = Telemetry(tracer_provider=tracer_provider, meter_provider=meter_provider)


@tool(scope="read")
async def lookup(email: str) -> str:
    """Find a customer by email."""
    return "Ada Lovelace, customer since 2019"


config: AgentConfig = {
    "namespace": "demo",
    "spec": {"model": MODEL},
    "provider": {"id": "scripted", "turns": [
        tool_turn("c1", "lookup", {"email": "ada@example.com"}),
        text_turn("Ada has been a customer since 2019."),
    ]},
}


async def main() -> None:
    tracer = tracer_provider.get_tracer("my-service")
    async with Server() as server:
        agent = await server.open_agent(config, tools=[lookup], telemetry=telemetry)
        with tracer.start_as_current_span("POST /ask") as request:
            run = await agent.run("s1", "Who is ada@example.com?", run_id="ask-1")
        await run.result()  # read the run to its end, or its spans stay open

    spans = sorted(exporter.get_finished_spans(), key=lambda span: (span.start_time, span.end_time))

    def show(parent, indent: str) -> None:
        for span in spans:
            if (span.parent.span_id if span.parent else None) == parent:
                print(indent + span.name)
                show(span.context.span_id, indent + "  ")

    show(None, "")
    run_span = next(span for span in spans if span.name == "invoke_agent")
    print("attributes:", {k: v for k, v in run_span.attributes.items() if k.startswith(("gen_ai.", "linch."))})
    same = run_span.context.trace_id == request.get_span_context().trace_id
    print("same trace as the request:", same)
    metrics = reader.get_metrics_data().resource_metrics[0].scope_metrics[0]
    print("metrics:", metrics.scope.name, sorted(metric.name for metric in metrics.metrics))


asyncio.run(main())
```

```text
POST /ask
  invoke_agent
    step
      chat scripted-model
      execute_tool lookup
    step
      chat scripted-model
attributes: {'gen_ai.operation.name': 'invoke_agent', 'gen_ai.provider.name': 'scripted', 'gen_ai.request.model': 'scripted-model', 'gen_ai.conversation.id': 's1', 'linch.run_id': 'ask-1', 'linch.resumed': False, 'gen_ai.usage.input_tokens': 6, 'gen_ai.usage.output_tokens': 2, 'gen_ai.usage.cache_read.input_tokens': 0, 'gen_ai.usage.cache_creation.input_tokens': 0, 'linch.stop_reason': 'end_turn'}
same trace as the request: True
metrics: linch ['gen_ai.client.operation.duration', 'gen_ai.client.token.usage']
```

Two rules follow from how the spans are built:

- **The parent is the context current at the call.** `Agent.run`, `resume`
  and `approve` take the current context when they are awaited, not while you
  iterate. Start your request span around that call, as above, to nest the run
  under it.
- **Read every run to its end.** The spans end on the run's `result` or
  `suspended` event, or when the stream fails. A run whose iterator you
  abandon keeps its spans open, and they are never exported. Iterate to the
  end or call `Run.result()`.

---

## Use the global providers

Without `telemetry=`, an agent uses the global tracer and meter providers,
read again at every run, so providers you set after opening the agent still
take effect.

```python
import asyncio

from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import ConsoleMetricExporter, PeriodicExportingMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

from linch_rust import AgentConfig, Server
from linch_rust.scripted import MODEL, text_turn

# Set once at startup; swap the console exporters for OTLP ones in production.
tracer_provider = TracerProvider()
tracer_provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
trace.set_tracer_provider(tracer_provider)
meter_provider = MeterProvider(metric_readers=[PeriodicExportingMetricReader(ConsoleMetricExporter())])
metrics.set_meter_provider(meter_provider)

config: AgentConfig = {
    "namespace": "demo",
    "spec": {"model": MODEL},
    "provider": {"id": "scripted", "turns": [text_turn("Hello.")]},
}


async def main() -> None:
    async with Server() as server:
        agent = await server.open_agent(config)  # no telemetry=: the global providers
        await (await agent.run("s1", "Say hello.")).result()
    tracer_provider.shutdown()  # flush before exit
    meter_provider.shutdown()


asyncio.run(main())
```

This prints the `invoke_agent`, `step` and `chat` spans and the two metrics
as JSON. For a collector, use `OTLPSpanExporter` and `OTLPMetricExporter`
from `opentelemetry-exporter-otlp` in place of the console exporters.

---

## Spans and metrics

```text
invoke_agent                      init .. result (or suspended)
└─ step                           one per turn, linch.turn_index
   ├─ compaction                  .. compaction
   │  └─ chat {model}             the summary call, .. its usage
   ├─ chat {model}                context_build .. assistant / error
   └─ execute_tool {tool}         tool_call_start .. tool_call_end
```

Each span starts at its opening event's server time (`Envelope.ts_ms`) and
ends at its closing event's. The instrumentation scope is `linch`.

| Span | Attributes |
|---|---|
| `invoke_agent` | `gen_ai.operation.name`, `gen_ai.provider.name`, `gen_ai.request.model`, `gen_ai.conversation.id` (the session), `linch.run_id`, `linch.resumed`; at the end the four `gen_ai.usage.*` token counts, `linch.stop_reason`, `linch.cost_usd` |
| `step` | The same identity attributes and `linch.turn_index` |
| `chat {model}` | Identity attributes and turn, `gen_ai.response.finish_reasons`, the four token counts, `linch.cost_usd` |
| `compaction` | Identity attributes and turn, `linch.compaction.strategy` |
| `execute_tool {tool}` | `gen_ai.operation.name`, `gen_ai.tool.name`, `gen_ai.tool.call.id`, `gen_ai.conversation.id`, `linch.run_id`, `linch.turn_index` |

`gen_ai.provider.name` comes from the config's provider id (`openai-chat` and
`openai-responses` report `openai`, `gemini` reports `gcp.gemini`), and
`gen_ai.request.model` from `spec.model` until the run's `init` event names
it. `linch.cost_usd` is set when the model has a price.

`gen_ai.usage.input_tokens` counts every input token, cached ones included,
as the GenAI semantic convention says: the uncached input plus
`gen_ai.usage.cache_read.input_tokens` plus
`gen_ai.usage.cache_creation.input_tokens`. For the uncached count, take the
two cache counts away from `input_tokens`.

Each model call records two histograms with the SDK's default buckets:

| Metric | Unit | Attributes |
|---|---|---|
| `gen_ai.client.operation.duration` | `s` | `gen_ai.operation.name`, `gen_ai.provider.name`, `gen_ai.request.model`, `error.type` on failure |
| `gen_ai.client.token.usage` | `{token}` | The same, and `gen_ai.token.type` (`input` or `output`); the `input` bucket includes the cached tokens, as `gen_ai.usage.input_tokens` does |

`error.type` holds a name, never a message:

| Cause | `error.type` |
|---|---|
| An `error` event, such as a failed provider call | Its `name`; it marks the chat, the step and the run |
| A tool call that ended in error | `tool_error` |
| A tool end whose start was in an earlier stream (a call a crash interrupted) | `interrupted` |
| A `result` that is not `success`, with no `error` event | Its subtype, such as `aborted` |
| A stream that failed | The exception's class name, such as `RunError` or `ConnectionClosed` |

A resumed or approved call has no `init` event: its spans have
`linch.resumed: true` and no session id or turn index. A subagent's child run
reaches the parent's stream as wrapped events and gets no spans of its own.

---

## Content capture

The prompt, the answers, and tool inputs and results are left out unless
`OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` is `true` (in any case).
The client reads it at the start of each run.

| Captured | Attribute | Span |
|---|---|---|
| The prompt's text | `gen_ai.input.messages` | `invoke_agent` |
| The final text | `gen_ai.output.messages` | `invoke_agent` |
| Each assistant message | `gen_ai.output.messages` | `chat` |
| Tool input | `gen_ai.tool.call.arguments` | `execute_tool` |
| Tool result text | `gen_ai.tool.call.result` | `execute_tool` |

Values are JSON text, and are built only for spans that record.

---

## Correlate logs and traces

`run.trace_id` is the trace id of a traced run's spans, as 32 hex digits, for
a log line or a response header. The run span starts with the first envelope,
so read it after that; before it, `run.trace_id` is the trace of the span
current at the call, or `None` outside any span. It is `None` for a run that
is not traced. `InitEvent.trace_id` and `ResultEvent.trace_id` stay `None`:
the server does not set them.

```python
import asyncio
import logging

from opentelemetry.sdk.trace import TracerProvider

from linch_rust import AgentConfig, Server
from linch_rust.otel import Telemetry
from linch_rust.scripted import MODEL, text_turn

logging.basicConfig(level=logging.INFO, format="%(message)s")
telemetry = Telemetry(tracer_provider=TracerProvider())
config: AgentConfig = {
    "namespace": "demo",
    "spec": {"model": MODEL},
    "provider": {"id": "scripted", "turns": [text_turn("Hello.")]},
}


async def main() -> None:
    async with Server() as server:
        agent = await server.open_agent(config, telemetry=telemetry)
        run = await agent.run("s1", "Say hello.")
        result = await run.result()
        logging.info("run %s trace %s: %s", run.id, run.trace_id, result.final_text)


asyncio.run(main())
```

```text
run 21c62618-fb4c-4160-bf07-742f4487880f trace 4f6060a55bd5884c81cbd82583b32a9c: Hello.
```

Log `run.id` as well: it is `linch.run_id` on every span of the run. A resume
or approve is its own `Run`, with its own trace unless you start them under
one span of yours.

---

## What differs from Python Linch

| Python Linch | linch-rust client |
|---|---|
| Spans are made inside the loop | The client builds them from the event stream, at the events' times |
| Providers come from the agent's `Context` | `open_agent(..., telemetry=Telemetry(...))` per agent |
| `OTEL_PYTHON_TRACER_PROVIDER` is read once at import | The global providers are read at every run |
| `linch.store.duration` | Not built: the stream carries no store calls |
| `gen_ai.response.model`, `gen_ai.input.messages` on `chat` | Not set: the stream does not carry them |
| No cost attribute | `linch.cost_usd` on `chat` and `invoke_agent` |
| `gen_ai.usage.input_tokens` and the `input` token bucket count uncached input only | Both include the cached tokens, as the GenAI convention says |
| `trace_id` on run events and `ToolContext.trace_id` | `None`; read `Run.trace_id` |
| `TraceIdFilter` for log records | Not available |
| Child runs get spans of their own | A child run has no spans |

The Rust side of the same mapping is `linch-otel`; see
[OpenTelemetry](../architecture/otel.md).

---

## Related pages

- [Events](./events.md): the stream the spans are built from
- [Runs](./runs.md): resume and approve, whose spans are marked resumed
- [Server](./server.md): the sidecar process the events come from
