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
| `deepseek` | `DEEPSEEK_API_KEY` | `https://api.deepseek.com` | `base_url` |
| `openai-chat` | `OPENAI_API_KEY` | `https://api.openai.com/v1` | `base_url` |
| `openai-responses` | `OPENAI_API_KEY` | `https://api.openai.com/v1` | `base_url`, `reasoning_summary`, `store`, `native_catalog_changes` |
| `anthropic` | `ANTHROPIC_API_KEY` | `ANTHROPIC_BASE_URL`, else `https://api.anthropic.com` | `base_url`, `native_catalog_changes` |
| `gemini` | `GEMINI_API_KEY` | `https://generativelanguage.googleapis.com` | `base_url` |
| `vllm` | `VLLM_API_KEY`, optional | none | `base_url`, required |
| `sglang` | `SGLANG_API_KEY`, optional | none | `base_url`, required |
| `llamacpp` | `LLAMA_API_KEY`, optional | none | `base_url`, required |
| `scripted` | none | none | `turns`, required |

`base_url` replaces the provider's endpoint. Any other field is refused at open
with a `ProtocolError` (code -32602), so a config can never carry an
`api_key`.

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
ignore `spec.thinking`.

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
call: `transport` (`message`), `status` (`status`, `message`,
`retry_after_ms?`) or `invalid_request` (`message`). With no `events`, the
call fails before any output:

```python
import asyncio

from linch_rust import AgentConfig, ErrorEvent, ResultEvent, Server
from linch_rust.scripted import MODEL

config: AgentConfig = {
    "namespace": "test",
    "spec": {"model": MODEL},
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
ResultEvent(subtype='error', stop_reason='error', total_usage={...}, usage_complete=False, ...)
```

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

## Failures and retries

Nothing retries a provider call: not the adapter, not the run. A failed call
(connect error, HTTP status, a stream that breaks its rules) ends the run with
an `error` event and a `result` of subtype `error`. The error event's `error`
dict carries `retryable` and, for HTTP failures, `status`, so you can decide
to start a new run. A cancelled call ends the run `aborted`. Each adapter has
a 30 s connect timeout and a read timeout between reads (300 s or 600 s); there
is no deadline on a whole call.

## Cost

Usage and result events carry `cost_usd`, `cumulative_cost_usd` and
`total_cost_usd` when the serving model has a price. The built-in price table
holds Anthropic models only; every other model reports `None`. See
[Events](./events.md#usage-and-cost).

## Related pages

- [Agent config](./agent.md#spec): `thinking`, `effort`, `max_output_tokens`.
- [Events](./events.md): the `error`, `usage` and `result` events.
- [Troubleshooting](./troubleshooting.md#opening-an-agent): open errors.
