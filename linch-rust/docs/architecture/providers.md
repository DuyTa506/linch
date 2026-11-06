# Providers

> Part of the [linch-rust architecture guide](./README.md).

A provider turns one `ProviderRequest` into a stream of normalized
`ProviderEvent`s. The contract lives in `linch-core`
(`crates/linch-core/src/provider/`); the HTTP adapters live in
`linch-providers` (`crates/linch-providers/src/`). Use this page when you
change an adapter, add one, or touch how a model call is assembled, priced
or failed.

Python Linch has the same idea
([provider contract](../../../docs/architecture/provider-contract.md)). What
differs in Rust:

- The trait has two methods, `id` and `stream`. There is no
  `context_window`, `capabilities`, `prepare` or `aclose`; an adapter that
  cannot honor a request field fails the call with `InvalidRequest`.
- `stream` takes an explicit `CancellationToken`.
- Unknown usage is `None`, never a zero `Usage`.
- Events are a closed enum that rejects unknown fields and tags on decode.

## The contract

`Provider` (`crates/linch-core/src/provider/client.rs:21`) is
`Send + Sync` and object safe; the agent holds it as `Arc<dyn Provider>`.

| Method | Contract |
|---|---|
| `fn id(&self) -> &str` | Stable identifier (`deepseek`, `openai-chat`, `vllm`, ...). The OpenTelemetry layer maps it to `gen_ai.provider.name`. |
| `async fn stream(&self, request, cancel) -> Result<ProviderStream, ProviderError>` | Start one call. An error before the first event (connect, HTTP status, a request the adapter refuses) is the `Err`. The stream ends after `message_end` or after one error item. Cancelling `cancel` ends it with `ProviderError::Cancelled`. |

`ProviderStream` is `BoxStream<'static, Result<ProviderEvent, ProviderError>>`
(`client.rs:17`), so the stream owns everything it needs and outlives the
borrow of `self`.

### Event vocabulary

`ProviderEvent` (`crates/linch-core/src/provider/event.rs:24`) is tagged by
`type` on the wire. Every payload struct has `deny_unknown_fields`.

| Variant (`type`) | Fields |
|---|---|
| `MessageStart` (`message_start`) | `model`: the model that served the call |
| `TextDelta` (`text_delta`) | `text` |
| `ThinkingDelta` (`thinking_delta`) | `text`, `signature?` |
| `RedactedThinking` (`redacted_thinking`) | one whole encrypted reasoning block |
| `ToolUseStart` (`tool_use_start`) | `id`, `name` |
| `ToolUseInputDelta` (`tool_use_input_delta`) | `id`, `json_delta` |
| `ToolUseEnd` (`tool_use_end`) | `id` |
| `MessageEnd` (`message_end`) | `stop_reason`, `usage?`, `provider_metadata?` |

`provider_metadata` is an opaque JSON object carried onto the assembled
message. Adapters use it to replay what the vendor needs back on the next
turn: Responses output items under `openai_responses`
(`crates/linch-providers/src/responses/stream.rs:26`), Gemini raw parts under
`gemini_parts` (`crates/linch-providers/src/gemini.rs:44`).

### Stream rules

The `Assembler` (`crates/linch-core/src/provider/assembler.rs:84`) is the only
consumer of the stream and enforces the rules on every push; `assemble`
(`assembler.rs:344`) runs it over a collected list. The rules
(`assembler.rs:46-62`):

- exactly one `message_start` first, exactly one `message_end` last, nothing
  after it;
- every `tool_use_start` has a non-empty id, unique across the stream, and a
  non-empty name;
- an input delta or `tool_use_end` names a call that is open;
- every opened call ends before `message_end`, and its concatenated input
  parses as a JSON object;
- `stop_reason` is `tool_use` if and only if at least one call completed.

The one exception is a `max_tokens` stop: a call still open, or with input
that is not a whole object, is dropped instead of failing the stream. The turn
then stops with `max_tokens` if no call is left and `tool_use` if finished
calls remain. A response cut off before it held any text or whole call is
rejected by the runner (`crates/linch-core/src/runner.rs:12-16`).

Coalescing: consecutive text deltas become one text block; consecutive
thinking deltas become one thinking block until a delta that carries a
signature. A signed thinking delta with empty text still yields a block, so
the signature replays (`assembler.rs:64-81`).

### The request

`ProviderRequest` (`crates/linch-core/src/provider/request.rs:17`) has four
required fields and optional controls an adapter may ignore or refuse:

| Field | Notes |
|---|---|
| `model`, `system`, `tools`, `messages` | Always set by the runner. |
| `max_output_tokens`, `temperature`, `stop_sequences` | Optional. |
| `effort` (`ReasoningEffort`), `thinking` (`Thinking`) | `None` keeps the provider default. Each adapter maps them itself. |
| `cache_control`, `output_schema`, `tool_choice` | Optional. |

There is no cancel signal, retry budget or catalog flag in the request; the
cancel token is the second argument of `stream`.

## Errors and retries

`ProviderError` (`client.rs:39`) has five variants. Messages are written by
the adapter and must never hold credentials or request headers.

| Variant | Meaning | `retryable()` |
|---|---|---|
| `Transport { message }` | No response: connect, TLS, timeout, reset, or a broken stream | yes |
| `Status { status, retry_after, message }` | Non-success HTTP status | 429 and >= 500 only |
| `InvalidRequest { message }` | The adapter refused the request as built | no |
| `Stream(StreamError)` | The event stream broke the stream contract | no |
| `Cancelled` | The cancel token fired | no |

Nothing in linch-rust retries a provider call:

- Adapters never retry (`crates/linch-providers/src/lib.rs:4`).
- `retryable()` and `retry_after` are advice for the caller
  (`client.rs:1-5`).
- The runner's `dispatch` (`crates/linch-core/src/runner/turn.rs:217`) ends
  the run `failed` with an `Error` event on any failure but `Cancelled`, which
  aborts the run (`turn.rs:285-292`). The error event carries `retryable` and
  `status` (`crates/linch-core/src/provider/call.rs:306`), so a host can
  decide to start a new run.
- The `attempt` number of a delivery is not a retry counter. It grows only
  when a resumed run finds a call that was dispatched but never settled and
  dispatches it again (`Recovery::Redispatch`,
  `crates/linch-core/src/runner/start.rs:165`).

There is no deadline on a whole provider call. Each adapter's HTTP client has
a 30 s connect timeout and a read timeout between reads (below); cancel the
run's token to stop a call sooner.

## Shared HTTP mechanics

Every adapter builds its client with `transport::client`
(`crates/linch-providers/src/transport.rs:23`):

- **No redirects.** reqwest strips only `Authorization` on a cross-origin
  hop, so `x-api-key` and `x-goog-api-key` would follow a redirect. A 3xx
  answer becomes `Status` with a fixed message (`transport.rs:57`); it is
  not retryable.
- **Bounded error bodies.** At most 8 KiB of a non-2xx body is read
  (`transport.rs:16`, `transport.rs:34`). The summary prefers the vendor's
  error message, is redacted, then cut to 500 characters
  (`crates/linch-providers/src/openai_compat/http.rs:73`).
- **`Retry-After`** is read as whole seconds; an HTTP date is ignored. Gemini
  also reads a `RetryInfo` detail's `retryDelay`
  (`crates/linch-providers/src/gemini/http.rs:68-88`).
- **Errors inside a 200 stream** map to the HTTP status the same error would
  have had, so they retry alike (`openai_compat/http.rs:104`).

Read timeouts:

| Adapter | Read timeout | Why |
|---|---|---|
| DeepSeek, Anthropic, Gemini | 300 s | The server sends keep-alives while it works |
| OpenAI Chat (and server profiles), OpenAI Responses | 600 s | Reasoning models can think for minutes before the first token |

### The single SSE loop

All five wire formats run through one loop, `sse::events`
(`crates/linch-providers/src/sse.rs:45`). An adapter supplies a crate-private
`Decoder` (`sse.rs:19`): `data` translates one non-blank `data:` payload and
says whether the response is complete; `close` handles a body that ended
early (a format with an end marker fails, one without finishes). The loop:

- checks the cancel token before each read and races the read against it;
- once the response is complete, yields what is queued even if a cancel
  arrives, so the end and its usage are not lost (`sse.rs:99-103`);
- yields nothing after an error (`sse.rs:156`);
- scrubs the API key and every `Bearer <token>` from transport and status
  messages (`sse.rs:137`);
- appends a closing blank line so a last event with no trailing blank line
  is still dispatched (`sse.rs:57-61`).

### Credentials

`ApiKey` (`crates/linch-providers/src/api_key.rs:14`) wraps the key in an
`Arc<str>`. `Debug` prints `ApiKey([redacted])`, there is no `Display`, and
an empty or all-whitespace key is `None`. Every string that reaches a
`ProviderError` passes through `scrub` (`api_key.rs:46`), which removes the
key and any `Bearer` token, even for a keyless local server (a proxy may add
one). Each options struct has `from_env()`, which reads the adapter's
variable and returns `None` when it is unset or empty.

## The adapters

Every adapter takes the model from each request, not from its options.

| Type | `id()` | Endpoint | Auth | Key variable |
|---|---|---|---|---|
| `DeepSeek` | `deepseek` | `{base}/chat/completions` | Bearer | `DEEPSEEK_API_KEY` |
| `OpenAiChat::new` | `openai-chat` | `{base}/chat/completions` | Bearer | `OPENAI_API_KEY` |
| `OpenAiChat::server` | `vllm`, `sglang`, `llamacpp` | `{base}/chat/completions` | Bearer when a key is set | none |
| `OpenAiResponses` | `openai-responses` | `{base}/responses` | Bearer | `OPENAI_API_KEY` |
| `Anthropic` | `anthropic` | `{base}/v1/messages` | `x-api-key` | `ANTHROPIC_API_KEY` (and `ANTHROPIC_BASE_URL`) |
| `Gemini` | `gemini` | `{base}/v1beta/models/{model}:streamGenerateContent?alt=sse` | `x-goog-api-key` | `GEMINI_API_KEY` |

Each module's doc comment lists the wire rules it was checked against, with
the date; read it before changing the adapter.

### DeepSeek

`crates/linch-providers/src/deepseek.rs`. `thinking` is always sent, because
DeepSeek thinks by default; `Thinking::Disabled` turns it off and every other
state turns it on (`deepseek.rs:121`). With thinking on, every earlier
assistant turn sends `reasoning_content`, `""` when it has none, or a request
with tools fails with 400 (`deepseek.rs:139-145`). A forced `tool_choice`
with thinking on, and any `output_schema`, fail locally as `InvalidRequest`.
Effort maps `medium` and `xhigh` to `high` (`deepseek.rs:176`).

### OpenAI Chat Completions and server profiles

One type, `OpenAiChat` (`crates/linch-providers/src/openai.rs:90`), serves
OpenAI and each OpenAI-compatible server. The difference is a `ChatProfile`
(`crates/linch-providers/src/openai/profile.rs:18`), a static table with no
defaults: every target states every rule, so a server never inherits OpenAI's
behavior by accident.

| Profile | Reasoning field | Echo reasoning | Named `tool_choice` | Output limit field | Thinking off |
|---|---|---|---|---|---|
| OpenAI (`profile.rs:50`) | none | no | yes | `max_completion_tokens` | not sent |
| vLLM (`servers/vllm.rs:33`) | `delta.reasoning` | yes | yes | `max_completion_tokens` | `enable_thinking: false` |
| SGLang (`servers/sglang.rs:31`) | `delta.reasoning_content` | yes | yes | `max_completion_tokens` | `enable_thinking: false`, `thinking: false` |
| llama.cpp (`servers/llamacpp.rs:31`) | `delta.reasoning_content` | yes | no (fails locally) | `max_tokens` | `enable_thinking: false` |

A server is chosen with `ServerOptions::new(Server::Vllm, base_url)`
(`crates/linch-providers/src/servers.rs:71`), which fills the
`chat_template_kwargs` that server's docs give for turning thinking off. When
a request asks for `Thinking::Disabled`, those kwargs are sent and effort is
left out (`profile.rs:132-142`). `output_schema` is not supported by any chat
profile yet.

### OpenAI Responses

`crates/linch-providers/src/responses.rs`. System blocks become
`instructions`. Function tools are sent with `strict: false`, so the API does
not rewrite a schema into strict mode. Each response's output items,
reasoning included, are kept in `provider_metadata` and sent back verbatim;
no `previous_response_id` is sent, so every request carries its full
history. `ResponsesOptions::store` off sends `store: false` and asks for
encrypted reasoning. Stop sequences are rejected: the API has no such
parameter.

### Anthropic

`crates/linch-providers/src/anthropic.rs`. `anthropic-version` is pinned to
`2023-06-01` (`anthropic.rs:40`) because the newer version removes
`temperature`, which `ProviderRequest` still carries. `max_tokens` is
required and defaults to 8192 (`anthropic.rs:43`). Thinking blocks are sent
back with their signatures unchanged. A request's `cache_control` marks
the last cacheable system block and the last block of the last user message
(`anthropic/payload.rs:39`).
`message_delta.usage` is cumulative: a present field replaces the
`message_start` value. `pause_turn` and unknown stop reasons fail the call as
a retryable transport error (`anthropic/stream.rs:311`).

### Gemini

`crates/linch-providers/src/gemini.rs`. The model is percent-encoded as one
path segment, so `/`, `?`, `#` and `:` in a model name cannot change the URL
(`gemini.rs:52`). Function calls without an `id` get a random one per
response that is never sent back. A turn with a thought signature or a call
id keeps its raw parts in `provider_metadata` and replays them verbatim.
`cache_control` is ignored (caching is implicit). Safety-type finish reasons
become `refusal`; unknown ones fail as a retryable transport error
(`gemini/stream.rs:293`). The module notes this adapter has not yet run
against a live key.

### Usage normalization

Linch's `Usage` (`crates/linch-core/src/usage.rs:13`) counts uncached input,
output, cache reads and cache writes in four separate buckets; `input_tokens`
excludes both cache buckets. Each adapter subtracts what its vendor folds
into the prompt count: DeepSeek its cache hits (`deepseek.rs:185`), OpenAI
cached and cache-write tokens (`profile.rs:77`), vLLM cached and created
cache tokens (`servers/vllm.rs:54`), SGLang and llama.cpp cached tokens
(`servers.rs:83`).

## Pricing

`linch_providers::cost(model, &usage)` (`crates/linch-providers/src/pricing.rs:97`)
prices each bucket at its own rate from a table matched on the exact model id
(`pricing.rs:56`). The table holds Anthropic models only, at the published
rates of 2026-06-08 with cache writes at the 5-minute tier. Every other
model, including every OpenAI, DeepSeek, Gemini and local model, prices to
`None`. `ModelPricing::cost` (`pricing.rs:33`) prices with rates a host
supplies.

Pricing is opt-in on the agent: `AgentBuilder::pricing`
(`crates/linch-core/src/agent/builder.rs:120`) takes any
`Fn(&str, &Usage) -> Option<f64>`. The `linch` crate passes
`linch_providers::cost` (`crates/linch/src/open.rs:136`). When a receipt has
usage, the runner prices it by the model that served the call, else the
agent's model (`crates/linch-core/src/runner/write.rs:107`). A missing price
or a non-finite one stays `None` (`crates/linch-core/src/agent.rs:141`), and a
run's total adds only priced calls. Costs appear as `cost_usd` on the
receipt and `Usage` events and `total_cost_usd` on `Result`. Pricing is not
part of the run contract.

## Add a provider in Rust

An out-of-tree provider implements `Provider` directly; `Decoder` and
`sse::events` are crate-private. A new in-tree adapter should reuse
`transport::client`, `sse::events` and `api_key::scrub` instead of writing
its own client, loop or redaction.

Checklist:

1. Never retry and never follow a redirect.
2. Honor the cancel token; return `ProviderError::Cancelled` for it.
3. Map HTTP statuses to `Status` with `retry_after`, and redact every message.
4. Emit the vocabulary above, in order, and put `usage: None` when the vendor
   reported none.
5. Refuse a request field you cannot honor with `InvalidRequest` instead of
   dropping it.
6. State each wire rule in the module doc with the source and date you
   checked it against. Do not reuse another adapter's profile.

This compiles and runs against `linch-core` at this revision. It checks a
tool-call stream with `assemble`, then drives a run with a priced echo
provider:

```rust
use std::sync::Arc;

use async_trait::async_trait;
use futures::StreamExt;
use linch_core::provider::{
    MessageEndEvent, MessageStartEvent, TextDeltaEvent, ToolUseEndEvent, ToolUseInputDeltaEvent,
    ToolUseStartEvent,
};
use linch_core::{
    AgentBuilder, AgentSpec, Event, MemoryRunStore, MemorySessionStore, PermissionEngine,
    PermissionMode, PermissionPolicy, Provider, ProviderError, ProviderEvent, ProviderRequest,
    ProviderStream, RunOptions, StopReason, Usage, assemble,
};
use tokio_util::sync::CancellationToken;

/// Answers every request by echoing the last message's text.
struct Echo;

#[async_trait]
impl Provider for Echo {
    fn id(&self) -> &str {
        "echo"
    }

    async fn stream(
        &self,
        request: ProviderRequest,
        cancel: CancellationToken,
    ) -> Result<ProviderStream, ProviderError> {
        if cancel.is_cancelled() {
            return Err(ProviderError::Cancelled);
        }
        let text = request.messages.last().map(|m| m.text()).unwrap_or_default();
        let events = vec![
            ProviderEvent::MessageStart(MessageStartEvent { model: request.model }),
            ProviderEvent::TextDelta(TextDeltaEvent { text }),
            ProviderEvent::MessageEnd(MessageEndEvent {
                stop_reason: StopReason::EndTurn,
                usage: Some(Usage { input_tokens: 3, output_tokens: 2, ..Usage::default() }),
                provider_metadata: None,
            }),
        ];
        Ok(futures::stream::iter(events.into_iter().map(Ok)).boxed())
    }
}

#[tokio::main(flavor = "current_thread")]
async fn main() {
    // 1. The assembler checks the stream contract offline.
    let call = vec![
        ProviderEvent::MessageStart(MessageStartEvent { model: "m".into() }),
        ProviderEvent::ToolUseStart(ToolUseStartEvent { id: "c1".into(), name: "Read".into() }),
        ProviderEvent::ToolUseInputDelta(ToolUseInputDeltaEvent {
            id: "c1".into(),
            json_delta: r#"{"file_path": "a.txt"}"#.into(),
        }),
        ProviderEvent::ToolUseEnd(ToolUseEndEvent { id: "c1".into() }),
        ProviderEvent::MessageEnd(MessageEndEvent {
            stop_reason: StopReason::ToolUse,
            usage: None,
            provider_metadata: None,
        }),
    ];
    let turn = assemble(call).expect("a valid stream");
    assert_eq!(turn.message.tool_uses().count(), 1);

    // 2. The provider drives a run like any built-in adapter.
    let permissions =
        PermissionEngine::new(PermissionPolicy::new(PermissionMode::Default, "/")).unwrap();
    let agent = AgentBuilder::new(AgentSpec::new("echo-1"))
        .provider(Arc::new(Echo))
        .permissions(permissions)
        .run_store(Arc::new(MemoryRunStore::default()))
        .session_store(Arc::new(MemorySessionStore::default()))
        .namespace("docs")
        .pricing(|_model, usage| Some(usage.output_tokens as f64 * 1e-6))
        .build()
        .unwrap();
    let mut stream = agent.run("hello", RunOptions::new("s1"));
    while let Some(item) = stream.next().await {
        if let Event::Result(result) = item.unwrap().event {
            assert_eq!(result.final_text.as_deref(), Some("hello"));
            assert_eq!(result.total_cost_usd, Some(2e-6));
        }
    }
}
```

## Testing adapters

- `ScriptedProvider` (`crates/linch-core/src/provider/scripted.rs`) replays
  fixed turns and records each request; `TurnBuilder` writes text, thinking,
  tool-call and usage turns. Use it for runner, eval and tool tests.
- Recorded streams live in `crates/linch-providers/tests/fixtures/*.sse`; the
  stream tests replay them through each decoder.
- `crates/linch-providers/tests/live_*.rs` call the real APIs and skip when
  the key variable is unset.

---

Back to the [architecture index](./README.md).
