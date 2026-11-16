# Providers

Use this page to choose a model provider, give the server its key, and test
offline with the scripted provider.

[<- Usage guide](./README.md)

The `provider` field of an [agent config](./agent.md) picks the provider by
`id`. The model itself is `spec.model`, sent with every request. The adapters
run inside the server; how each one maps requests, thinking and usage is on
the [architecture providers page](../architecture/providers.md).

---

## Choose a provider

| `id` | Key variable | Default endpoint | Extra fields |
|---|---|---|---|
| `deepseek` | `DEEPSEEK_API_KEY` | `https://api.deepseek.com` | `base_url`, `context_window` |
| `openai-chat` | `OPENAI_API_KEY` | `https://api.openai.com/v1` | `base_url`, `context_window` |
| `openai-responses` | `OPENAI_API_KEY` | `https://api.openai.com/v1` | `base_url`, `reasoning_summary`, `store`, `native_catalog_changes` |
| `anthropic` | `ANTHROPIC_API_KEY` | `ANTHROPIC_BASE_URL`, else `https://api.anthropic.com` | `base_url`, `native_catalog_changes` |
| `gemini` | `GEMINI_API_KEY` | `https://generativelanguage.googleapis.com` | `base_url` |
| `vllm` | `VLLM_API_KEY`, optional | none | `base_url`, required; `context_window` |
| `sglang` | `SGLANG_API_KEY`, optional | none | `base_url`, required; `context_window` |
| `llamacpp` | `LLAMA_API_KEY`, optional | none | `base_url`, required; `context_window` |
| `scripted` | none | none | `turns`, required; `context_window` |

`base_url` replaces the provider's endpoint. Any other field is refused at open
with a `ProtocolError` (code -32602), so a config can never carry an
`api_key`.

### Request settings

Every live provider takes `request`, the data form of Python's `extra_body`,
`default_headers` and `timeout`:

```json
{"id": "vllm", "base_url": "http://gw.internal/v1",
 "request": {"extra_body": {"top_k": 40, "cache_salt": "t1"},
             "default_headers": {"x-gateway-token": "..."},
             "timeout_ms": 60000},
 "chat": {"parallel_tool_calls": false, "include_stream_options": false,
          "chat_template_kwargs": {"preserve_thinking": true},
          "sampling_params": {"min_p": 0.1}}}
```

| Field | Meaning |
|---|---|
| `request.extra_body` | Fields merged into every request body. They replace what the adapter set, except `model`, `stream`, `temperature` and the conversation (`messages`, `tools`, `input`, `contents`, `system`, `instructions`), which the session owns. `chat_template_kwargs` merges per field |
| `request.default_headers` | Headers sent with every request, for gateways. `authorization`, `x-api-key` or `x-goog-api-key` replaces the provider key: the key from the environment is then not sent and need not be set. `host`, `content-length`, `transfer-encoding`, `connection`, `upgrade`, `te` and `trailer` are refused at open. Values are hidden when a config is printed or logged |
| `request.timeout_ms` | Longest silence between two chunks of a response before the call fails; absent: the adapter's own limit |
| `chat.parallel_tool_calls` | Sent when the request has tools; absent: the server's default |
| `chat.include_stream_options` | `false` drops `stream_options`, for servers that reject it. On when absent |
| `chat.chat_template_kwargs` | Fields for servers that apply a chat template; with thinking off, the server's thinking switch is merged over them |
| `chat.sampling_params` | Native sampling fields (`top_k`, `min_p`, ...), under `extra_body` |

`chat` applies to `deepseek`, `openai-chat`, `vllm`, `sglang` and `llamacpp`.
On a remote server, `request` is admitted with the `base_url` that carries it;
it cannot widen `provider_base_urls`.

Where Python Linch has the same knob (`deepseek`, `openai-chat`, `vllm`,
`sglang`, `llamacpp`, through the OpenAI SDK's own `extra_body`), two things
differ here. Python's `extra_body` replaces any field, `model` and the
conversation included, because the SDK merges it in last with nothing
protected; `request.extra_body` instead keeps the fields the table above
excludes. Python's `timeout` is one number applied to every phase of the call
(connect, read, write, the pool); `request.timeout_ms` only replaces the read
timeout above, and the 30 s connect timeout stays fixed.

A `default_headers` value is hidden only when a config is echoed or logged
(above); it is not scrubbed from a provider's own error text. A gateway that
quotes a rejected header back in its response body sends that value straight
into the failed run's `error` event.

### Context window

Each provider knows the context window of the spec's model, in tokens. It
sets when compaction runs without a `threshold_tokens`, the offload threshold
a `filesystem.offload_threshold_fraction` gives, and how long the skill
listing may be ([Agent config](./agent.md#compaction)). `context_window`
replaces it for every model. Absent, it is Python Linch's:

| `id` | Window |
|---|---|
| `anthropic` | 200000 |
| `gemini` | 2000000 for `gemini-1.5-pro`, else 1048576 |
| `openai-responses` | `gpt-5` 400000, `gpt-4.1` 1000000, `o1` 200000, else 128000 |
| `openai-chat` | `gpt-5.5` and `gpt-5.4` 400000, `o1` 200000, else 128000 |
| `deepseek`, `scripted` | 128000 |
| `vllm`, `sglang` | Asked when the agent opens: the model's `max_model_len` on `GET /v1/models` (a server with one model answers for any name), else 128000 |
| `llamacpp` | Asked when the agent opens: `n_ctx` on `GET /props`, else 128000 |

A server is asked once, for at most 2 seconds, with the same key as its calls;
one that does not answer leaves 128000 and the open goes on.

A live provider, for example Anthropic (needs `ANTHROPIC_API_KEY`; the open was
checked with a fake key, the call was not):

```python
import asyncio

from linch_rust import AgentConfig, ResultEvent, Server

config: AgentConfig = {
    "namespace": "demo",
    "spec": {"model": "claude-sonnet-4-6", "max_output_tokens": 1024},
    "provider": {"id": "anthropic"},
}


async def main() -> None:
    # The server reads ANTHROPIC_API_KEY from the environment it inherits.
    async with Server() as server:
        agent = await server.open_agent(config)
        run = await agent.run("s1", "Name one prime number.")
        result = await run.result()
        if isinstance(result, ResultEvent):
            print(result.final_text, result.total_cost_usd)


asyncio.run(main())
```

## Keys come from the server's environment

The server reads the provider's key when the agent opens. A missing or blank
key fails the open with `OpenError` (code -32013) whose `data` names the
variable:

```text
provider anthropic needs its key in the environment variable ANTHROPIC_API_KEY (code -32013)
```

`Server()` passes this process's environment to the server. `Server(env=...)`
passes exactly the mapping you give and nothing else, which keeps keys out of
your own process's environment:

```python
import asyncio
import os

from linch_rust import AgentConfig, Server


def load_key() -> str:
    # Stand-in for your secret store.
    return "sk-not-real"


config: AgentConfig = {
    "namespace": "demo",
    "spec": {"model": "deepseek-chat"},
    "provider": {"id": "deepseek"},
}


async def main() -> None:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "DEEPSEEK_API_KEY": load_key()}
    async with Server(env=env) as server:
        agent = await server.open_agent(config)
        print("opened", agent.handle)


asyncio.run(main())
```

Opening builds the client only; no request is made until a run calls the
model, so a wrong key shows up as an `error` event in the first run, not at
open.

The server keeps keys away from the processes it starts (`Bash`, stdio MCP
servers) and, on Linux, marks itself not dumpable so a child running as the
same user cannot read its `/proc/<pid>/environ`. Your process gets no such
protection: keys in its own environment stay readable to any process of the
same user, the server's children included. When agents run untrusted commands,
read keys from a secret store and hand them to the server in `env`.

## Hosted providers

### `deepseek`

DeepSeek's Chat Completions API. DeepSeek thinks by default; set
`spec.thinking` to `{"type": "disabled"}` to turn it off.

```python
{"id": "deepseek"}
```

### `openai-chat`

OpenAI Chat Completions. With `base_url` it also reaches a gateway that speaks
the same API with an OpenAI key:

```python
{"id": "openai-chat"}
{"id": "openai-chat", "base_url": "https://gateway.example.com/v1"}
```

A self-hosted vLLM, SGLang or llama.cpp server has its own `id` below; do not
point `openai-chat` at one, because each server differs from OpenAI in the
fields it reads.

### `openai-responses`

The OpenAI Responses API. Each request carries the full history; no
`previous_response_id` is sent.

| Field | Default | What it controls |
|---|---|---|
| `reasoning_summary` | none | Stream a reasoning summary as thinking: `auto`, `concise` or `detailed` |
| `store` | `True` | Whether OpenAI stores each response. `False` sends `store: false` and asks for encrypted reasoning instead |
| `native_catalog_changes` | `False` | Send a tool added or removed mid-session in the input as `additional_tools`, with an `allowed_tools` choice, so the prompt cache holds. Only with gpt-5.4 and later; not yet run against the live API |

```python
{"id": "openai-responses", "reasoning_summary": "concise", "store": False}
```

Use `spec.effort` (`low` to `max`) for reasoning depth; the OpenAI adapters
ignore `spec.thinking`. The servers (`vllm`, `sglang`, `llamacpp`) pass effort
to the model's chat template, which decides what it accepts. vLLM's Qwen3
template takes only `low`, `medium` and `xhigh`, so `high` and `max` are sent
as `xhigh`; SGLang takes every level (it only switches thinking on) and llama.cpp lets the
model's template refuse levels; both send the level as it is.

### `anthropic`

The Anthropic Messages API. The endpoint is `base_url`, else the server's
`ANTHROPIC_BASE_URL`, else the public one. `max_tokens` is required by the API
and defaults to 8192 when `spec.max_output_tokens` is absent.

| Field | Default | What it controls |
|---|---|---|
| `native_catalog_changes` | `False` | Send a tool added or removed mid-session in the conversation as a tool block (beta `inline-tools-2026-09-15`), so the prompt cache holds. Only on `api.anthropic.com` with the models that take it; not yet run against the live API |

```python
{"id": "anthropic"}
```

### `gemini`

Gemini `streamGenerateContent` with an API key (`GEMINI_API_KEY`). Vertex AI
credentials are not supported. The adapter has not yet been run against a
live key.

```python
{"id": "gemini"}
```

## Self-hosted servers

vLLM, SGLang and llama.cpp each have a profile of their own: which field
carries reasoning, which `tool_choice` forms work, which output limit field is
sent and how thinking is turned off. A profile copies nothing from OpenAI's.
The table of what each supports is in the
[architecture providers page](../architecture/providers.md#openai-chat-completions-and-server-profiles).

`base_url` is required and includes `/v1`. The key is optional: set it in the
server's environment only when the inference server was started with one.

```python
{"id": "vllm", "base_url": "http://127.0.0.1:8000/v1"}      # VLLM_API_KEY
{"id": "sglang", "base_url": "http://127.0.0.1:30000/v1"}   # SGLANG_API_KEY
{"id": "llamacpp", "base_url": "http://127.0.0.1:8080/v1"}  # LLAMA_API_KEY
```

`spec.model` is the name the inference server serves, for example
`Qwen/Qwen3-8B`. `{"type": "disabled"}` in `spec.thinking` sends that server's
switch for turning thinking off; every other value leaves the chat template's
default.

## The scripted provider

`scripted` replays a list of turns, one per provider call in call order,
including the calls a subagent makes. Use it in tests and before a key exists.
`linch_rust.scripted` builds turns:

| Name | What it is |
|---|---|
| `MODEL` | `"scripted-model"`, a model name for `spec.model` |
| `text_turn(text, model=MODEL)` | A turn that answers `text`; a run ends on it |
| `tool_turn(call_id, name, input, model=MODEL)` | A turn that calls tool `name` once with `input`, as call `call_id` |

```python
from linch_rust import AgentConfig
from linch_rust.scripted import MODEL, text_turn, tool_turn

config: AgentConfig = {
    "namespace": "test",
    "spec": {"model": MODEL},
    "provider": {
        "id": "scripted",
        "turns": [tool_turn("call_1", "lookup", {"email": "ada@example.com"}), text_turn("Found.")],
    },
}
```

Each turn is `{"events": [...]}`, the provider event stream of one response
(`message_start`, `text_delta`, `tool_use_start`, `tool_use_input_delta`,
`tool_use_end`, `message_end`, and `thinking_delta`); the event fields are in
the [architecture providers page](../architecture/providers.md#event-vocabulary).
The `model` of a turn's `message_start` is the model that served the call, so
`text_turn("Hi.", model="claude-sonnet-4-6")` exercises the cost fields.

A turn may also end with an `error`, to test how your host handles a failed
call: `connect` (`message`), `transport` (`message`), `status` (`status`,
`message`, `retry_after_ms?`), `context_length` (`status?`, `message`: the
prompt does not fit, see [Memory](./memory.md#when-the-prompt-does-not-fit))
or `invalid_request` (`message`). With no
`events`, the call fails before any output. Runs retry a retryable failure by
default, so this one turns retries off to see the failure end the run:

```python
import asyncio

from linch_rust import AgentConfig, ErrorEvent, ResultEvent, Server
from linch_rust.scripted import MODEL

config: AgentConfig = {
    "namespace": "test",
    "spec": {"model": MODEL, "provider_retry": {"max_attempts": 1}},
    "provider": {
        "id": "scripted",
        "turns": [{"error": {"type": "status", "status": 503, "message": "busy"}}],
    },
}


async def main() -> None:
    async with Server() as server:
        agent = await server.open_agent(config)
        async for envelope in await agent.run("s1", "Hi"):
            if isinstance(envelope.event, ErrorEvent | ResultEvent):
                print(envelope.event)


asyncio.run(main())
```

```text
ErrorEvent(error={'name': 'ProviderError', 'kind': 'status', 'message': 'provider returned HTTP 503: busy', 'retryable': True, 'status': 503})
ResultEvent(subtype='error', stop_reason='error', total_usage={...}, usage_complete=True, ...)
```

`usage_complete` is true: a call the provider refused with a status before
any output cost nothing ([below](#failures-and-retries)).

A run that needs more turns than the script holds ends with an `error` event
(`scripted provider ran out of turns after N`) and a `result` of subtype
`error`. The script is not part of the run contract, so a test can resume a
run on a new server whose script holds only the turns still to come.

## Every provider, opened offline

This opens one agent per provider with fake keys. It checks each config
without a network call:

```python
import asyncio

from linch_rust import AgentConfig, Server

# Fake keys: opening builds each client without a network call.
FAKE_ENV = {
    "DEEPSEEK_API_KEY": "sk-not-real",
    "OPENAI_API_KEY": "sk-not-real",
    "ANTHROPIC_API_KEY": "sk-not-real",
    "GEMINI_API_KEY": "not-real",
}

PROVIDERS = [
    ("deepseek-chat", {"id": "deepseek"}),
    ("gpt-5", {"id": "openai-chat"}),
    ("gpt-5", {"id": "openai-responses", "reasoning_summary": "concise", "store": False}),
    ("claude-sonnet-4-5", {"id": "anthropic"}),
    ("gemini-2.5-flash", {"id": "gemini"}),
    ("Qwen/Qwen3-8B", {"id": "vllm", "base_url": "http://127.0.0.1:8000/v1"}),
    ("Qwen/Qwen3-8B", {"id": "sglang", "base_url": "http://127.0.0.1:30000/v1"}),
    ("local", {"id": "llamacpp", "base_url": "http://127.0.0.1:8080/v1"}),
    ("gpt-5", {"id": "openai-chat", "base_url": "https://gateway.example.com/v1"}),
]


async def main() -> None:
    async with Server(env=FAKE_ENV) as server:
        for model, provider in PROVIDERS:
            config: AgentConfig = {"namespace": "demo", "spec": {"model": model}, "provider": provider}
            agent = await server.open_agent(config)
            print(provider["id"], agent.handle)


asyncio.run(main())
```

## Ask what a provider supports

Before opening an agent, ask the server what its provider supports and which
models it lists. They take the `provider` of an agent config, or the whole
`AgentConfig`:

```python
caps = await server.provider_capabilities(config)      # no key needed
models = await server.list_models(config["provider"])  # needs the key
info = await server.model_info(config["provider"], "qwen3.6-35b-a3b")
```

`ProviderCapabilities` reads as in
[Providers (architecture)](../architecture/providers.md#capabilities-and-model-lists):
`context_window` with its `context_source` (`server`, `table`, `configured` or
`default`, a guess), `output_schema`, `tool_choice` (with
`forced_refused_with`), `thinking`, `vision`, `streams_tool_arguments`,
`prompt_cache` and `stop_sequences`. A cloud provider's answer is the
adapter's, not the vendor's; the model list is the vendor's, and a field it
does not give (OpenAI gives no window) is `None`.

`output_schema` says how a run with an `output_schema` gets its structured
answer from this provider: `native` (the schema goes in the request, as for
OpenAI, Anthropic and Gemini) or `final_tool` (the request carries no schema
and the run offers a final tool whose input is the schema; DeepSeek, vLLM,
SGLang and llama.cpp). `final_tool` does not mean the server cannot do
structured output. vLLM takes a `response_format`, but with tools in the same
request its guided decoding binds the whole answer to the schema and the
model never calls a tool, so a run there uses the final tool. (SGLang and
llama.cpp keep the same rule, not checked against a live server.)

`list_models` returns a list that also has `.truncated`: true when the
provider had more pages than the server follows (10), so a model that is not
listed may still exist. `provider.capabilities` never sends the server's key,
even when one is set for that provider; `list_models` and `model_info` do.

Errors: `ModelsUnsupported` (-32035, the `scripted` provider),
`ModelNotFound` (-32036; `.truncated` is true when the list was cut, so it
proves nothing), `ProviderRequestFailed` (-32037: `.kind` is `unreachable`
(a refused connection, a reset and a timeout look alike), `status` (with
`.status`), `invalid_request` or `other`; the message is fixed text per
kind, never the key, the URL or the vendor's own words), and `OpenError`
(-32013) when the key is not set on the server. A remote tenant needs
`allow.provider_info` and a `base_url` that is allowed in origin and path
(`provider_base_urls`; see [Server](./server.md)), and may make 10
`list_models` and `model_info` calls a minute, then gets `LimitReached`
(`kind` `provider_calls`).

## Temperature

`spec.temperature` (0 to 2) goes with every request when set, `0` included, and
stays off the wire when unset. `agent.open` fails for a value outside 0 to 2, or
above 1 on `anthropic`. Each adapter sends it as given (checked 2026-11-17 against
the vendor references):

| Provider | Field | Notes |
|---|---|---|
| `openai-chat`, `openai-responses`, `vllm`, `sglang`, `llamacpp` | `temperature` | 0 to 2. OpenAI says parameter support varies by model; a model that refuses it answers with an HTTP error, which Linch does not predict. |
| `anthropic` | `temperature` | 0 to 1. Claude Opus 4.7 and later, Sonnet 5 and later and Haiku 5.5 answer 400 to a non-default value on every request; older models refuse it only while thinking is on; API version `2026-10-21` removes it. |
| `deepseek` | `temperature` | 0 to 2. Has no effect in thinking mode (the default), where it is sent and ignored. |
| `gemini` | `generationConfig.temperature` | 0.0 to 2.0. Silently ignored on Gemini 3.6 Flash and newer models. |

## Failures and retries

A run retries a provider call that failed with an error another call may
fix: no connection or no response at all (refused, reset, timeout), an HTTP
408, 429 or 5xx (Anthropic's 529 overload included), or an error the provider
sent inside its stream with one of those statuses. Adapters never retry; the
run does. A failure it does not retry ends the run with an `error` event and
a `result` of subtype `error`; the error event's `error` dict carries `kind`
(`connect`, `transport`, `status`, `context_length`, `invalid_request`,
`stream`), `retryable` and, for HTTP failures, `status`. A prompt the
provider refuses as longer than the model's context window (`context_length`)
is never retried: with `spec.compaction` the run shortens it and calls again
([Memory](./memory.md#when-the-prompt-does-not-fit)). A cancelled call ends the run
`aborted`. Each adapter has a 30 s connect timeout and a read timeout between
reads (300 s or 600 s); there is no deadline on a whole call.

Two [`spec`](./agent.md#spec) fields shape this:

| Field | Default | What it controls |
|---|---|---|
| `provider_retry` | on, with the defaults below | Call the same model again after a wait: `{"max_attempts"?, "base_delay_ms"?, "max_delay_ms"?, "jitter"?}`, typed as `ProviderRetry`. Set only the fields to change. `{"max_attempts": 1}` turns retries off |
| `fallback_models` | `[]` | Models to fail over to, in order, once a model has no retry left. Not blank, not `model`, no repeats |

| `provider_retry` field | Default | What it controls |
|---|---|---|
| `max_attempts` | `5` | Most calls to one model in one turn, the first included; `1` never retries |
| `base_delay_ms` | `1000` | Wait before the first retry; each later retry waits twice as long |
| `max_delay_ms` | `30000` | Longest wait. A server's `Retry-After` is waited as given, unjittered; one longer than this ends the retries on that model rather than hold the run |
| `jitter` | `0.2` | Fraction of each wait added or taken away, from 0 to 1, so runs that failed together do not call again together |

The defaults are Python Linch's `RetryOptions`: at most 1 + 2 + 4 + 8 = 15 s
of waiting (±20%) before a model is given up, unless the server asks for
longer.

What a run does after a retryable failure:

1. While the model has calls left this turn, the run sends a
   `ProviderRetryEvent` (`model`, `retry` from 1, `delay_ms`, `error`), waits,
   and calls the model again. Text the failed call streamed is void: the next
   call answers from the start, so drop what you showed of it.
2. Then, with a fallback left, it sends a `ModelFallbackEvent` (`from_model`,
   `to_model`, `error`) and calls the fallback at once, with its own retries.
   The run keeps that model for the rest of the run, compaction included. A
   turn that failed over calls the fallback even if a skill set its model.
3. Otherwise the run ends with the last failure's `error`.

A failure that is not retryable (400, 401, 403, 404, a request the adapter
refused) ends the run at once, without a retry or a failover. A cancel during
a wait ends the run `aborted` at once.

```python
import asyncio

from linch_rust import (
    AgentConfig,
    ModelFallbackEvent,
    ProviderRetryEvent,
    ResultEvent,
    Server,
    UsageEvent,
)
from linch_rust.scripted import MODEL, text_turn

overloaded = {"error": {"type": "status", "status": 529, "message": "overloaded"}}
config: AgentConfig = {
    "namespace": "test",
    "spec": {
        "model": MODEL,
        # Two calls per model, 0.5 s apart; jitter 0 waits exactly.
        "provider_retry": {"max_attempts": 2, "base_delay_ms": 500, "jitter": 0},
        "fallback_models": ["backup-model"],
    },
    "provider": {
        "id": "scripted",
        "turns": [overloaded, overloaded, text_turn("Done.", model="backup-model")],
    },
}


async def main() -> None:
    async with Server() as server:
        agent = await server.open_agent(config)
        async for envelope in await agent.run("s1", "Hi"):
            event = envelope.event
            if isinstance(event, ProviderRetryEvent):
                print(f"retry {event.retry} of {event.model} in {event.delay_ms} ms")
            elif isinstance(event, ModelFallbackEvent):
                print(f"{event.from_model} -> {event.to_model}: {event.error['message']}")
            elif isinstance(event, UsageEvent):
                print(f"answered by {event.actual_model}")
            elif isinstance(event, ResultEvent):
                print(event.subtype, event.final_text)


asyncio.run(main())
```

```text
retry 1 of scripted-model in 500 ms
scripted-model -> backup-model: provider returned HTTP 529: overloaded
answered by backup-model
success Done.
```

The `usage` event's `actual_model` and the cost fields name the model that
answered. The fallback models run on the agent's one provider, so list models
that provider serves.

Each attempt is its own durable call, so a crash never buys an answer twice:

- A run resumed after a crash during a wait calls again at once, without
  waiting the rest, and keeps counting the turn's retries.
- A run that failed over resumes on its fallback.
- With an `after_provider_call` callback attached, a paid answer's usage is
  written before the callbacks run (`provider_call_stop` included). If the server
  crashes while one runs, the resumed run keeps that usage and cost in its
  totals and calls the model again; that call spends none of the turn's
  retries.
- A failed attempt that cost nothing, a connection never made or a refusal
  before any output (a 4xx status, 408 and 429 included, or 503 or 529),
  counts as zero usage: it sends no `usage` event and leaves
  `usage_complete` true.
- A request that went out with nothing back (a reset, a read timeout), a
  500, 502 or 504 (a gateway can send one after the model already ran and
  billed), any other error status, or a failure after output began, may
  have been billed, so its usage is unknown and the run's `result` has
  `usage_complete` false.
- Under a capped [`budget`](./agent.md#budget), a call of unknown usage means
  the cap can no longer be kept, so the run is not retried after one: it ends
  with the provider's error. A call that cost nothing is retried as usual.
  Python Linch counts every failed call as unknown, so under a cap it never
  retries.

Both fields are part of the [run contract](./agent.md#the-run-contract), and
a subagent's runs use them too. The defaults leave the contract as it was
before retries existed, and `{"max_attempts": 1}` changes it. Python Linch
also recovers from a prompt that is too long by compacting and calling
again; this server does not.

## Limit concurrent calls

Many runs on one API key hit its rate limit together. `max_provider_concurrency`
(an integer of at least 1, in the [agent config](./agent.md#top-level-fields))
caps the provider calls in flight; a call over the cap waits its turn, first
come first served. It is Python Linch's `max_provider_concurrency`, a
semaphore around every live provider call, and there is no rate or token
limit, as there is none in Python Linch.

```python
config = {
    "namespace": "support",
    "spec": {"model": "deepseek-flash"},
    "provider": {"id": "deepseek"},
    "max_provider_concurrency": 2,
}
```

- **What it counts.** Each model call: a turn's call, a compaction summary
  and a subagent's calls, all through the one limit. A retry's backoff, the
  summary after a context-length refusal and the hooks around a call do not
  hold a place, so a limit of 1 does not deadlock a run on itself.
- **Scope.** All runs of the agent. Agents opened on one server that use the
  same provider key and `base_url` origin (scheme, host and port, as the
  tenant's `provider_base_urls` match it) share one limit, so `openai-chat`
  and `openai-responses` on one `OPENAI_API_KEY` count together, and so do
  two `base_url`s that differ only in path or query; a different key variable
  or origin counts apart. A config that leaves `base_url` unset and one that
  spells out the provider's default are two origins to this count. Agents that ask for different numbers on
  one key share the limit of the first, which lasts until no open agent
  uses it. For a remote tenant that is every agent and run of the tenant
  ([the tenant's cap](./server.md#what-admission-refuses-and-why)). The
  limit is the server process's: a [worker](./scheduler.md#run-a-worker) is
  another process, and a second server another, each with its own count.
  A limit shared across processes is not built.
- **What a caller sees.** Nothing but time: the run is `running` and emits
  no event while it waits. It has dispatched nothing, so no delivery or
  receipt is written for the call (Python Linch begins its call record
  before it waits; this server does not). Cancelling a run that waits aborts
  it with no call, and it leaves the queue without taking a place.
- **Durability.** If a run must wait and has events not yet written (a failed
  call's receipt before a failover), they are written first; a run that
  finds a place free writes nothing extra.
- **Contract.** Unset is no limit and is not sent. The limit is not part of
  the [run contract](./agent.md#the-run-contract), so changing it never
  strands a suspended run.

## Cost

Usage and result events carry `cost_usd`, `cumulative_cost_usd` and
`total_cost_usd` when the serving model has a price. The built-in price table
holds Anthropic models only; every other model reports `None`. See
[Events](./events.md#usage-and-cost).

## Related pages

- [Agent config](./agent.md#spec): `thinking`, `effort`, `max_output_tokens`.
- [Events](./events.md): the `error`, `usage` and `result` events.
- [Troubleshooting](./troubleshooting.md#opening-an-agent): open errors.
