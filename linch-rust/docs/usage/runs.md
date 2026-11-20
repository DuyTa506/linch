# Runs

Use this page to start, stream, resume, approve, answer and cancel runs, and to know
what survives a crash.

[<- Usage guide](./README.md)

A run is one prompt in one session. `agent.run`, `agent.resume`,
`agent.approve` and `agent.answer` each return a `Run` as soon as the server accepts the call;
the run's events then stream in as `Envelope`s.

| Call | Does |
|---|---|
| `await agent.run(session_id, prompt, *, run_id=None, principal=None, trace_id=None, output_schema=None, tool_choice=None)` | Start a run; the session is created when missing. `trace_id` is yours, echoed on the run's `init` and `result` events ([Observability](./observability.md#correlate-logs-and-traces)). `output_schema` and `tool_choice` replace the agent's for this run ([Structured output](./structured-output.md#give-one-run-its-own-schema)) |
| `await agent.resume(run_id, *, principal=None)` | Continue a run after a crash or a dropped stream |
| `await agent.approve(run_id, decisions, *, principal=None)` | Answer a suspended run's pending calls and continue it |
| `await agent.answer(run_id, answers, *, principal=None)` | Answer a suspended run's `AskUser` questions and continue it |
| `async for envelope in run` | Every envelope of this call, in order, ending after `result` or `suspended` |
| `await run.result()` | Read the rest and return the `ResultEvent` or `SuspendedEvent` |
| `await run.cancel()` | Abort the run, driven or suspended; `True` when it was cancelled |
| `await agent.steer(run_id, text, *, steer_id=None, principal=None)` | Send text to a run that is going on; it reads it at its next turn boundary ([below](#steer)) |
| `await run.steer(text, *, steer_id=None)` | The same, for this run |
| `await agent.cancel(run_id)` | Abort a run by id, a suspended one included, from any process ([below](#cancel-a-suspended-run)) |
| `await agent.events(run_id, after_seq=0)` | The run's durable envelopes after `after_seq`, from the stores; a long run is read page by page ([below](#long-runs-are-read-in-pages)) |
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
  waits for an approval (status `awaiting_approval`) or an answer (status
  `awaiting_input`). `result.subtype` is `success`, `error`, `aborted` or
  `interrupted`.
- You can iterate part of a run and call `result()` for the rest. Stopping
  iteration does not stop the run: the server goes on driving it and its
  envelopes wait in memory. Cancel it if you no longer want it.
- **Choose the run id** when you need to find the run again even if `run()`
  never returned (a timeout, a crash of your process). The server picks a UUID
  when you do not. An id already stored ends the run with `RunExists` (code
  -32023) and writes nothing.
- **An acknowledged run id is durable.** `run()` returns only once the run is
  stored (with Postgres, once its create has committed), so the id it hands you
  exists after any crash that follows: `resume` finds it, and `agent.events`
  never says `RunNotFound` for it. Until `run()` returns the run may or may not
  exist (the create can commit while the answer is on its way, or after your
  process stopped waiting): `resume` the id you chose, retrying `RunNotFound`
  for a moment, and start it again with the same id only if it stays missing
  (`RunExists` then means it landed meanwhile). `run_or_resume`
  ([Workflows](./workflows.md#run_or_resume)) does the second half for a
  retried job: it starts with the id and resumes on `RunExists`. A refused start (a
  busy session, a blocked prompt) still returns, and the refusal arrives as
  the end of the stream. `resume`, `approve` and `answer` return at once.
- Many runs of one server stream at once; each `Run` gets only its own
  envelopes.

### One live run per session

A session has at most one run that is not finished: one running or waiting for
an approval or an answer. Starting another ends the new run with `SessionBusy` (code -32030)
before anything is written; `busy.session_id` and `busy.run_id` name the live
run. Resume, approve, answer or cancel that run first, or queue the prompt. With
Postgres the rule holds across every server on the database.

## Errors arrive at the end of the stream

`run()` returns once the run is stored or refused; `resume()`, `approve()` and
`answer()` return before the server has done anything but accept the call. A refusal (a busy session, an unknown run, a
contract that differs) and a failure to continue durably arrive as the end of
the stream: iterating, or `result()`, raises a `RunError` subclass.

| Exception | Code | When |
|---|---|---|
| `RunNotFound` | -32021 | No such run in the agent's stores (with memory stores: not in this handle) |
| `EventTooLarge` | -32051 | `agent.events` met one envelope too long for a message; `error.events` holds what was read before it and `error.seq` names it ([below](#long-runs-are-read-in-pages)). `EventsIncomplete` is its base: it also ends a read whose page cursor does not advance |
| `RunPruned` | -32050 | The run existed and the server's [retention](./stores.md#retention) deleted it; a `RunNotFound` subclass, `run_id` names it. After the tombstone period it reads as `RunNotFound` |
| `RunExists` | -32023 | `run_id` is already stored; `run_or_resume` handles it ([Workflows](./workflows.md#run_or_resume)) |
| `ContractMismatch` | -32025 | The run started under another contract ([Agent config](./agent.md#the-run-contract)); `data["expected"]` is the run's contract and `data["found"]` this agent's, as the message names them |
| `RunTerminal` | -32026 | The run already finished; `data.status` is its status |
| `NotSuspended` | -32027 | `approve` on a run that is not waiting for approval, or `answer` on one not waiting for an answer ([below](#approve)) |
| `PromptBlocked` | -32029 | A `user_prompt_submit` callback refused the prompt; `.reason` says why. No run or session was created |
| `SessionBusy` | -32030 | The session has a live run |
| `InvalidOutputSchema` | -32031 | The run's `output_schema` does not compile or its root is not an object; `.reason` says why. No run or session was created |
| `InvalidToolChoice` | -32034 | The run's `tool_choice` names a tool the run does not offer, or the provider refuses it; `.reason` says why. No run or session was created |
| `InvalidAnswer` | -32032 | The answers do not fit the pending `AskUser` questions; `.reason` says why. Nothing was written: the run still waits ([below](#answer)) |
| `InvalidSteer` | -32038 | The steer's text is blank or longer than 16 KiB, its id is not usable, or the run is idle; `.reason` says why. Nothing was written |
| `RunLeased` | -32033 | Another server drives the run (Postgres); `.owner` and `.expires_in` (seconds) say whose lease and for how long. Nothing was written ([Stores](./stores.md#run-leases)) |
| `LeaseLost` | -32022 | Another server took the run over, or failed it, while this one drove it; the run stopped here without writing. A write that finds the session longer than this run left it (the new driver's entries) after a takeover reports this too, not `RunExists` |
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
the stream to see it. It returns `False`, not an error, when there is nothing
to cancel: the run already finished. An aborted run is finished: its session
takes a new run. Only the server that drives a running run can cancel it; a
suspended run can be cancelled from anywhere ([below](#cancel-a-suspended-run)).

Cancelling the `run`, `resume` or `approve` call itself (with
`asyncio.wait_for`, say) before it returns raises `CancelledError` as usual.
The run the server may have started for it is cancelled as soon as its answer
arrives, and `agent.close()` waits for that run to end before it frees the
agent.

Read every run you start to its end. A connection holds at most 1024 unread
event batches across its runs; past that the client stops reading, so the
server pauses its runs instead of the client's memory growing. Answers wait
behind those batches too: a run left unread while it still produces events
stalls the other calls on its connection. `cancel()` keeps reading its run
while it waits, so it never waits on itself.

### Cancel a suspended run

A run waiting for an approval or an answer has no stream: the server does
not drive it. `agent.cancel(run_id)`, or `cancel()` on the `Run` whose stream
ended with `suspended`, ends it through the agent's stores instead of
approving, denying or answering it:

```python
import asyncio

from linch_rust import AgentConfig, ResultEvent, RunTerminal, Server, SuspendedEvent, tool
from linch_rust.scripted import MODEL, text_turn, tool_turn


@tool(scope="write")
async def deploy(service: str) -> str:
    """Deploy a service."""
    return "deployed"


config: AgentConfig = {
    "namespace": "runs-demo",
    "spec": {"model": MODEL},
    "provider": {
        "id": "scripted",
        "turns": [tool_turn("c1", "deploy", {"service": "api"}), text_turn("Nothing deployed.")],
    },
}


async def main() -> None:
    async with Server() as server:
        agent = await server.open_agent(config, tools=[deploy])
        run = await agent.run("s1", "Deploy api")
        assert isinstance(await run.result(), SuspendedEvent)

        print("cancelled:", await agent.cancel(run.id))
        events = [e.event for e in await agent.events(run.id)]
        print(events[-3].result["content"][0]["text"], "/", events[-1].subtype)
        try:
            await (await agent.approve(run.id, {})).result()
        except RunTerminal as late:
            print("late approve:", late.data["status"])
        print("cancel again:", await agent.cancel(run.id))

        # The session goes on: the next run sees the call answered.
        result = await (await agent.run("s1", "What happened?")).result()
        assert isinstance(result, ResultEvent)
        print(result.final_text)


asyncio.run(main())
```

```text
cancelled: True
Tool call aborted / aborted
late approve: aborted
cancel again: False
Nothing deployed.
```

- Every call of the response that has no result gets an error result,
  `Tool call aborted` (an `AskUser` question too; a call that started before
  a crash keeps its interrupted placeholder), then the run ends with a
  `result` of subtype `aborted` and status `aborted`. The results go into
  the session, so the model of a later run there sees each call answered.
- A later `approve` or `answer` ends with `RunTerminal`; another cancel
  returns `False`.
- **Postgres**: the cancel can come from another process, or after the
  server that suspended the run restarted. A run another server drives
  right now (an approve that got there first) is not taken from it: the
  cancel returns `False` once that approve committed its decisions, or
  raises `RunLeased` while the approve has only claimed the run.
- A cancel and an approve (or an answer) that race leave one winner: the one
  that claims the run last fences the other out. A cancel that loses
  returns `False` or raises `LeaseLost`; an approve that loses ends with
  `RunTerminal` or `LeaseLost`.
- The cancel is durable from its first write: if the server dies part way,
  `resume` or another `cancel` finishes it, and an `approve` gets
  `NotSuspended`.
- The run's contract is not checked, so a run of an older config can still
  be cancelled.

## Resume

`resume(run_id)` continues a run from its last durable state. Use it after
your process or the server crashed, after the stream dropped, or to pick up a
run another process started. A run waiting for an approval or an answer
suspends again at once. Resume only after the earlier stream ended (its iteration finished or
raised); while a server still drives the run, `resume` is `RunBusy`.

Which handle can resume depends on the stores:

- **Memory stores**: the run lives in the agent handle that started it. Resume
  and approve or answer it through that same `Agent`; it is gone when the server stops.
- **Postgres stores**: any agent opened with the same config and tools (so the
  same contract), on any server, can resume it, but not while another
  server drives it: that resume is refused with `RunLeased`. A server that
  crashed holds its runs until their leases run out (30 s by default, see
  [Run leases](./stores.md#run-leases)); retry after `.expires_in` seconds:

```python
from linch_rust import RunLeased


async def resume_when_free(agent, run_id):
    while True:
        run = await agent.resume(run_id)
        try:
            return await run.result()
        except RunLeased as leased:
            await asyncio.sleep(leased.expires_in or 1)
```

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
Tool execution was interrupted before a result was recorded; its effect is unknown, and it is not re-run automatically on resume.
```

A tool call whose start was recorded but whose end was not is never run again:
the model gets an error result saying it was interrupted, and decides what to
do. Make host tools that change things safe to call twice anyway, or check
before acting, since the crash may have come after the effect and before the
record.

### Tools run at most once, never exactly once

A resumed run gives each tool call one of three fates, by what was durable when
the process died:

| Durable at the crash | What a resume does | Effect of the tool |
|---|---|---|
| Neither start nor end | Plans the call again and runs it (permission is asked again unless a recorded decision covers it) | Happens once, now |
| The start, not the end | Never runs it. It writes the call's `tool_call_end` as an error with `error_code` `tool_interrupted`, and the model reads the error result `Tool execution was interrupted before a result was recorded; its effect is unknown, and it is not re-run automatically on resume.` for that `tool_use_id`, so the transcript stays paired for providers that require a result for every tool use | Happened (the crash came after it, before its end was written) or did not (the crash came before it): unknown |
| Both | Reuses the recorded result | Happened once |

So a tool runs at most once per call, and linch never claims exactly once: the
window between the effect and the write of its end cannot be closed from
outside the tool. A host closes it in its own tools:

- Use `ctx.idempotency_key` (`run_id:tool_use_id`, the `ToolContext` field of
  [Tools](./tools.md#which-call-a-tool-serves)) as the idempotency key of the
  effect. The `tool_use_id` is the provider's and is the same after a resume, so
  a side effect keyed by it is done once however often the call is attempted.
  This holds for host tools you write. A remote execution host gets the same
  key in `exec.start` (`ExecStart.idempotency_key`, see
  [Remote execution](./remote-execution.md#after-a-crash-the-key-lets-the-host-dedupe)),
  and can dedupe by it. The other backends have no host to dedupe on (what each
  guarantees after a crash is on that page too), so a `local`, `sandbox` or
  `docker` command with side effects is at most once with no deduplication:
  make the command itself idempotent, or check its effect before running it
  again.
- Find the calls to reconcile with the `tool_call_end` events whose
  `error_code` is `tool_interrupted`, and ask the system the tool talks to
  whether the key was applied.
- Ask the model to check before it acts again: its error result already says
  the effect is unknown.
- After a connection loss, `resume` the run first (retry `RunLeased` as above);
  re-issue `run` with the same `run_id` only when the run does not exist
  (`RunNotFound` stays), never to repeat a run that does.

```python
import asyncio

from linch_rust import Agent, ToolCallEndEvent, ToolContext, tool

charged: dict[str, str] = {}  # your payment system's idempotency records


@tool(scope="write")
async def charge(order: str, ctx: ToolContext) -> str:
    """Charge an order once, however often the call is attempted."""
    key = ctx.idempotency_key
    if key not in charged:  # the real check is the payment API's own key
        charged[key] = f"receipt-{order}"
    return charged[key]


async def interrupted_calls(agent: Agent, run_id: str) -> list[str]:
    """The tool_use_ids whose effect a crash left unknown, to reconcile."""
    ends = [e.event for e in await agent.events(run_id)]
    return [
        end.tool_use_id
        for end in ends
        if isinstance(end, ToolCallEndEvent) and end.error_code == "tool_interrupted"
    ]
```

`charged.get(f"{run_id}:{tool_use_id}")` then tells whether the effect landed.

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

## Answer

`answer(run_id, answers)` takes an `Answer(...)` or `Answer.declined(note)`
per `request_id` of the `input_request` events (the `suspended` event's
`pending_request_ids`); a request left out is asked again. How to ask and what
the model reads are on [Tools](./tools.md#ask-the-user).

It follows the approve rules: only a run waiting for an answer takes one; the
answer is written as the call's result before the run goes on, so the same
answer sent again after a crash ends with `NotSuspended` and writes nothing,
and `resume` continues the run with the committed answer. An answer on a
finished run is `RunTerminal`. With Postgres stores the answer can come from
another process or a later day.

## Steer

`agent.steer(run_id, text)` (or `run.steer(text)`) sends text to a run while
it works, the way a person would type into the conversation. It returns the
steer's id.

```python
steer_id = await agent.steer(run.id, "Use tabs, not spaces.")
```

- **When the model reads it.** The text is a user message of its own. It is
  logged after the tool results of the batch the run is in and before the next
  model call, so the assistant-to-results order the providers need is never
  broken. When the model answers without calling a tool, a waiting steer is
  logged before the run would end, and the run takes another turn instead. A
  `user` event with `subtype` `alignment` and `steer_id` set to the returned id
  reports the delivery; it is in `agent.events` too.
- **What is accepted.** A run that is running, or suspended for an approval or
  an answer. A suspended run keeps the steer and reads it once you approve or
  answer; it stays suspended. A run that finished is refused with
  `RunTerminal`, an unknown one with `RunNotFound`. An idle run (queued, or
  `running` with no server driving it, after a crash or a dropped stream) is
  refused with `InvalidSteer`: resume it first, then steer.
- **Durable before the answer.** The steer is a `steer_received` event in the
  run's own journal (journal only: `agent.events` never lists it). The call
  returns after it is written. A run the server drives writes it at its next
  step, so the answer waits for the model call or tool batch in progress,
  which can be minutes after a long tool; time out on your side as you would
  on any request. With Postgres stores a crash does not lose an answered
  steer, and a resumed run reads what is still pending back from its journal:
  a steer is logged once, whatever write a crash cut. With memory stores
  nothing survives the process, so neither does a steer.
- **A steer that fails after its claim.** A suspended run is claimed for the
  write and put back. If the write that puts it back fails, the run can show
  `running` although it is still suspended and nobody drives it. It is not
  stuck: `approve`, `answer`, `cancel` and `resume` each still end or
  continue it, and a steer sent again with the same id is taken once.
- **A call sent again.** `steer_id` names the steer (letters, digits and
  `-_.:`, at most 128 bytes). Sent again with the same id after a dropped
  answer, the steer is taken once. A call that fails with the run stopped
  first (`RunExists`, -32023) may or may not have landed; send it again with
  the same id.
- **Other servers.** A run another server drives is not routed to: the call
  gets `RunLeased` (with `expires_in`), as `approve` does, and so does one for
  a run whose crashed server's lease has not run out.
- **Bounds.** At most 16 KiB of text, not blank (`InvalidSteer`, -32038,
  `reason`), and at most 16 steers a run has not read yet, counting
  those still waiting for its next step (`LimitReached`, `kind` `steers`):
  past 16 the call is refused at once, so waiting steers never hold the
  connection's request places and a `cancel` on it is always read. A tenant steers only the runs of its own agents: a run id
  of another namespace is `RunNotFound`.
- **A cancel with steers waiting.** A steer is durable before its call
  answers, so a run cancelled while steers wait keeps them: the waiting steers
  are journaled and still answer with their ids (measured: 16 waiting
  steers, 16 `steer_received` rows, 16 answers), and each appears as an
  `alignment` event in the aborted run. The session's next run sees them as
  part of the conversation, so a steer sent before a cancel is not lost, and
  one you did not mean to keep has to be corrected by the next prompt.
- **Prompt checks.** Prompt redaction and the `user_prompt_submit` callbacks
  see a steer as they see a prompt, with `source` `"steer"` in the callback's
  request. A steer a callback blocks is dropped; its `hook` records are
  streamed.

How it differs from Python Linch's `session.align`: the call answers once the
steer is durable, not once it is logged; the text is never split from a
tool-results message because a steer waits for the whole batch; a steer
arriving while the model answers is not lost to the run; and `images` are not
taken.

## Acting for a principal

`principal` says who a run acts for: `{"tenant", "user", "roles"?}` (the
`Principal` typed dict). Permission rules with `role` or `unless_role` match
its roles ([Tools](./tools.md#permission-rules)), and a `permission_request`
callback's request carries it. Pass it to `run`: the run keeps it, and each
`resume`, `approve` and `answer` that names no principal acts for it, even
after a crash or from another process. One that names a principal acts for
that one instead. On a remote server, `tenant` must be the connection's
tenant ([Server](./server.md#tenants-and-namespaces)).

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
| `run()` returning | The run itself (its create), so the run id you hold exists after any crash |
| Before each model call | The run so far and a record that the call was sent |
| After each model response | The assistant message, its usage and cost |
| After each model response, before its callbacks (only when an `after_provider_call` callback is attached) | The response's usage and cost, without the message |
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
- With an `after_provider_call` callback attached, a crash while a callback
  on the response runs (`provider_call_stop` included) keeps the paid call's usage and cost: the resumed run
  counts them in its `result` totals and its budget, shows them in a `usage`
  event, and calls the model again (a new `model_call_receipt` of the same
  turn, not a retry). The message itself is written only after the
  callbacks, so a callback that redacts it is never bypassed.
- A tool whose start was recorded is never run again on resume: at most once, never exactly once ([above](#tools-run-at-most-once-never-exactly-once)).
- A steer is written at a step boundary, before its call answers; a resumed run reads the pending ones back ([Steer](#steer)).
- Closing the `Server` block, or the server dying, leaves each run at its last
  durable state; a Postgres run is resumable, a memory run is gone.

With memory stores the same points apply, but the stores are inside the server
process. How many writes a turn costs and how they are grouped is in the
[architecture stores page](../architecture/stores.md#group-commit-write-counts).

### Long runs are read in pages

`agent.events` returns the whole run: it always asks for pages (`max_bytes` of 8 MiB) and follows them for you.
On the wire, `run.events` answers one message at a time, and a message holds
at most `limits.max_message_bytes` of `server.info` (16 MiB) in all. What
a request gets:

- **Neither `limit` nor `max_bytes`** (what an older client sends): every
  envelope after `after_seq` in one answer when they fit one message (16 MiB
  less 128 bytes of framing), as before; when they do not, an error,
  `-32052`, and nothing is answered: never a first page with a cursor. A
  client that does not know `next_after_seq` therefore gets the whole run or
  an error, and no events are lost without a sign. `data.seq` is the first
  envelope that did not fit, and the message says to ask again with
  `max_bytes` or `limit`, which pages with a cursor.
- **`limit`, `max_bytes` or both**: at most `limit` envelopes (1000 when
  absent) and `max_bytes` encoded bytes (8 MiB when absent). A value above
  the most (10000 envelopes, 8 MiB) is lowered to it, whatever its size; 0 is
  `-32602`. The first envelope of a page is always returned, so a reader
  advances even when one envelope is longer than `max_bytes`.

When more follow, the answer carries `next_after_seq`, the seq of its last
envelope: ask again with it as `after_seq`. The last page has no
`next_after_seq`. The Python client stops with `EventsIncomplete` if a page's
`next_after_seq` does not advance.

An envelope that cannot fit a message with its framing (16 MiB less about 130
bytes) is refused with `-32051` (`EventTooLarge`, a `RunError`); `data.seq`
names it, and the message says what to do: ask again with `after_seq` set to
that seq to continue after it. `agent.events` raises `EventTooLarge` with
the envelopes it had read in `error.events` and the seq in `error.seq`:

```python
try:
    events = await agent.events(run_id)
except EventTooLarge as error:
    events = error.events                       # everything before the envelope
    events += await agent.events(run_id, after_seq=error.seq)   # and after it
```

The server still loads the run's remaining events from the stores for each
page; the page bounds what is sent and encoded, not that read.

## Related pages

- [Events](./events.md): every event in the stream.
- [Tools](./tools.md#approvals): approval loops and permission rules.
- [Tools](./tools.md#ask-the-user): `AskUser` and its answers.
- [Stores](./stores.md): memory and Postgres.
- [Troubleshooting](./troubleshooting.md#runs): run errors and fixes.
