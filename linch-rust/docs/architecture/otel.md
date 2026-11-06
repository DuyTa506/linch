# OpenTelemetry

> Part of the [linch-rust architecture guide](./README.md).

`linch-otel` (`crates/linch-otel/src/`) turns a run's event stream into
OpenTelemetry spans and metrics with the same names Python Linch emits. It
depends on the `opentelemetry` API crate only; the host owns the SDK and its
exporters (Langfuse, Tempo, Datadog, ...). Use this page when you change a
span, an attribute or a metric, or wire tracing into a host.

Python Linch emits spans from inside the loop
([OpenTelemetry](../../../docs/architecture/subsystems.md#310-opentelemetry)).
In Rust the core has no tracing calls. A `RunObserver` reads the run's
`Envelope`s as they stream and builds the same spans after the fact, each
started at its opening event's `ts_ms` and ended at its closing event's
(`crates/linch-otel/src/lib.rs:4-7`). Spans therefore carry the run's own
event times, and a host that does not trace pays nothing in the loop.

## Span tree

```text
invoke_agent                      init .. result (or suspended)
└─ step                           one per turn, linch.turn_index
   ├─ compaction                  .. compaction
   │  └─ chat {model}             the summary call, .. its usage
   ├─ chat {model}                context_build .. assistant / error
   └─ execute_tool {tool}         tool_call_start .. tool_call_end
```

(`lib.rs:9-16`.) A step ends when the turn's tool results (or stop feedback)
arrive as a `user` event (`crates/linch-otel/src/observer.rs:243-259`). The
run span ends on `result` or `suspended`. `chat` spans are `SpanKind::Client`;
the others are `Internal`. A span never ends before it starts, even when the
wall clock steps back (`crates/linch-otel/src/span.rs:73-77`).

## Names and attributes

Every key is a constant in `crates/linch-otel/src/keys.rs`, spelled as in
Python's `src/linch/_telemetry.py`. The `gen_ai.*` keys follow the GenAI
semantic conventions, which are still experimental; keeping them in one file
keeps a convention update local.

| Span | Attributes |
|---|---|
| `invoke_agent` | `gen_ai.operation.name`, `gen_ai.provider.name`, `gen_ai.request.model`, `gen_ai.conversation.id` (the session), `linch.run_id`, `linch.resumed`; at the end the four `gen_ai.usage.*` token counts, `linch.stop_reason` and `linch.cost_usd` |
| `step` | The same identity attributes plus `linch.turn_index` |
| `chat {model}` | Identity attributes and turn, `gen_ai.response.finish_reasons`, `gen_ai.response.model` (the `usage` event's `actual_model`), the four token counts, `linch.cost_usd` |
| `compaction` | Identity attributes and turn, `linch.compaction.strategy`; it starts the `compaction` event's `duration_ms` before that event, and holds the summary call's `chat` from there to its `usage` |
| `execute_tool {tool}` | `gen_ai.operation.name`, `gen_ai.conversation.id`, `gen_ai.tool.name`, `gen_ai.tool.call.id`, `linch.run_id`, `linch.turn_index` |

An attribute whose value the observer does not know is left out rather than
filled with a placeholder.

The token attributes are `gen_ai.usage.input_tokens`,
`gen_ai.usage.output_tokens`, `gen_ai.usage.cache_read.input_tokens` and
`gen_ai.usage.cache_creation.input_tokens` (`span.rs:99-110`), from the four
`Usage` buckets. As the GenAI convention says, `input_tokens` includes the
cached tokens: it is `Usage::total_input_tokens()`, the uncached input plus
both cache buckets.

What differs from Python:

| Name | Rust |
|---|---|
| `linch.cost_usd` | Rust only: the call's or the run's cost when the agent prices its model (`keys.rs:59-61`) |
| `gen_ai.input.messages` on `chat` | Not set: the stream does not carry the request's messages |
| `linch.store.duration` | Not built: the stream carries no store calls |
| `gen_ai.provider.name` | From the `init` event's `provider`, mapped by `provider_name`; `RunObserver::provider` sets it for a stream without one |
| `gen_ai.usage.input_tokens`, the `input` token bucket | Cached tokens included, where Python counts uncached input only |

Subagent events reach the parent's stream wrapped, after the fact and without
their own times, so they are not traced (`lib.rs:104-113`).

`provider_name(id)` (`lib.rs:220`) maps a Linch provider id as Python does:
`openai-chat` and `openai-responses` are `openai`, `gemini` is `gcp.gemini`,
any other id is itself.

### Errors

`error.type` holds the error's name only, never its message, which may hold
private content; the span status is an error with an empty description
(`span.rs:61-67`).

| Source | `error.type` |
|---|---|
| An `error` event | The payload's `name`, else `run_error`; marks the open chat, the step and the run (`observer.rs:261-271`) |
| A tool call that ended in error | Its `tool_call_end` `error_code`, else `tool_error`, as Python uses the outcome's error type (`crates/linch-otel/src/observer/tools.rs`) |
| A tool end whose start is in an earlier stream (the placeholder of a call a crash interrupted) | `interrupted`, on a span started `duration_ms` before the end (`crates/linch-otel/src/observer/tools.rs:43-65`) |
| A non-success `result` with no earlier `error` event | The result subtype's wire name (`observer.rs:273-293`) |
| `RunObserver::fail(type)` | The type the host passes |

## Metrics

Per model call (`crates/linch-otel/src/observer/calls.rs:142-176`):

| Metric | Unit | Attributes |
|---|---|---|
| `gen_ai.client.operation.duration` | `s` | `gen_ai.operation.name` (`chat`), `gen_ai.provider.name`, `gen_ai.request.model`, `error.type` on failure |
| `gen_ai.client.token.usage` | `{token}` | The same, plus `gen_ai.token.type` (`input` or `output`) |

Both are histograms with the SDK's default buckets, created once on the meter
the host passes (`lib.rs:152-172`). Token usage records two buckets: `input`,
with the cached tokens, and `output`. A call with unknown usage records its
duration and no tokens.

## Content capture

Content (the prompt, answers, tool input and results) is left out unless
capture is on. `Telemetry::observer()` starts at
`capture_content_from_env()` (`lib.rs:209`), which is true when
`OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` is `true` in any case,
Python's switch. `RunObserver::capture_content(bool)` overrides it per run.

| Captured | Attribute | Span |
|---|---|---|
| The user prompt's text | `gen_ai.input.messages` | `invoke_agent` |
| The final text | `gen_ai.output.messages` | `invoke_agent` |
| Each assistant message | `gen_ai.output.messages` | `chat` |
| Tool input | `gen_ai.tool.call.arguments` | `execute_tool` |
| Tool result text (the content's JSON when it holds an image) | `gen_ai.tool.call.result` | `execute_tool` |

Values are JSON text. Content is serialized only when the span is recording,
so a sampled-out span costs nothing extra (`span.rs:52-55`).

## What hosts wire

1. Build the SDK providers and exporters, and make a tracer and a meter for
   the scope `linch_otel::SCOPE` (`"linch"`, as Python names it,
   `lib.rs:130`).
2. Make one `Telemetry::new(tracer, &meter)` (`lib.rs:152`) and share it; it is
   cheap to clone. `Telemetry::global()` (`lib.rs:176`) uses the process-global
   providers instead; nothing else in the crate reads or installs global state.
3. For every run, resume or approve, make an observer with
   `telemetry.observer()` and set what the stream does not carry:
   `.provider(id)` before the `init` event names it, and, for a stream that
   does not start with `init`, `.session_id(..)`, `.model(..)` and `.turn(..)`.
   Every run, resume and approve stream starts with `init` (a resume's is
   `resumed`, with the checkpoint's `turn`); a stream without one is traced
   as resumed with those attributes from the host (`observer.rs:86-112`).
   `.parent(cx)` nests the run span under the host's request span; without it
   the run span is a trace root.
4. Feed every envelope, in order, with `observe` (`observer.rs:137`). When the
   stream ends with an error, call `fail` with the error's type name, or drop
   the observer: either ends every open span at the wall clock
   (`observer.rs:172`, `observer.rs:329-335`).
5. Read `trace_id()` (`observer.rs:132`) to correlate logs or a response with
   the trace. The events' own `trace_id` (on `init`, `result` and each
   `ToolContext`) is the one the host gave in `RunOptions::trace_id`, kept
   with the run; pass the span's there to have both agree.

The observer never panics on an order no run produces: it traces as far as
the events make sense, and ignores envelopes of another run or anything after
the run's end (`observer.rs:30-37`). `Telemetry` and `RunObserver` are `Send`,
so an observer can move between tasks with its stream (`lib.rs:260-265`).

## Example

This compiles and runs at this revision. It traces a scripted run into the
SDK's in-memory exporter:

```rust
use std::sync::Arc;

use futures::StreamExt;
use linch_core::provider::scripted::TurnBuilder;
use linch_core::{
    Agent, AgentBuilder, AgentSpec, MemoryRunStore, MemorySessionStore, PermissionEngine,
    PermissionMode, PermissionPolicy, RunOptions, ScriptedProvider,
};
use linch_otel::{SCOPE, Telemetry};
use opentelemetry::metrics::MeterProvider as _;
use opentelemetry::trace::TracerProvider as _;
use opentelemetry_sdk::metrics::SdkMeterProvider;
use opentelemetry_sdk::trace::{InMemorySpanExporter, SdkTracerProvider};

/// Run `prompt` and trace it: one observer per run, fed every envelope.
async fn traced_run(agent: &Agent, telemetry: &Telemetry, prompt: &str) -> Option<String> {
    let mut observer = telemetry.observer().provider("anthropic").capture_content(false);
    let mut stream = agent.run(prompt, RunOptions::new("s1"));
    while let Some(item) = stream.next().await {
        match item {
            Ok(envelope) => observer.observe(&envelope),
            // A type name only: an error message may hold private content.
            Err(_) => observer.fail("RunError"),
        }
    }
    observer.trace_id()
}

#[tokio::main(flavor = "current_thread")]
async fn main() {
    // The host owns the SDK and its exporters.
    let spans = InMemorySpanExporter::default();
    let tracer_provider = SdkTracerProvider::builder().with_simple_exporter(spans.clone()).build();
    let meter_provider = SdkMeterProvider::builder().build();
    let telemetry = Telemetry::new(tracer_provider.tracer(SCOPE), &meter_provider.meter(SCOPE));

    let provider = ScriptedProvider::new([TurnBuilder::new("m").text("hello").build()]);
    let permissions =
        PermissionEngine::new(PermissionPolicy::new(PermissionMode::Default, "/")).unwrap();
    let agent = AgentBuilder::new(AgentSpec::new("m"))
        .provider(Arc::new(provider))
        .permissions(permissions)
        .run_store(Arc::new(MemoryRunStore::default()))
        .session_store(Arc::new(MemorySessionStore::default()))
        .namespace("docs")
        .build()
        .unwrap();

    let trace_id = traced_run(&agent, &telemetry, "hi").await;
    assert_eq!(trace_id.map(|id| id.len()), Some(32));
    let mut names: Vec<_> = spans
        .get_finished_spans()
        .unwrap()
        .into_iter()
        .map(|span| span.name.into_owned())
        .collect();
    names.sort();
    assert_eq!(names, ["chat m", "invoke_agent", "step"]);
}
```

## Tests

`crates/linch-otel/tests/` checks the run tree (`run_tree.rs`), span times
(`timeline.rs`), content capture (`content.rs`) and failure marking
(`failures.rs`) against the SDK's in-memory exporters.

---

Back to the [architecture index](./README.md).
