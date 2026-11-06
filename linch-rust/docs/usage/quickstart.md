# Quickstart

Use this path to run a first agent offline, then add a live provider, a host
tool, an approval and Postgres persistence.

[<- Usage guide](./README.md)

You write Python; the agent loop runs in `linch-server`, a Rust sidecar the
client starts for you as a child process. Nothing on this page needs Rust
except building from a checkout.

## 1. Install

The `linch-rust` wheel holds the Python client and the `linch-server` binary
for its platform. It needs Python 3.11 or newer. For a published release:

```bash
pip install linch-rust              # client and server binary
pip install 'linch-rust[otel]'      # with OpenTelemetry spans and metrics
```

From a checkout, build the wheel (this needs a Rust toolchain) and install it:

```bash
cd linch-rust/python
uv build                            # writes dist/linch_rust-*.whl
pip install dist/linch_rust-*.whl
```

When you work on the checkout itself, `cargo build -p linch-server` is
enough: the client finds `linch-rust/target/debug/linch-server` (or the newer
of the debug and release builds) when no wheel is installed. See
[where the binary is found](./troubleshooting.md#the-server-binary).

Prefer a ready-made project? `linch-rust new my-agent` writes one that runs
and tests offline ([CLI](./cli.md)). The rest of this page builds the same
shapes by hand.

## 2. Run offline

The scripted provider replays turns you write, so this needs no key and no
network. Save it as `quickstart_offline.py`:

```python
import asyncio

from linch_rust import AgentConfig, ResultEvent, Server
from linch_rust.scripted import MODEL, text_turn

config: AgentConfig = {
    "namespace": "quickstart",
    "spec": {"model": MODEL},
    "provider": {"id": "scripted", "turns": [text_turn("Hello from linch-rust.")]},
}


async def main() -> None:
    async with Server() as server:
        agent = await server.open_agent(config)
        run = await agent.run("session-1", "Say hello")
        async for envelope in run:
            print(envelope.seq, envelope.event.type)
            if isinstance(envelope.event, ResultEvent):
                print(envelope.event.final_text)


asyncio.run(main())
```

```bash
python3 quickstart_offline.py
```

```text
1 init
2 user
3 context_build
4 partial_assistant
6 assistant
7 usage
8 result
Hello from linch-rust.
```

`Server()` starts the sidecar and stops it when the block ends.
`open_agent` opens an agent from its config and returns a handle.
`agent.run(session_id, prompt)` starts a run and returns it at once; iterating
it yields one `Envelope` per event, ending with the `result`. Sequence numbers
grow but skip some values: those are accounting records the client never sees
([Events](./events.md#sequence-numbers)).

## 3. Run with a live provider

Export a key, then point the config at the provider. The server reads the key
from the environment it inherits; the config never carries it.

```bash
export OPENAI_API_KEY="..."
```

This needs a key (the open was checked with a fake one, the call was not). It
prints the answer as it streams:

```python
import asyncio

from linch_rust import AgentConfig, PartialAssistantEvent, ResultEvent, Server

config: AgentConfig = {
    "namespace": "quickstart",
    "spec": {"model": "gpt-5", "system_prompt": "Answer in one paragraph."},
    "provider": {"id": "openai-responses"},
}


async def main() -> None:
    async with Server() as server:
        agent = await server.open_agent(config)
        run = await agent.run("session-1", "What is a sidecar process?")
        async for envelope in run:
            event = envelope.event
            if isinstance(event, PartialAssistantEvent) and event.delta.get("kind") == "text":
                print(event.delta["text"], end="", flush=True)
            elif isinstance(event, ResultEvent):
                print(f"\n[{event.subtype}]")


asyncio.run(main())
```

Other providers (DeepSeek, Anthropic, Gemini, vLLM, SGLang, llama.cpp) differ
only in the `provider` dict; see [Providers](./providers.md).

## 4. Add a host tool

A host tool is an async Python function the server calls when the model asks
for it. `scope` is required: it decides whether the call needs approval.

```python
import asyncio

from linch_rust import AgentConfig, ResultEvent, Server, ToolCallEndEvent, tool
from linch_rust.scripted import MODEL, text_turn, tool_turn


@tool(scope="read")
async def service_status(name: str) -> str:
    """Return the current status of a service."""
    return f"{name}: healthy"


config: AgentConfig = {
    "namespace": "quickstart",
    "spec": {"model": MODEL},
    "provider": {
        "id": "scripted",
        "turns": [
            tool_turn("call_1", "service_status", {"name": "billing"}),
            text_turn("Billing is healthy."),
        ],
    },
}


async def main() -> None:
    async with Server() as server:
        agent = await server.open_agent(config, tools=[service_status])
        run = await agent.run("session-1", "Is billing up?")
        async for envelope in run:
            event = envelope.event
            if isinstance(event, ToolCallEndEvent):
                print(event.tool_name, "->", event.result["content"][0]["text"])
            elif isinstance(event, ResultEvent):
                print(event.final_text)


asyncio.run(main())
```

```text
service_status -> billing: healthy
Billing is healthy.
```

The input schema comes from the type hints and the description from the
docstring. More on [Tools](./tools.md#host-tools).

## 5. Approve a write

In the default permission mode, `read` calls run and `write` and `exec` calls
ask. A call that asks suspends the run: its stream ends with a `suspended`
event listing the pending calls, and `agent.approve` continues it.

```python
import asyncio

from linch_rust import AgentConfig, Allow, ResultEvent, Server, SuspendedEvent, tool
from linch_rust.scripted import MODEL, text_turn, tool_turn

notes: list[str] = []


@tool(scope="write")
async def save_note(text: str) -> str:
    """Save a note."""
    notes.append(text)
    return "saved"


config: AgentConfig = {
    "namespace": "quickstart",
    "spec": {"model": MODEL},
    "provider": {
        "id": "scripted",
        "turns": [tool_turn("call_1", "save_note", {"text": "buy milk"}), text_turn("Saved.")],
    },
}


async def main() -> None:
    async with Server() as server:
        agent = await server.open_agent(config, tools=[save_note])
        run = await agent.run("session-1", "Remember to buy milk")
        stopped = await run.result()
        assert isinstance(stopped, SuspendedEvent)
        print("waiting for", stopped.pending_request_ids)

        decisions = {call: Allow() for call in stopped.pending_request_ids}
        run = await agent.approve(run.id, decisions)
        result = await run.result()
        assert isinstance(result, ResultEvent)
        print(result.final_text, notes)


asyncio.run(main())
```

```text
waiting for ['call_1']
Saved. ['buy milk']
```

`Deny("reason")` refuses a call instead; the model reads the reason. See
[Tools](./tools.md#approvals) and [Runs](./runs.md#approve).

## 6. Keep state across restarts

With the default memory stores, runs and sessions live in the agent handle and
are gone when the server stops. Postgres stores keep them: a new server, or
another process, continues the same session. The server creates its tables
when the agent opens.

This reads the DSN from `DATABASE_URL`, for example
`postgres://user:pass@127.0.0.1:5432/linch`:

```python
import asyncio
import os

from linch_rust import AgentConfig, ContextBuildEvent, ResultEvent, Server
from linch_rust.scripted import MODEL, text_turn


def config(reply: str) -> AgentConfig:
    return {
        "namespace": "quickstart",
        "spec": {"model": MODEL},
        "provider": {"id": "scripted", "turns": [text_turn(reply)]},
        "stores": {"type": "postgres", "dsn": os.environ["DATABASE_URL"]},
    }


async def ask(prompt: str, reply: str) -> None:
    # A new server each time, as after a restart.
    async with Server() as server:
        agent = await server.open_agent(config(reply))
        run = await agent.run("session-1", prompt)
        async for envelope in run:
            if isinstance(envelope.event, ContextBuildEvent):
                print("the model sees", envelope.event.messages, "messages")
            elif isinstance(envelope.event, ResultEvent):
                print(envelope.event.final_text)


async def main() -> None:
    await ask("My name is Ada.", "Nice to meet you, Ada.")
    await ask("What is my name?", "Your name is Ada.")


asyncio.run(main())
```

```text
the model sees 1 messages
Nice to meet you, Ada.
the model sees 3 messages
Your name is Ada.
```

The second server sees the first conversation: the prompt, the answer and the
new prompt. See [Stores](./stores.md).

## Next

- [Agent config](./agent.md): every field, the run contract, callbacks.
- [Runs](./runs.md): resume after a crash, cancel, replay events.
- [Events](./events.md): every event the client decodes.
- [Troubleshooting](./troubleshooting.md): errors and their fixes.
