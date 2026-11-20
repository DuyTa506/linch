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
- Unknown usage is `None`, never a zero `Usage`; a zero means known to have
  cost nothing ([below](#retries-and-fallback-models)).
- Events are a closed enum that rejects unknown fields and tags on decode.

## The contract

`Provider` (`crates/linch-core/src/provider/client.rs:25`) is
`Send + Sync` and object safe; the agent holds it as `Arc<dyn Provider>`.

| Method | Contract |
|---|---|
| `fn id(&self) -> &str` | Stable identifier (`deepseek`, `openai-chat`, `vllm`, ...). The OpenTelemetry layer maps it to `gen_ai.provider.name`. |
| `async fn stream(&self, request, cancel) -> Result<ProviderStream, ProviderError>` | Start one call. An error before the first event (connect, HTTP status, a request the adapter refuses) is the `Err`. The stream ends after `message_end` or after one error item. Cancelling `cancel` ends it with `ProviderError::Cancelled`. |
| `fn context_window(&self, model: &str) -> u64` | The model's context window, without I/O; `DEFAULT_CONTEXT_WINDOW` (128000) unless overridden. Compaction without a `threshold_tokens` (`crates/linch-core/src/compaction.rs`, `due`), the skill listing budget and the facade's offload fraction derive from it. |
| `async fn prepare(&self)` | One-time discovery, a no-op unless overridden; it never fails. The facade calls it when it opens an agent, before anything reads the window; a host that builds an agent in code calls it before `AgentBuilder::build`. |
| `fn native_output_schema(&self) -> bool` | Whether the adapter sends `output_schema` in its own structured-output form; `true` unless overridden. When `false` the run never sends it one and offers a final tool made from the schema instead ([turn lifecycle](./turn-lifecycle.md#the-final-tool)). DeepSeek and the vLLM, SGLang and llama.cpp profiles override it. |
| `fn tool_choice_refusal(&self, choice, thinking) -> Option<String>` | Why the adapter cannot send `choice` with `thinking`, without I/O; `None` (takes every choice) unless overridden. The agent refuses a `spec.tool_choice` it names at build, and a run's own one before anything is written; the run asks it before forcing its final tool. Each adapter's override is the function its payload builder calls, so the open-time and call-time rules cannot drift. |

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
`gemini_parts` (`crates/linch-providers/src/gemini.rs:51`).

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
- every opened call ends before `message_end`;
- `stop_reason` is `tool_use` if and only if at least one call completed.

A call whose concatenated input is not a JSON object (malformed, cut off,
an array) does not fail the stream: the block keeps an empty `input` and the
model's text in `ToolUseBlock.raw_input`, the scheduler plans it as an error
(`invalid JSON arguments for <tool>: <parse error>; received: <first 200
characters>`, before hooks and permission) and never runs it, and the model
reads that error and calls again. The provider request builders replay only
`input`, so a provider sees `{}` for it; the raw text stays in the log. The
one exception is a `max_tokens` stop: a call still open, or with input that is
not a whole object, is dropped instead. The turn
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
| `max_output_tokens`, `temperature`, `stop_sequences` | Optional. `temperature` is `spec.temperature`; every adapter sends it when set, `0` included, and `Provider::max_temperature` (2, Anthropic 1) bounds it when the agent is built. |
| `effort` (`ReasoningEffort`), `thinking` (`Thinking`) | `None` keeps the provider default. Each adapter maps them itself. |
| `cache_control` | Set (no TTL) on every primary call of a run, as Python's `cache_prompt=True`; a compaction summary call has none (`crates/linch-core/src/agent.rs`). |
| `output_schema`, `tool_choice` | Optional. |
| `appended_tools` | The names of the `tools` a session added after its frozen prefix; empty outside a session's catalog. Only the native catalog changes of the Anthropic and Responses adapters read it. |
| `context_tail` | How many trailing blocks of the last message are request-only context (recalled memory, retrieval, `context_build` blocks), never logged; 0 and off the wire when none. Anthropic puts its conversation breakpoint on the last block before them; adapters with automatic prefix caching ignore it, so the context stays at the tail. |

There is no cancel signal, retry budget or native-catalog switch in the
request; the cancel token is the second argument of `stream`.

## Errors and retries

`ProviderError` (`client.rs:58`) has seven variants. Messages are written by
the adapter and must never hold credentials or request headers.

| Variant | Meaning | `retryable()` |
|---|---|---|
| `Connect { message }` | The connection was never made (refused, DNS, TLS handshake, connect timeout), so the request never went out. Each adapter's `transport` maps reqwest's `is_connect()` to it (`anthropic/http.rs:69`, `openai_compat/http.rs:66`, `gemini/http.rs:63`) | yes |
| `Transport { message }` | The request went out but no response came back (reset, read timeout), or the stream broke after it began | yes |
| `Status { status, retry_after, message }` | Non-success HTTP status | 408, 429 and >= 500 only |
| `ContextLength { status, message }` | The provider refused the prompt as longer than the model's context window. `status` is `None` for an error inside a stream. Each adapter maps its own provider's refusal ([below](#context-length-refusals)) | no |
| `InvalidRequest { message }` | The adapter refused the request as built | no |
| `Stream(StreamError)` | The event stream broke the stream contract | no |
| `Cancelled` | The cancel token fired | no |

Adapters never retry (`crates/linch-providers/src/lib.rs:4`); `retryable()`
and `retry_after` are advice for the caller (`client.rs:1-5`). The runner is
that caller. Its `dispatch` (`crates/linch-core/src/runner/turn.rs:295`)
ends the run `failed` with an `Error` event on any failure that it does not
retry or fail over (below), and aborts the run on `Cancelled`. The error
event carries `kind`, `retryable` and `status`
(`crates/linch-core/src/provider/call.rs:325`).

### Retries and fallback models

Two spec fields turn a retryable failure into another call instead of the
end of the run (`crates/linch-core/src/reliability.rs`, applied in
`crates/linch-core/src/runner/retry.rs`). Both are part of the run contract
and inherited by a subagent's child runs.

| Field | Default | Effect |
|---|---|---|
| `provider_retry` | on | `ProviderRetry` (`reliability.rs:46`): `max_attempts` 5 (calls to one model in one turn, the first included), `base_delay_ms` 1000, `max_delay_ms` 30000, `jitter` 0.2, Python's `RetryOptions`. `max_attempts: 1` turns it off |
| `fallback_models` | `[]` | The models to fail over to, in order. Not blank, not the spec's `model`, no repeats (`reliability.rs:193`) |

`AgentSpec::provider_retry` is a `ProviderRetry`, not an `Option`: serde
fills a missing field with the default and leaves the default off the wire
(`ProviderRetry::is_default`, `reliability.rs:80`). So a spec that does not
set it, `{}`, and the defaults spelled out are one spec, with the wire form,
and the contract digest, of a spec written before retries existed. A run
started before retries existed resumes under the same contract and retries
from then on; it has no `provider_retry` event, so its turn's count starts
at zero. `{"max_attempts": 1}` is a different contract. `null` is a decode
error: off is spelled `{"max_attempts": 1}`.

After a failed call, `Runner::recover` (`retry.rs:45`) decides, in order:

1. A failure that is not `retryable()`, or a budget that refuses the next
   call, ends the run as before.
2. **Same-model retry.** `ProviderRetry::delay` (`reliability.rs:112`)
   gives the wait before retry `n`: `base_delay_ms * 2^(n-1)`, capped at
   `max_delay_ms`, moved by up to `jitter` of itself (drawn from the failed
   delivery's id, so runs that failed together spread out), never past the
   cap. A `Retry-After` replaces it, unjittered; one longer than
   `max_delay_ms` gives no wait, so the run does not hold its lease for a
   server-chosen time. The model gets no more calls once it had
   `max_attempts` in the turn. Each turn starts with its full budget.
3. **Failover.** With no retry left, the run takes the next of
   `fallback_models` and calls it at once, with a fresh retry budget. It
   keeps that model for the rest of the run, compaction summary calls
   included (`retry.rs:98`). A turn that failed over calls the fallback
   even where a loaded skill names a model; a later skill turn gets the
   skill's model again, as in Python (`retry.rs:105`).
4. With neither, the last failure ends the run.

Every attempt is a dispatch of its own: `Step::Dispatch` with the next
`attempt` builds the request again, runs `context_build` and
`before_provider_call` again, stages a new delivery and flushes it with
`ProviderPending` before the call. So the delivery `attempt` grows with each
retry or failover, as it does when a resumed run redispatches a call that
never settled (`Recovery::Redispatch`, `runner/start.rs:227`). Each attempt
settles one receipt.

With an `after_provider_call` hook attached, an accepted receipt is durable
before any hook runs on its call. A resume after a crash in a hook finds the accepted receipt without its response (an orphan), keeps
its usage and cost in the totals and the budget, and dispatches the call
again under the next attempt; that is not a retry, so no `provider_retry`
event is written and the turn's retries are untouched
([turn lifecycle](./turn-lifecycle.md#a-crash-between-a-receipt-and-its-response)).

A failed receipt's usage is a known zero when the call cost nothing, and
unknown (`None`) when it may have been billed (`costs_nothing`,
`provider/call.rs:257`, applied at `call.rs:169`):

| Failure | Receipt usage | `CallFailure::free` |
|---|---|---|
| `Connect`: the request never went out | `Some(zero)` | true |
| `Status` 4xx (408 and 429 included), 503 or 529 before any stream event: the provider refused it | `Some(zero)` | true |
| `Status` 500, 502 or 504 before any stream event: a gateway may send it after the upstream model ran and billed | `None` | false |
| Any other `Status` before any stream event (501, 505, 520-524, ...) | `None` | false |
| `Transport`: sent, nothing back | `None`, unless a `message_end` reported usage | false |
| Anything after the first stream event, a `Status` included | `None`, unless a `message_end` reported usage | false |

`Option<Usage>` already tells a zero from unknown, so the receipt format does
not change, and a binary from before this reads `Some(zero)` as a call that
cost nothing. A free failure sends no `usage` event (`runner/turn.rs:405`,
and the same in `runner/compact.rs`), as Python sends none for a failed call,
and leaves `usage_complete` true. A capped `budget` only refuses the next
call after an unknown usage (`budget.rs:118`), so a free failure is retried
under a cap; one that may have been billed still ends the run with the
provider's error. A 500, 502 or 504 is therefore not retried under a cap,
though it is retryable and is retried without one. Python marks every failed call's usage `unknown`
(`src/linch/loop/streaming.py:412-415`) and then refuses any call under a cap
(`src/linch/model_calls.py:643-650`, checked before every delivery, a retry included, at `src/linch/loop/streaming.py:467`), so under a cap it never retries.

The two events, both durable:

| Event | When | Written |
|---|---|---|
| `provider_retry` (`model`, `retry`, `delay_ms`, `error`) | Before the wait | In its own write with the failed receipt, its `Usage` (if it may have cost something), and `ProviderPending` re-saved at the totals that count them (`retry.rs:69`), so the caller sees it before the wait |
| `model_fallback` (`from_model`, `to_model`, `error`) | Before the fallback's call | With the failed receipt, on the next dispatch's barrier |

`error` is the failure in the `error` event's shape. Python's
`model_fallback` has a `reason` text in its place, and Python tries its
fallback models before its same-model retries; Rust spends a model's retries
first.

Resume reads where a run stands from these events (`Standing::of`,
`reliability.rs:161`, called in `runner/start.rs:289`): the last
`model_fallback` names the model, and the `provider_retry` events since the
last `assistant` (or the last failover) are the turn's spent retries. Nothing
is added to the checkpoint. A crash during a wait resumes with the next
attempt at once, without waiting the rest; its failed receipt is durable, so
it is neither lost nor settled twice. A crash before a failover's barrier
leaves the old model with its retries spent, so the resumed run fails over
again; a crash after it resumes on the fallback. A cancel during a wait
aborts the run at once (`retry.rs:73`).

With no failure, a run with retries on and fallback models set writes the
same events and checkpoints, and sends the same requests, as one with
retries off and no fallbacks (pinned by
`reliability_costs_nothing_when_no_call_fails`,
`crates/linch-core/tests/e2e_provider_retry.rs`).

A `ContextLength` failure is not retryable, and before any event with a 4xx,
503 or 529 status it cost nothing, like a `Status` refusal. With
`spec.compaction` set, the run shortens its view and calls again instead of
failing ([context-length recovery](./compaction.md#context-length-recovery));
that call is the turn's next attempt, not a retry.

### The concurrency gate

`max_provider_concurrency` builds a `ProviderGate`
(`crates/linch-core/src/provider/gate.rs`): a FIFO semaphore whose clones
share one budget, bound with `AgentBuilder::provider_gate` and cloned into
a subagent's agent. It is not in the spec, so it is not in the contract
digest, and unset it is not in the config's JSON either (pinned by
`the_cap_is_off_the_wire_when_unset_and_out_of_the_contract`).

The runner takes a place in `dispatch` after `before_provider_call` and
before it stages the call's delivery, and in compaction before the summary's
delivery (`runner/gate.rs`). A run that waits has staged no delivery, so a
cancel there is the `abort` of a run that never called; the wait is
`cancel.run_until_cancelled`, and a cancelled waiter leaves the queue
(pinned by `cancelling_a_run_that_waits_leaves_no_call_and_no_receipt`).
With a place free the path writes nothing. With none, whatever the run has
staged (a failed receipt and its usage, a `model_fallback`, a child's
events) is flushed before the wait, since a paid receipt must not sit in
memory behind another run's call. The place is held through the delivery
write, `provider_call_start` and `drive`, and dropped before the receipt is
handled, so the backoff, `overflow` and the hooks never hold it.

`PgPools` (the facade's per-scope shared state: the server keeps one per
tenant and one for local connections) holds `Weak` gates by provider key
variable and endpoint, not by size, so two agents' limits do not add up. A
gate has the size of the open that created it, as a pool does, and goes with
the last agent that holds it. Nothing shares a gate between processes; a
Postgres-backed limiter is not built, and Python Linch's pluggable `Limiter`
protocol (a host-supplied `acquire` and `release`) has no counterpart.

### Context-length refusals

`crates/linch-providers/src/overflow.rs` holds one `Overflow` per provider
family: the statuses it may come with, the error codes or types that name it,
and the message phrases, matched in lower case. `Overflow::classify` turns a
matching error into `ProviderError::ContextLength` and leaves any other as it
was. Each adapter applies its own on an HTTP error body and on an error that
arrives inside the stream:

| Adapter | Status | Code or type | Message phrases |
|---|---|---|---|
| Anthropic (`anthropic/http.rs`) | 400 | none | `prompt is too long`, `exceed context limit` |
| OpenAI Chat (`openai_compat/http.rs`, `openai/profile.rs`) | 400 | `context_length_exceeded` | `maximum context length` |
| OpenAI Responses (`responses/stream.rs`) | 400, or `response.failed` | `context_length_exceeded` | `exceeds the context window`, `maximum context length` |
| Gemini (`gemini/http.rs`) | 400 | none | `exceeds the maximum number of tokens allowed` |
| DeepSeek (`deepseek.rs`) | 400 | none | `maximum context length` |
| vLLM (server profile) | 400 | none | `maximum context length` |
| SGLang (server profile) | 400 | none | `maximum context length`, `exceeds the maximum allowed length`, `is longer than the model's context length` |
| llama.cpp (server profile) | 400 | `exceed_context_size_error` | `exceeds the available context size`, `larger than the max context size` |

Anthropic's 413 means the body is too large in bytes, which compaction may
not fix, so it stays a `Status`. Sources: OpenAI's code is in its SDKs and
the Responses `response.failed` code in Codex's fixtures; vLLM, SGLang and
llama.cpp's texts are in their source; the Anthropic, Gemini and DeepSeek
phrases are not documented by the vendor and come from the errors those
APIs return. A server that rewords its message falls back to a plain
`Status`, the behaviour before this mapping. The SSE loop scrubs a
`ContextLength` message like any error message.

There is no deadline on a whole provider call. Each adapter's HTTP client has
a 30 s connect timeout and a read timeout between reads (below); cancel the
run's token to stop a call sooner.

## Shared HTTP mechanics

Every adapter builds its client with `transport::client`
(`crates/linch-providers/src/transport.rs:24`):

- **No redirects.** reqwest strips only `Authorization` on a cross-origin
  hop, so `x-api-key` and `x-goog-api-key` would follow a redirect. A 3xx
  answer becomes `Status` with a fixed message (`transport.rs:64`); it is
  not retryable.
- **Bounded error bodies.** At most 8 KiB of a non-2xx body is read
  (`transport.rs:17`, `transport.rs:41`). The summary prefers the vendor's
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
(`crates/linch-providers/src/sse.rs:202`). An adapter supplies a crate-private
`Decoder` (`sse.rs:32`): `data` translates one non-blank `data:` payload and
says whether the response is complete; `close` handles a body that ended
early (a format with an end marker fails, one without finishes). The loop:

- frames the body itself (`Framer`, `sse.rs:88`), in one pass over a buffer
  with a cursor, so the cost is linear in the bytes whatever the chunk size.
  It reads LF, CRLF and CR line ends, drops a leading byte order mark, joins
  the `data` lines of an event, and drops comments and the `event`, `id` and
  `retry` fields, which no decoder reads. The body end delivers a last event
  with no trailing blank line;
- bounds one event: the bytes read since the last event end, an unterminated
  line included, may not pass `MAX_EVENT_BYTES` (16 MiB, `sse.rs:25`; the
  largest real event is the Responses terminal event, which repeats the whole
  output, about 2 MiB for a 128 000-token answer), and a byte that is not
  UTF-8 fails the call when it is read. Both are `Transport` errors;
- checks the cancel token before each read and races the read against it;
- once the response is complete, yields what is queued even if a cancel
  arrives, so the end and its usage are not lost (`sse.rs:242`);
- yields nothing after an error (`sse.rs:327`);
- scrubs the API key and every `Bearer <token>` from transport and status
  messages, then cuts them to 500 characters (`MAX_ERROR_CHARS`), like an
  HTTP error body (`sse.rs:301`).

### Credentials

`ApiKey` (`crates/linch-providers/src/api_key.rs:14`) wraps the key in an
`Arc<str>`. `Debug` prints `ApiKey([redacted])`, there is no `Display`, and
an empty or all-whitespace key is `None`. Every string that reaches a
`ProviderError` passes through `scrub` (`api_key.rs:46`), which removes the
key and any `Bearer` token, even for a keyless local server (a proxy may add
one). Each options struct has `from_env()`, which reads the adapter's
variable and returns `None` when it is unset or empty.

### Request options

`RequestOptions` (`request.rs`) carries `extra_body`, a read `timeout` and
default headers for every adapter; `ChatOptions` carries the Chat Completions
knobs (`parallel_tool_calls`, `include_stream_options`, `chat_template_kwargs`,
`sampling_params`). Default headers go on the `reqwest` client, which never
replaces a header set per request, so a caller's key header (`authorization`,
`x-api-key`, `x-goog-api-key`, `api-key`) makes the adapter skip its own key
header (`sets_key_header`, `key_to_send`) instead of overriding it, for a turn, a
model list and a window probe alike. `merge_body` runs last on the built payload
and skips the fields in `PROTECTED_FIELDS` (the model, `stream`, `temperature`,
the conversation, the tools and `tool_choice` in each wire's spelling, and the
state fields `store`, `previous_response_id`, `conversation`); Gemini's
`generationConfig` is merged field by field with the request's own winning.
Header values are marked sensitive, never appear in `Debug`, and `scrub_error`
and `scrub_stream` remove those of 8 bytes or more from every error text an
adapter returns. The settings are not part of the run contract.

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
state turns it on (`deepseek.rs:137`). With thinking on, every earlier
assistant turn sends `reasoning_content`, `""` when it has none, or a request
with tools fails with 400 (`deepseek.rs:155-161`). A forced `tool_choice`
with thinking on, and any `output_schema`, fail locally as `InvalidRequest`:
DeepSeek has no JSON-schema response format and `json_object` mode does not
carry the schema. `native_output_schema` is `false` (the capabilities say
`output_schema: final_tool`), so a run with a schema offers its final tool
instead, and `tool_choice_refusal` (the same rule as
the payload's) refuses a forced choice unless thinking is `Disabled`.
Effort maps `medium` and `xhigh` to `high` (`deepseek.rs:197`).

### OpenAI Chat Completions and server profiles

One type, `OpenAiChat` (`crates/linch-providers/src/openai.rs:102`), serves
OpenAI and each OpenAI-compatible server. The difference is a `ChatProfile`
(`crates/linch-providers/src/openai/profile.rs:19`), a static table with no
defaults: every target states every rule, so a server never inherits OpenAI's
behavior by accident.

| Profile | Reasoning field | Echo reasoning | Named `tool_choice` | Output limit field | Thinking off |
|---|---|---|---|---|---|
| OpenAI (`profile.rs:53`) | none | no | yes | `max_completion_tokens` | not sent |
| vLLM (`servers/vllm.rs:33`) | `delta.reasoning` | yes | yes | `max_completion_tokens` | `enable_thinking: false` |
| SGLang (`servers/sglang.rs:31`) | `delta.reasoning_content` | yes | yes | `max_completion_tokens` | `enable_thinking: false`, `thinking: false` |
| llama.cpp (`servers/llamacpp.rs:31`) | `delta.reasoning_content` | yes | no (fails locally) | `max_tokens` | `enable_thinking: false` |

A server is chosen with `ServerOptions::new(Server::Vllm, base_url)`
(`crates/linch-providers/src/servers.rs:80`), which fills the
`chat_template_kwargs` that server's docs give for turning thinking off. When
a request asks for `Thinking::Disabled`, those kwargs are sent and effort is
left out (`profile.rs:144-154`). The profile's `reasoning_effort` writes the
effort: OpenAI, SGLang and llama.cpp send `low` to `max` as they are; vLLM
sends `high` and `max` as `xhigh`, because the Qwen3 chat template behind it
takes only `low`, `medium` and `xhigh` and answers HTTP 400 otherwise
(`servers/vllm.rs`). An `output_schema` is sent as
`response_format: {"type": "json_schema", "json_schema": {name, strict,
schema, description?}}` when the profile's `json_schema` rule is on, which only
OpenAI's is; vLLM, SGLang and llama.cpp refuse it locally as `InvalidRequest`
and report `native_output_schema` `false` (`output_schema: final_tool`), so a
run offers its final tool there. The reason is checked for vLLM only: with a
`json_schema` `response_format` and tools in one request its guided decoding
binds the whole answer to the schema and the model never calls a tool (a
request that asked for a tool call still came back as schema JSON with zero
tool calls, against a live server on 2026-11-17). So `final_tool` does not say
vLLM cannot do structured output; it says a run that also has tools must not
send the schema. SGLang and llama.cpp keep the same rule unchecked.
`tool_choice_refusal` is the profile's `tool_choice` row, the rule
`build_payload` checks.

### OpenAI Responses

`crates/linch-providers/src/responses.rs`. System blocks become
`instructions`. Function tools are sent with `strict: false`, so the API does
not rewrite a schema into strict mode. Each response's output items,
reasoning included, are kept in `provider_metadata` and sent back verbatim;
no `previous_response_id` is sent, so every request carries its full
history. `ResponsesOptions::store` off sends `store: false` and asks for
encrypted reasoning. Stop sequences are rejected: the API has no such
parameter. An `output_schema` is sent as `text.format: {"type":
"json_schema", name, strict, schema, description?}` (`responses/payload.rs`).

### Anthropic

`crates/linch-providers/src/anthropic.rs`. `anthropic-version` is pinned to
`2023-06-01` (`anthropic.rs:47`) because the newer version removes
`temperature`, which `ProviderRequest` still carries. `max_tokens` is
required and defaults to 8192 (`anthropic.rs:50`). Thinking blocks are sent
back with their signatures unchanged. A request's `cache_control` marks
the last cacheable system block and the last logged block of the last user
message: request-only context (`context_tail`) stays after it, so the
breakpoint is where the next turn's cached message also ends
(`anthropic/payload.rs`).
An `output_schema` is sent as `output_config.format: {"type": "json_schema",
"schema": ...}`, beside `output_config.effort` (`anthropic/payload.rs`). It
needs no beta header and works with tools, thinking and streaming; it shapes
only the text, so the model keeps calling tools. No forced final tool is used:
current Claude models refuse a forced `tool_choice` with thinking on.
`message_delta.usage` is cumulative: a present field replaces the
`message_start` value. `pause_turn` and unknown stop reasons fail the call as
a retryable transport error (`anthropic/stream.rs:311`).

### Gemini

`crates/linch-providers/src/gemini.rs`. The model is percent-encoded as one
path segment, so `/`, `?`, `#` and `:` in a model name cannot change the URL
(`gemini.rs:59`). Function calls without an `id` get a random one per
response that is never sent back. A turn with a thought signature or a call
id keeps its raw parts in `provider_metadata` and replays them verbatim.
`cache_control` is ignored (caching is implicit). Safety-type finish reasons
become `refusal`; unknown ones fail as a retryable transport error
(`gemini/stream.rs:293`). An `output_schema` is sent as
`generationConfig.responseMimeType: "application/json"` with
`responseJsonSchema` (`gemini/payload.rs`); Gemini combines it with function
calling from Gemini 3 on. The module notes this adapter has not yet run
against a live key.

### Context windows

Each adapter reports Python Linch's values (`crates/linch-providers/src/context.rs`
holds the lookups and probes): Anthropic 200000; Gemini 2000000 for
`gemini-1.5-pro`, else 1048576; Responses and OpenAI Chat their own tables
(Python keeps two), else 128000; DeepSeek 128000. `DeepSeekOptions`,
`OpenAiChatOptions` and `ServerOptions` take a `context_window` that wins.
A server profile states its rule (`ChatProfile::context`): vLLM and SGLang read
each model's `max_model_len` from `GET /v1/models`, llama.cpp `n_ctx` from
`GET /props` (then the root's), once in `prepare`, within 2 seconds, with the
call's bearer key. A failed probe leaves 128000. Unlike Python, a configured
llama.cpp window skips the probe rather than serving as its fallback.

### Usage normalization

Linch's `Usage` (`crates/linch-core/src/usage.rs:13`) counts uncached input,
output, cache reads and cache writes in four separate buckets; `input_tokens`
excludes both cache buckets. Each adapter subtracts what its vendor folds
into the prompt count: DeepSeek its cache hits (`deepseek.rs:217`), OpenAI
cached and cache-write tokens (`profile.rs:89`), vLLM cached and created
cache tokens (`servers/vllm.rs:54`), SGLang and llama.cpp cached tokens
(`servers.rs:95`).

## Pricing

`linch_providers::cost(model, &usage)` (`crates/linch-providers/src/pricing.rs:97`)
prices each bucket at its own rate from a table matched on the exact model id
(`pricing.rs:56`). The table holds Anthropic models only, at the published
rates of 2026-06-08 with cache writes at the 5-minute tier. Every other
model, including every OpenAI, DeepSeek, Gemini and local model, prices to
`None`. `ModelPricing::cost` (`pricing.rs:33`) prices with rates a host
supplies.

Pricing is opt-in on the agent: `AgentBuilder::pricing`
(`crates/linch-core/src/agent/builder.rs:124`) takes any
`Fn(&str, &Usage) -> Option<f64>`. The `linch` crate passes
`linch_providers::cost` (`crates/linch/src/open.rs:170`). When a receipt has
usage, the runner prices it by the model that served the call, else the
agent's model (`crates/linch-core/src/runner/write.rs:107`). `AgentBuilder::build` refuses a
`budget.max_cost_usd` when pricing has no finite price for the spec's model
or one of its `fallback_models` (`BuildError::Invalid`, which `agent.open` reports as `-32011`), and a run
with one ends in `BudgetAccountingError` on a call that has usage and no
price. A missing price
or a non-finite one stays `None` (`crates/linch-core/src/agent.rs:141`), and a
run's total adds only priced calls. Costs appear as `cost_usd` on the
receipt and `Usage` events and `total_cost_usd` on `Result`. Pricing is not
part of the run contract.

Python's table is the only source: it prices six Claude ids and nothing
else, so no OpenAI, Gemini, DeepSeek or local-server model has a price
unless the agent gives one. `AgentConfig.pricing`
(`crates/linch/src/config/pricing.rs`) holds per-model `ModelRates` that
`open` puts in front of the table: `crate::config::pricing` returns the
override of a model, else `linch_providers::cost`. It is per agent and not
part of the contract. Local servers cost `None` without an override, as in
Python.

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

## Capabilities and model lists

`Provider::capabilities(model)` (`crates/linch-core/src/provider/client.rs`)
answers what the adapter supports as plain data,
`ProviderCapabilities` (`provider/capabilities.rs`). It does no I/O: a window
a server reports is read once by `prepare`. Every adapter overrides it and
states each field; the trait default only repeats what the trait already
says (`declared: false`, vision `unknown`, nothing else supported), so no
adapter takes another's rules. The server profiles state `vision`,
`streams_tool_arguments`, `prompt_cache`, `stop_sequences` and the shape of
their model list as required `ChatProfile` fields, so the compiler forces
each one.

| Field | Meaning |
|---|---|
| `context_window`, `context_source` | The window a run would use, and whether it is `configured`, from the `server`, the adapter's `table` or a `default` guess |
| `output_schema` | `native` (`Provider::native_output_schema` is true: the schema goes in the request in the provider's own form) or `final_tool` (it is false: the request carries no schema and the run offers a final tool made from it). Not "can the server do structured output": vLLM takes a `response_format` and is still `final_tool`, since with tools in the same request it never calls them |
| `tool_choice` | `auto`, `none`, `required`, `named`, and `forced_refused_with`: the thinking states (`unset`, `adaptive`, `budgeted`) in which a forced choice is refused. Read by probing `tool_choice_refusal`, so it is the agent's rule |
| `thinking` | `disable` (`thinking: disabled` is sent), `budget`, `effort`, `reasoning_text` (the stream carries reasoning text; the Responses adapter has it only with a `reasoning_summary`) |
| `vision` | `supported` (Anthropic, Gemini), `model_dependent` (the adapter sends images, the served model decides) |
| `streams_tool_arguments` | False only for Gemini, whose call arrives whole |
| `prompt_cache` | `explicit` (Anthropic's `cache_control`) or `automatic` (the server caches a shared prefix; no control is sent) |
| `stop_sequences` | False for the Responses API, which refuses them |

`crates/linch-providers/src/capabilities_tests.rs` checks every declared rule
against the adapter's behaviour: each tool choice with each thinking state
against both the refusal and whether the payload builds, the output schema
and stop sequences against the payload, and thinking, effort, budget and
`cache_control` against whether they change the request body.

`list_models()` and `model_info(model)` ask the vendor's API
(`crates/linch-providers/src/models.rs`): OpenAI, the Responses adapter and
DeepSeek `GET {base}/models`; vLLM and SGLang the same with `max_model_len`;
llama.cpp the same with the window from `/props`; Anthropic `GET /v1/models`
(limit 1000, paged by `after_id`) with `max_input_tokens`, `max_tokens` and
`image_input`; Gemini `GET /v1beta/models` (page size 1000, `pageToken`),
leaving out models without `generateContent`. `list_models` returns a
`ModelList`: `models`, and `truncated`, true (and left off the wire when
false) when the vendor had more than the 10 pages that are read, so the list
is only the first of them. One request takes at most 15 seconds and 4 MiB
(an error answer is read to its first 8 KiB), a redirect is refused, and the
model id is never put in a URL (`model_info` reads the list; a miss on a
truncated list is `NotFound { truncated: true }`, inconclusive). A
`display_name` is cleaned before it is returned: control and
direction-changing characters are dropped and it is cut at 100 characters. A
failure is a `ModelsError`: `Unsupported` (the scripted provider), `NotFound`,
or a `ProviderError` whose message has fixed text for a failure to connect or
a timeout and, for an error answer, only `error.message`, scrubbed of the key,
reduced to ASCII printable characters and cut at 120, so neither the key nor
the URL can appear.

### The `base_url` rule

An adapter builds each URL by appending to the base as written
(`{base}/models`, `{base}/v1/models?limit=1000`), and sends its key there. So
every adapter refuses, when it is built (`check_base_url`,
`crates/linch-providers/src/base_url.rs`; `ProviderError::InvalidRequest`), a
base that is not an absolute `http` or `https` URL with a host, or that has
userinfo, a query or a fragment (`https://host/v1/files#` would make the
request a `GET /v1/files`), a percent escape, a backslash, whitespace or a
control character, or a path segment that is `.`, `..` or empty before the
last. The check reads the text as written, because the URL parser resolves dot
segments before anyone sees them. The message names no part of the URL.

A host that takes provider configs from tenants adds the path rule
(`linch-rpc`'s `provider_base_urls`, which `agent.open` and the provider
questions share): the path must be the provider's own
(`ProviderConfig::default_base_paths`: `/v1` for OpenAI Chat and Responses,
none for DeepSeek, Anthropic and Gemini, `/v1` or none for vLLM, SGLang and
llama.cpp), when the entry names an origin only; or, when the entry has a
path, exactly that path. A local caller keeps any plain path, such as a
gateway's prefix.

### Keyless questions

`ProviderConfig::build_keyless` never reads the key from the environment: the
provider holds a placeholder (a self-hosted server's optional key is left
unset), and `provider.capabilities`, which uses it, sends at most the one
unauthenticated window probe. A probe answer is read to at most 64 KiB.

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
