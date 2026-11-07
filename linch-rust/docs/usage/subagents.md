# Subagents

Use subagents when the model should hand a focused task to a child run and
use its answer.

[← Usage guide](./README.md)

`subagents: True` gives the model the `Subagent` tool. Each call starts a
child run on the same provider, model and stores, recorded with the parent's
run id as its `parent_run_id`; the child's final text is the call's result.
`workers` keeps children around: in the background, or for a follow-up with
`SubagentContinue`. `mailbox` lets sessions send each other messages.

Every snippet here uses the `scripted` provider, which answers every model
call in order, the children's included. That is why each turn list mixes the
parent's turns with the child's.

---

## Define subagents

A project subagent is a markdown file, `<config_dir>/agents/<name>.md`, with
the same `config_dir` rules as [skills](./skills.md#turn-skills-on)
(`.linch`, relative to the workspace or the server's working directory).

```markdown
---
name: reviewer
description: Reviews a change and lists concrete problems.
tools: [Read, Grep, Glob]
---
You are a code reviewer. Read the files you are pointed at and list each
problem with its file and line. Do not edit anything.
```

| Field | Required | Meaning |
|---|---|---|
| `description` | yes | Listed in the `Subagent` tool's description |
| `name` | no | What the model passes as `subagent_type`; the file name when absent |
| `tools` | no | The tools the child may use, as a list or a comma-separated string; `Read(...)` counts as `Read`; absent or `*` means all of the parent's |

The body is the child's instructions. Two definitions come built in:
`verification`, which tries to break completed work with `Read`, `Glob`,
`Grep` and `Bash` (those the parent has) and ends with a `VERDICT:` line, and
a general-purpose agent with all of the parent's tools, used when the call
names no `subagent_type`. A file named `verification` (in any case) replaces
the built-in one. A file that does not parse, a symlink, or a second file with
the same name is skipped; `Agent.skipped` lists each one with its reason
([Agent config](./agent.md#skipped-definitions)).

What a child gets:

| | The child run |
|---|---|
| Session | Its own, named by its worker id, and empty at the start |
| System prompt | The parent's, then the skill listing if it has `Skill`, then `User-provided instructions:` and the body |
| Tools | The parent's, without `Subagent`, `SubagentContinue` and `TaskStop`; only those in `tools` when it lists any |
| Provider, model, stores, hooks | The parent's |
| Approval | It cannot suspend: a call that needs approval is denied, unless a permission rule or a `permission_request` callback allows it |

---

## Run a subagent

Run this from a project directory holding the `reviewer` file above in
`.linch/agents/`. The workspace gives the reviewer its read tools. The second
run continues the same reviewer (see [Keep and continue](#keep-and-continue-workers)).

```python
import asyncio
import os

from linch_rust import AgentConfig, InitEvent, Server, SubagentEvent, ToolCallEndEvent
from linch_rust.scripted import MODEL, text_turn, tool_turn

# Every model call takes the next turn, the child's included.
turns = [
    # Run 1: the parent delegates, the reviewer answers, the parent sums up.
    tool_turn("c1", "Subagent", {
        "description": "Review the parser",
        "prompt": "Review src/parser.py for error handling.",
        "subagent_type": "reviewer",
    }),
    text_turn("parser.py:12 swallows ValueError."),
    text_turn("The reviewer found one problem."),
    # Run 2: the parent asks the same reviewer for a fix.
    tool_turn("c1", "SubagentContinue", {"to": "review-1:c1", "message": "Suggest a fix."}),
    text_turn("Re-raise it as ParseError."),
    text_turn("The fix is to re-raise it as ParseError."),
]

config: AgentConfig = {
    "namespace": "demo",
    "spec": {"model": MODEL},
    "provider": {"id": "scripted", "turns": turns},
    "workspace": ".",
    "bash": False,
    "permissions": {"mode": "skip_dangerous", "project_root": os.getcwd()},
    "subagents": True,
    "workers": {"retain": True},
}


async def show(run) -> None:
    async for envelope in run:
        event = envelope.event
        if isinstance(event, InitEvent):
            print("parent tools:", event.tools)
        elif isinstance(event, SubagentEvent) and isinstance(event.event, InitEvent):
            print("child run:", event.event.run_id, event.event.tools)
        elif isinstance(event, ToolCallEndEvent):
            print("tool result:", event.result["content"][0]["text"])


async def main() -> None:
    async with Server() as server:
        agent = await server.open_agent(config)
        await show(await agent.run("s1", "Get the parser reviewed.", run_id="review-1"))
        await show(await agent.run("s1", "How do we fix it?", run_id="review-2"))


asyncio.run(main())
```

```text
parent tools: ['Read', 'Write', 'Edit', 'Grep', 'Glob', 'Subagent', 'SubagentContinue']
child run: review-1:c1 ['Read', 'Grep', 'Glob']
tool result: parser.py:12 swallows ValueError.

[Worker ID: review-1:c1]
parent tools: ['Read', 'Write', 'Edit', 'Grep', 'Glob', 'Subagent', 'SubagentContinue']
child run: review-2:c1 ['Read', 'Grep', 'Glob']
tool result: Re-raise it as ParseError.
```

- The `Subagent` input is `description` and `prompt` (both required),
  `subagent_type` and `run_in_background`. An unknown type fails with the list
  of known ones.
- The tool is exec-scoped, so in `default` mode the call asks first; approve
  it as in [Runs](./runs.md). It runs alongside other read calls only when its
  type lists its tools and each one is a registered read-scoped tool.
- Each event of the child reaches the parent's stream wrapped in a
  `SubagentEvent` before the call ends: `child_run_id`, `tool_use_id` (the
  parent's call), `ts_ms` (the child's own time) and `event`, the child's
  event decoded into its own class. The durable ones are replayed by
  `Agent.events` on the parent run.
- The child's run is an ordinary run in the same stores:
  `agent.events("review-1:c1")` reads its own journal, and its `init` event
  names the parent in `parent_run_id`.
- The parent's `ResultEvent.total_usage` counts the parent's model calls only.
  Add the wrapped `usage` events for the whole tree.

---

## Background workers

With `workers: {"background": True}` a call may pass `run_in_background:
True`. It returns at once with an acknowledgement, and the child goes on
beside the parent. The agent also gets `SubagentContinue` and `TaskStop`.

```python
import asyncio
import tempfile

from linch_rust import AgentConfig, ChildRunStatusEvent, Server, ToolCallEndEvent, UserEvent
from linch_rust.scripted import MODEL, text_turn, tool_turn

turns = [
    tool_turn("c1", "Subagent", {
        "description": "Find the flaky test",
        "prompt": "Run the suite three times and name the flaky test.",
        "run_in_background": True,
    }),
    text_turn("test_upload fails one run in three."),  # the worker
    text_turn("The worker says test_upload is flaky."),  # the parent
]

config: AgentConfig = {
    "namespace": "demo",
    "spec": {"model": MODEL},
    "provider": {"id": "scripted", "turns": turns},
    "permissions": {"mode": "skip_dangerous", "project_root": "/"},
    "subagents": True,
    "workers": {"background": True},
    "config_dir": tempfile.mkdtemp(),  # no project subagents
}


async def main() -> None:
    async with Server() as server:
        agent = await server.open_agent(config)
        run = await agent.run("s1", "Find the flaky test.", run_id="ci-1")
        async for envelope in run:
            event = envelope.event
            if isinstance(event, ToolCallEndEvent):
                print("ack:", event.result["content"][0]["text"])
            elif isinstance(event, ChildRunStatusEvent):
                print("status:", event.status)
            elif isinstance(event, UserEvent) and event.subtype != "prompt":
                for block in event.message["content"]:
                    if block["type"] == "text":
                        print("model reads:", block["text"])
        print("final:", (await run.result()).final_text)
        replayed = {e.event.type for e in await agent.events(run.id)}
        print("replayed:", sorted(replayed))


asyncio.run(main())
```

```text
status: running
ack: Worker 'ci-1:c1' started in background. You will receive a <task-notification> when it finishes.
status: completed
model reads: <task-notification><task-id>ci-1:c1</task-id><status>completed</status><summary>Worker 'Agent #1' finished.</summary><result>test_upload fails one run in three.</result></task-notification>
final: The worker says test_upload is flaky.
replayed: ['assistant', 'child_run_status', 'context_build', 'init', 'result', 'subagent', 'tool_call_end', 'tool_call_start', 'usage', 'user']
```

- A `ChildRunStatusEvent` (`running`, then `completed`, `failed` or
  `aborted`) marks the worker in the parent's log.
- The worker's end becomes a `<task-notification>` (Python Linch's text),
  added to the next tool results the parent logs, or logged as a user message.
- When the parent answers with text while a worker still runs, the run does
  not end: it waits for the worker and gives the model another turn with the
  notification. A worker `TaskStop` stopped has no notification, so the wait
  goes on for the others. A parent that ends anyway (cancelled, failed, out of
  turns) cancels its workers, waits for them, and marks each one's end with a
  `ChildRunStatusEvent` in its last write.
- The parent's stream drives its workers: keep reading the run to its end, or
  call `Run.result()`.
- A parent that suspends for approval keeps its workers running while it
  waits, as Python Linch does: each writes its own log, and the approve (or a
  resume) in the same server takes them over and hears of their ends. A
  worker inside a model call or a tool at the suspend is not called or run
  again.
- Closing the parent's session (`Agent.close_session`) or the agent while the
  parent waits cancels its workers: each ends `aborted`, and a later approve
  or resume of the parent hears that.
- One run has at most `workers.max_background` workers running at once (4
  unless set). A `run_in_background` call past it gets a tool error the model
  reads, and starts nothing; a worker that ends frees its place. Python Linch
  has no such cap.
- The worker's durable events reach the parent's log wrapped, at the parent's
  next turn; each keeps the child's own `ts_ms` and the spawning call's
  `tool_use_id`.

---

## Keep and continue workers

A background worker is always kept. With `workers: {"retain": True}` a
foreground one is kept too: its result ends with `[Worker ID: <id>]`, and the
agent gets `SubagentContinue`. A kept worker is addressed by its id or by its
display name when only one worker has it.

| | Value |
|---|---|
| Worker id | `<parent run id>:<tool_use_id>`; pass `run_id=` to `Agent.run` to know it in advance. A later call with a `tool_use_id` the run already used (a provider that repeats ids) gets a tool error and starts nothing |
| Display name | The definition's name, or `Agent #N` for the N-th general-purpose worker of the session |
| `SubagentContinue` input | `{"to": <id or display name>, "message": ...}` |

`SubagentContinue` runs the worker again on its own session, its history
intact, as a new foreground child run of the calling run (`review-2:c1`
above). It is refused while that worker still runs in the background. Workers
are kept per parent session; `Agent.close_session` closes their sessions with
the parent's.

---

## Stop a background worker

`TaskStop` takes `{"task_id": <id or display name>, "reason": ...}`. The
worker's run ends `aborted`, with no notification, and it stays addressable
for `SubagentContinue`.

```python
import asyncio
import tempfile

from linch_rust import AgentConfig, ChildRunStatusEvent, Server, ToolCallEndEvent, tool
from linch_rust.scripted import MODEL, text_turn, tool_turn


@tool(scope="read")
async def wait_for_ci(build: str) -> str:
    """Wait until a CI build finishes."""
    await asyncio.Event().wait()  # never finishes in this example
    return "green"


turns = [
    tool_turn("c1", "Subagent", {
        "description": "Watch CI",
        "prompt": "Wait for build 812 and report.",
        "run_in_background": True,
    }),
    tool_turn("w1", "wait_for_ci", {"build": "812"}),  # the worker
    tool_turn("c2", "TaskStop", {"task_id": "Agent #1", "reason": "build cancelled"}),
    text_turn("Stopped the CI watcher."),
]

config: AgentConfig = {
    "namespace": "demo",
    "spec": {"model": MODEL},
    "provider": {"id": "scripted", "turns": turns},
    "permissions": {"mode": "skip_dangerous", "project_root": "/"},
    "subagents": True,
    "workers": {"background": True},
    "config_dir": tempfile.mkdtemp(),
}


async def main() -> None:
    async with Server() as server:
        agent = await server.open_agent(config, tools=[wait_for_ci])
        run = await agent.run("s1", "Watch build 812.", run_id="ci-2")
        async for envelope in run:
            event = envelope.event
            if isinstance(event, ToolCallEndEvent):
                print(event.tool_name + ":", event.result["content"][0]["text"])
            elif isinstance(event, ChildRunStatusEvent):
                print("status:", event.status)
        print("final:", (await run.result()).final_text)


asyncio.run(main())
```

```text
status: running
Subagent: Worker 'ci-2:c1' started in background. You will receive a <task-notification> when it finishes.
TaskStop: Worker 'ci-2:c1' (Agent #1) stopped. Reason: build cancelled. Use SubagentContinue to re-engage if needed.
status: aborted
final: Stopped the CI watcher.
```

---

## The mailbox

`mailbox: True` gives the agent a mailbox and the `send_message` tool, for
messages between its sessions. An address is a session id. With memory stores
the mailbox is in process; with Postgres stores it is the `linch_mailbox`
table, keyed by namespace and session id, so a message waits there across a
server restart.

```python
import asyncio

from linch_rust import AgentConfig, Server, ToolCallEndEvent, UserEvent
from linch_rust.scripted import MODEL, text_turn, tool_turn

turns = [
    # The lead's run: it messages alice, then ends.
    tool_turn("c1", "send_message", {
        "to": "alice",
        "content": "Please own the migration.",
        "request_id": "req-1",
    }),
    text_turn("Asked alice."),
    # Alice's run: her prompt carries the message; she replies.
    tool_turn("c1", "send_message", {"to": "lead", "content": "Yes.", "in_reply_to": "req-1"}),
    text_turn("Replied."),
]

config: AgentConfig = {
    "namespace": "demo",
    "spec": {"model": MODEL},
    "provider": {"id": "scripted", "turns": turns},
    "permissions": {"mode": "skip_dangerous", "project_root": "/"},
    "mailbox": True,
}


async def main() -> None:
    async with Server() as server:
        agent = await server.open_agent(config)
        async for envelope in await agent.run("lead", "Find an owner for the migration."):
            if isinstance(envelope.event, ToolCallEndEvent):
                print("lead:", envelope.event.result["content"][0]["text"])
        async for envelope in await agent.run("alice", "Check your messages."):
            event = envelope.event
            if isinstance(event, UserEvent) and event.subtype == "prompt":
                for block in event.message["content"]:
                    print("alice reads:", block["text"])


asyncio.run(main())
```

```text
lead: Message delivered to 'alice'.
alice reads: Check your messages.
alice reads: <peer-message><from>lead</from><type>message</type><request-id>req-1</request-id><content>Please own the migration.</content></peer-message>
```

- `send_message` takes `to` and `content`, and optional `type` (default
  `message`), `request_id` and `in_reply_to`. The sender is the calling
  session. It is exec-scoped.
- A run drains its session's inbox at its start and after each tool batch.
  Each message joins the logged prompt or tool-results message as a
  `<peer-message>` block, so the model sees it in its next request.
- A drain leases the messages it takes. The run deletes them once the event
  that carries them is durable, and gives them back when it ends first (its
  prompt is refused, or it fails). On Postgres, a lease from a server that
  stopped in between runs out after 60 seconds and the next run takes the
  message again: delivery is at least once.
- A worker's session id is its worker id, so a message to `review-1:c1`
  reaches that worker at its next run, such as a `SubagentContinue`. A display
  name is not an address.
- What a message means is up to your prompts; the kernel only delivers it.
  Python Linch's `Correlator` is a Rust API here
  (`linch_core::coordination::Correlator`); from Python, the model matches
  `request_id` and `in_reply_to` itself.

---

## Fork a session

Forking is not in the Python client yet. Rust hosts call
`Agent::fork_session(source, dest, before)`
(`crates/linch-core/src/agent/fork.rs`), which copies the first `before`
entries of a session's log (all of them when `None`) into a new session, and
refuses a cut that leaves tool calls without their results.

---

## What is durable

| | Memory stores | Postgres stores |
|---|---|---|
| A child run and its journal | In the agent handle | Durable, like any run |
| Wrapped child events in the parent's log | Yes, the durable kinds | Yes |
| A foreground call cut by a crash | Lost with the server | Resuming the parent runs the call again, which resumes the child run; a model call it already paid is not made twice |
| A background worker | Lost with the server | Resuming the parent resumes each worker its log marks `running`, or reads back one that finished |
| Worker ids and display names | Process memory | Process memory; a resumed run knows its own workers again from its log |
| Mailbox messages | Process memory | Durable in `linch_mailbox` until a run logs them; a message drained just before a crash comes again once its lease runs out |

A parent that suspends for approval keeps its workers running in the server
until its approve or resume takes them over there. Only a parent resumed after
a crash, or in another server, picks its workers up again from their logs.

A child run's contract is its parent agent's, as the agent was opened, plus
the child's definition: a tool registered since does not change it. A child
whose definition changed since it started (a host that reloaded the subagents
before resuming the parent) cannot continue: resuming the parent ends it
`failed`, which frees its session, and the parent gets that error.

---

## What differs from Python Linch

| Python Linch | linch-rust |
|---|---|
| Worker ids look like `agent-a1b2` | `<parent run id>:<tool_use_id>`, as the tool descriptions say |
| A background worker can outlive the run and notify a later one | The run waits for its workers before it ends |
| A running worker is marked `killed` after a restart | It is resumed with its parent run (Postgres) |
| `Correlator`, `MailboxPlugin`, durable inbox | Every drain is leased and acked once logged; the mailbox is durable with Postgres stores; `Correlator` in Rust only |
| `generate_subagent_definition()` | Not in the client; write the file yourself |
| `session.workers` handles | Not exposed; follow `ChildRunStatusEvent` and `SubagentEvent` |
| `agent.fork_session(...)` | Rust only, `Agent::fork_session` |
| Reloading subagents | Not yet; open a new agent |

---

## Related pages

- [Skills](./skills.md): prompt workflows the model loads itself
- [Session tasks](./tasks.md): a checklist per session, not shared with
  workers
- [Deep agent](./deep-agent.md): a preset with a roster of subagents
- [Runs](./runs.md): approve, resume and cancel
- [Events](./events.md): `SubagentEvent`, `ChildRunStatusEvent` and replay
