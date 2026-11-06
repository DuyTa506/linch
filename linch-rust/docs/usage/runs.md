# Runs

Use this page to start, stream, resume, approve and cancel runs, and to know
what survives a crash.

[<- Usage guide](./README.md)

A run is one prompt in one session. `agent.run`, `agent.resume` and
`agent.approve` each return a `Run` as soon as the server accepts the call;
the run's events then stream in as `Envelope`s.

| Call | Does |
|---|---|
| `await agent.run(session_id, prompt, *, run_id=None, principal=None)` | Start a run; the session is created when missing |
| `await agent.resume(run_id, *, principal=None)` | Continue a run after a crash or a dropped stream |
| `await agent.approve(run_id, decisions, *, principal=None)` | Answer a suspended run's pending calls and continue it |
| `async for envelope in run` | Every envelope of this call, in order, ending after `result` or `suspended` |
| `await run.result()` | Read the rest and return the `ResultEvent` or `SuspendedEvent` |
| `await run.cancel()` | Abort the run; `True` when the server was driving it |
| `await agent.events(run_id, after_seq=0)` | The run's durable envelopes after `after_seq`, from the stores |
| `run.id` | The run id, for `resume`, `approve` and `events` |
| `run.trace_id` | The trace id of the call's spans when it is traced ([Observability](./observability.md#correlate-logs-and-traces)) |

---

## Start and stream

```python
import asyncio

from linch_rust import AgentConfig, ResultEvent, Server, SessionBusy, SuspendedEvent, tool
from linch_rust.scripted import MODEL, text_turn, tool_turn


@tool(scope="write")
async def save(text: str) -> str:
    """Save a note."""
    return "saved"


config: AgentConfig = {
    "namespace": "runs-demo",
    "spec": {"model": MODEL},
    "provider": {
        "id": "scripted",
        "turns": [text_turn("First answer."), tool_turn("c1", "save", {"text": "x"})],
    },
}


async def main() -> None:
    async with Server() as server:
        agent = await server.open_agent(config, tools=[save])

        # Iterate for every envelope, or await result() for the end only.
        run = await agent.run("s1", "Hello", run_id="order-42-reply")
        result = await run.result()
        assert isinstance(result, ResultEvent)
        print(run.id, result.subtype, result.final_text)

        # Replay what was durable, from the start or after a seq you saw.
        replayed = await agent.events(run.id)
        print([e.event.type for e in await agent.events(run.id, after_seq=replayed[2].seq)])

        # A second live run on a session is refused, naming the live one.
        waiting = await agent.run("s1", "Save x")
        assert isinstance(await waiting.result(), SuspendedEvent)
        try:
            await (await agent.run("s1", "Something else")).result()
        except SessionBusy as busy:
            print("busy with", busy.run_id == waiting.id)


asyncio.run(main())
```

```text
order-42-reply success First answer.
['assistant', 'usage', 'result']
busy with True
```

- A stream ends with `result` when the run finished, or `suspended` when it
  waits for an approval. `result.subtype` is `success`, `error`, `aborted` or
  `interrupted`.
- You can iterate part of a run and call `result()` for the rest. Stopping
  iteration does not stop the run: the server goes on driving it and its
  envelopes wait in memory. Cancel it if you no longer want it.
- **Choose the run id** when you need to find the run again even if `run()`
  never returned (a timeout, a crash of your process). The server picks a UUID
  when you do not. An id already stored ends the run with `RunError` (code
  -32023) and writes nothing.
- Many runs of one server stream at once; each `Run` gets only its own
  envelopes.

### One live run per session

A session has at most one run that is not finished: one running or waiting for
approval. Starting another ends the new run with `SessionBusy` (code -32030)
before anything is written; `busy.session_id` and `busy.run_id` name the live
run. Resume, approve or cancel that run first, or queue the prompt. With
Postgres the rule holds across every server on the database.

## Errors arrive at the end of the stream

`run()`, `resume()` and `approve()` return before the server has done
anything but accept the call. A refusal (a busy session, an unknown run, a
contract that differs) and a failure to continue durably arrive as the end of
the stream: iterating, or `result()`, raises a `RunError` subclass.

| Exception | Code | When |
|---|---|---|
| `RunNotFound` | -32021 | No such run in the agent's stores (with memory stores: not in this handle) |
| `RunError` | -32023 | `run_id` is already stored |
| `ContractMismatch` | -32025 | The run started under another contract ([Agent config](./agent.md#the-run-contract)); `data["expected"]` is the run's contract and `data["found"]` this agent's, as the message names them |
| `RunTerminal` | -32026 | The run already finished; `data.status` is its status |
| `NotSuspended` | -32027 | `approve` on a run that is not waiting for approval ([below](#approve)) |
| `PromptBlocked` | -32029 | A `user_prompt_submit` callback refused the prompt; `.reason` says why. No run or session was created |
| `SessionBusy` | -32030 | The session has a live run |
| `RunError` | -32020 to -32028 | A store or journal write failed, or the stored state does not fit together; the run stays at its last durable state |
| `ConnectionClosed` | none | The server went away mid-stream |

One refusal comes from the call itself: `RunBusy` (code -32002) when this
server already drives that run id, for example a `resume` of a run you are
still streaming. `agent.events` raises its errors directly too. A provider or
tool failure is not an exception: it is an `error` event and a `result` of
subtype `error`. Every message is on [Troubleshooting](./troubleshooting.md).

## Cancel

```python
import asyncio

from linch_rust import AgentConfig, ResultEvent, RunBusy, Server, ToolCallStartEvent, tool
from linch_rust.scripted import MODEL, text_turn, tool_turn


@tool(scope="read")
async def crawl(url: str) -> str:
    """Fetch a page, slowly."""
    try:
        await asyncio.sleep(3600)
    except asyncio.CancelledError:
        print("crawl cancelled")
        raise
    return "page"


config: AgentConfig = {
    "namespace": "runs-demo",
    "spec": {"model": MODEL},
    "provider": {
        "id": "scripted",
        "turns": [tool_turn("c1", "crawl", {"url": "https://example.com"}), text_turn("unused")],
    },
}


async def main() -> None:
    async with Server() as server:
        agent = await server.open_agent(config, tools=[crawl])
        run = await agent.run("s1", "Crawl example.com")
        async for envelope in run:
            if isinstance(envelope.event, ToolCallStartEvent):
                try:
                    await agent.resume(run.id)
                except RunBusy:
                    print("this server already drives", run.id == envelope.run_id)
                print("cancelled:", await run.cancel())
            elif isinstance(envelope.event, ResultEvent):
                print(envelope.event.subtype)
        print("cancel again:", await run.cancel())


asyncio.run(main())
```

```text
this server already drives True
cancelled: True
aborted
crawl cancelled
cancel again: False
```

`cancel()` aborts the model call or tool in flight (a host tool's task is
cancelled) and ends the run with a `result` of subtype `aborted`; keep reading
the stream to see it. It returns `False`, not an error, when the server no
longer drives the run. An aborted run is finished: its session takes a new run.
Only the server that drives a run can cancel it.

## Resume

`resume(run_id)` continues a run from its last durable state. Use it after
your process or the server crashed, after the stream dropped, or to pick up a
run another process started. A run waiting for approval suspends again at
once. Resume only after the earlier stream ended (its iteration finished or
raised); while a server still drives the run, `resume` is `RunBusy`.

Which handle can resume depends on the stores:

- **Memory stores**: the run lives in the agent handle that started it. Resume
  and approve it through that same `Agent`; it is gone when the server stops.
- **Postgres stores**: any agent opened with the same config and tools (so the
  same contract), on any server, can resume it.

On Postgres, after a crash (this reads the DSN from `DATABASE_URL`):

```python
import asyncio
import os
import signal

from linch_rust import (
    AgentConfig,
    ConnectionClosed,
    ResultEvent,
    Server,
    ToolCallStartEvent,
    UserEvent,
    tool,
)
from linch_rust.scripted import MODEL, text_turn, tool_turn


@tool(scope="read")
async def lookup(q: str) -> str:
    """Look something up, slowly."""
    await asyncio.sleep(3600)
    return q


def config(turns: list) -> AgentConfig:
    return {
        "namespace": "runs-demo",
        "spec": {"model": MODEL},
        "provider": {"id": "scripted", "turns": turns},
        "stores": {"type": "postgres", "dsn": os.environ["DATABASE_URL"]},
    }


async def main() -> None:
    # First process: the server dies while a tool call is in flight.
    async with Server() as server:
        agent = await server.open_agent(config([tool_turn("c1", "lookup", {"q": "x"})]), tools=[lookup])
        run = await agent.run("s1", "Look up x", run_id="job-7")
        try:
            async for envelope in run:
                if isinstance(envelope.event, ToolCallStartEvent):
                    os.kill(server.pid, signal.SIGKILL)
        except ConnectionClosed as closed:
            print("lost the server:", closed)

    # Later, another process: same config and tools, so the same contract.
    async with Server() as server:
        agent = await server.open_agent(config([text_turn("Recovered.")]), tools=[lookup])
        resumed = await agent.resume("job-7")
        result = await resumed.result()
        assert isinstance(result, ResultEvent)
        print(result.subtype, result.final_text)
        for envelope in await agent.events("job-7"):
            if isinstance(envelope.event, UserEvent) and envelope.event.subtype == "tool_result":
                print(envelope.event.message["content"][0]["content"][0]["text"])


asyncio.run(main())
```

```text
lost the server: the server closed its output
success Recovered.
Tool execution was interrupted before a result was recorded; not re-running automatically on resume.
```

A tool call whose start was recorded but whose end was not is never run again:
the model gets an error result saying it was interrupted, and decides what to
do. Make host tools that change things safe to call twice anyway, or check
before acting, since the crash may have come after the effect and before the
record.

## Approve

`approve(run_id, decisions)` takes an `Allow()` or `Deny(reason)` per
`tool_use_id` of the `suspended` event; a call left out is asked again. The
loop that answers approvals is on [Tools](./tools.md#approvals).

Only a run waiting for an approval takes one. Once an approve has committed
its answers, the same approve sent again (because your process crashed or the
connection dropped before you saw the outcome) ends with `NotSuspended` and
writes nothing; continue that run with `resume`, which keeps the committed
decisions. An approve on a finished run is `RunTerminal`.

```python
import asyncio
import os
import signal

from linch_rust import (
    Agent,
    AgentConfig,
    Allow,
    ConnectionClosed,
    Decision,
    NotSuspended,
    ResultEvent,
    Server,
    SuspendedEvent,
    ToolCallStartEvent,
    tool,
)
from linch_rust.scripted import MODEL, text_turn, tool_turn


@tool(scope="write")
async def deploy(service: str) -> str:
    """Deploy a service."""
    await asyncio.sleep(3600)
    return "deployed"


def config(turns: list) -> AgentConfig:
    return {
        "namespace": "runs-demo",
        "spec": {"model": MODEL},
        "provider": {"id": "scripted", "turns": turns},
        "stores": {"type": "postgres", "dsn": os.environ["DATABASE_URL"]},
    }


async def approve_once(
    agent: Agent, run_id: str, decisions: dict[str, Decision]
) -> ResultEvent | SuspendedEvent:
    """Approve, or continue a run whose approval was already committed."""
    try:
        return await (await agent.approve(run_id, decisions)).result()
    except NotSuspended:
        return await (await agent.resume(run_id)).result()


async def main() -> None:
    async with Server() as server:
        agent = await server.open_agent(config([tool_turn("c1", "deploy", {"service": "api"})]), tools=[deploy])
        run = await agent.run("s1", "Deploy api", run_id="deploy-1")
        assert isinstance(await run.result(), SuspendedEvent)
        approved = await agent.approve("deploy-1", {"c1": Allow()})
        try:
            async for envelope in approved:
                if isinstance(envelope.event, ToolCallStartEvent):
                    os.kill(server.pid, signal.SIGKILL)  # a crash mid-deploy
        except ConnectionClosed:
            print("lost the server; did the approve land?")

    async with Server() as server:
        agent = await server.open_agent(config([text_turn("Deploy was interrupted.")]), tools=[deploy])
        result = await approve_once(agent, "deploy-1", {"c1": Allow()})
        assert isinstance(result, ResultEvent)
        print(result.final_text)


asyncio.run(main())
```

```text
lost the server; did the approve land?
Deploy was interrupted.
```

## Acting for a principal

`principal` says who a run acts for: `{"tenant", "user", "roles"?}` (the
`Principal` typed dict). Permission rules with `role` or `unless_role` match
its roles ([Tools](./tools.md#permission-rules)), and a `permission_request`
callback's request carries it. Pass it to `run`, and again to each `resume`
and `approve`: each call acts for the principal it is given, and one without
acts for nobody. On a remote server, `tenant` must be the connection's tenant
([Server](./server.md#tenants-and-namespaces)).

```python
import asyncio

from linch_rust import AgentConfig, Principal, Server, tool
from linch_rust.scripted import MODEL, text_turn, tool_turn


@tool(scope="write")
async def refund(order: str) -> str:
    """Refund an order."""
    return f"refunded {order}"


config: AgentConfig = {
    "namespace": "demo",
    "spec": {"model": MODEL},
    "provider": {"id": "scripted", "turns": [
        tool_turn("c1", "refund", {"order": "A-1"}),  # the clerk's run
        tool_turn("c1", "refund", {"order": "A-1"}),  # the manager's run
        text_turn("Refunded A-1."),
    ]},
    "permissions": {
        "mode": "default",
        "project_root": "/",
        "layers": [{"name": "app", "rules": [
            {"type": "tool", "tool": "refund", "decision": "allow", "role": "manager"},
        ]}],
    },
}
clerk: Principal = {"tenant": "acme", "user": "bob"}
manager: Principal = {"tenant": "acme", "user": "ada", "roles": ["manager"]}


async def main() -> None:
    async with Server() as server:
        agent = await server.open_agent(config, tools=[refund])
        for session, who in (("s1", clerk), ("s2", manager)):
            stopped = await (await agent.run(session, "Refund A-1", principal=who)).result()
            print(who["user"], "->", type(stopped).__name__)


asyncio.run(main())
```

```text
bob -> SuspendedEvent
ada -> ResultEvent
```

## What is durable, and when

With Postgres stores, the run is written at fixed points, and nothing with an
effect outside the run happens before the state that led to it is written:

| Point | What is durable once it passes |
|---|---|
| Before each model call | The run so far and a record that the call was sent |
| After each model response | The assistant message, its usage and cost |
| Before each tool runs | Its `tool_call_start`, so a resume never runs it a second time |
| After each tool returns | Its `tool_call_end` |
| Before the conversation is appended | The checkpoint that matches it |
| At the end | The `result` (or `suspended`) and the final status |

What that means for you:

- A durable event reaches your stream only after it is written. If you saw
  seq `n`, `agent.events(run_id, after_seq=n)` returns exactly what came after,
  even from another process.
- Stream-only events (`partial_assistant`, `tool_progress`, `hook`) arrive at
  once and are never stored or replayed.
- A model call that was sent but whose response was not recorded is sent again
  on resume, so a crash at that moment can cost one call twice.
- A tool whose start was recorded is never run again on resume ([above](#resume)).
- Closing the `Server` block, or the server dying, leaves each run at its last
  durable state; a Postgres run is resumable, a memory run is gone.

With memory stores the same points apply, but the stores are inside the server
process. How many writes a turn costs and how they are grouped is in the
[architecture stores page](../architecture/stores.md#group-commit-write-counts).

## Related pages

- [Events](./events.md): every event in the stream.
- [Tools](./tools.md#approvals): approval loops and permission rules.
- [Stores](./stores.md): memory and Postgres.
- [Troubleshooting](./troubleshooting.md#runs): run errors and fixes.
