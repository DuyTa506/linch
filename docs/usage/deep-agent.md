# Deep agent preset

Use `create_deep_agent` when you want a model to coordinate subagents and workspace tools.

[← Usage guide](./README.md)

`create_deep_agent()` is an explicit opt-in factory that wires up a
multi-agent configuration with a single call — workspace/task tools, a
built-in subagent roster, trusted project features, optional durable stores,
and a deepened system prompt. A bare `Agent` remains neutral, tool-free, and
feature-disabled; reach for this preset when you want a coordinator-and-workers
setup without assembling the pieces by hand.

Unlike a [workflow](./workflows.md), a deep agent is LLM-driven: the model
itself decides which subagents to spawn, when to run them in the background,
and when to continue an existing worker. That makes the orchestration
non-deterministic and provides no content-addressed workflow journal. If you
configure durable stores, ordinary session/run checkpoints can still resume a
run; use a workflow when replaying the orchestration itself is the requirement.

---

## `create_deep_agent()`

```python
import os
from linch import CompositeFileBackend, OpenAIChatCompletionsProvider
from linch import PostgresRunStore, StateFileBackend
from linch.ext.deep_agent import create_deep_agent
from linch.filesystem.postgres import PostgresFileBackend
from linch.sessions.postgres import PostgresSessionStore

dsn = os.environ["LINCH_POSTGRES_DSN"]
agent = create_deep_agent(
    model="gpt-5",
    provider=OpenAIChatCompletionsProvider(),  # configure options for your endpoint
    cwd=".",
    durable=True,
    session_store=PostgresSessionStore(dsn),
    run_store=PostgresRunStore(dsn),
    filesystem=CompositeFileBackend(
        default=StateFileBackend(),
        routes={"/memories/": PostgresFileBackend(dsn, namespace="tenant-1")},
    ),
    permissions={"mode": "skip-dangerous"},
)
session = await agent.session()
# At application shutdown:
await agent.close()
```

When `profile` is omitted, `profile="balanced"` sets `durable=True`, bounds
the agent to 64 turns and 1,000,000 tokens, retains workers for continuation,
and enables background subagents. Use `profile="coordinator"` for a parent
that delegates heavy work, or name `profile="unbounded"` only when the host
owns equivalent cost and lifetime controls. When `features` is omitted, the
factory enables skills, subagents, and the virtual filesystem; MCP is enabled
only when `mcp_servers` is configured. An explicit `features=` value is used
as supplied.

`durable=True` requires explicit `session_store` and `run_store` adapters plus
an explicit `filesystem` when that feature is enabled. There are no automatic
persistent stores or implicit environment-based defaults. Missing configuration
raises `ConfigError`; explicit InMemory adapters are accepted for host-controlled
tests but provide only process-lifetime state. Use `durable=False` for an
ephemeral demo. When filesystem features are disabled, no filesystem is required.

For restart durability, use the Postgres stores shown above. The composite
filesystem keeps bulk offloads in process-local `StateFileBackend` storage and
routes `/memories/` to Postgres. Configure the local database with the
[Quickstart](./quickstart.md#5-keep-state-when-the-process-restarts). Session
history and run checkpoints can then resume after restart. A running detached
worker is process-local and is not resumed; on recovery, a worker checkpointed
as `running` is marked `killed` and a recovery notification reports that it did
not finish. Completion notifications use the in-memory queue by default. Pass
`durability=DurabilityOptions(durable_inbox=True)` with a session store that
supports the durable inbox when completion delivery must survive a restart.

See [Agent configuration](./agent.md) and [Virtual filesystem](./filesystem.md)
for the underlying store and routing contracts.

---

> On this page a worker is a subagent. It is not a [`linch.worker`](./production.md#scale-out)
> process, which runs queued jobs.

## Background workers

Spawn a worker in the background by passing `run_in_background=True` to the
`Subagent` tool. The required tool input is a non-empty `description` and
`prompt`; `subagent_type` and `run_in_background` are optional fields:

```json
{
  "description": "research asyncio",
  "prompt": "Summarise Python asyncio.gather in 2 sentences.",
  "subagent_type": "researcher",
  "run_in_background": true
}
```

The `Subagent` tool call returns promptly with an acknowledgement; the model
may continue the current run and choose its final text. After the worker
finishes, its `<task-notification>` is delivered at the next turn boundary:
another provider turn in the current `session.run()` can receive it, or a
later `session.run()` can drain it.

```python
# Turn 1 — request a background worker; the tool call returns promptly
async for event in session.run(
    "Call Subagent with this input: "
    "description='research asyncio', "
    "prompt='Summarise Python asyncio.gather in 2 sentences.', "
    "subagent_type='researcher', run_in_background=True."
):
    if event.type == "tool_call_end" and event.tool_name == "Subagent":
        print(event.result)  # ToolResult text contains the background acknowledgement.

# Wait for the worker (optional; a later run can receive the notification after it finishes)
for handle in session.workers.values():
    if handle.task and not handle.task.done():
        await handle.task

# A later run can receive the notification if the current run ended first.
async for event in session.run("Summarise what the background researcher found."):
    if event.type == "result":
        print(event.final_text)
```

The key behaviour: a background spawn does **not** block the turn. The worker
runs as a detached task while the coordinator keeps going. When it finishes it
calls `Session.notify()` with a `<task-notification>` message. By default that
message enters the session's in-memory pending queue. The next provider-turn
boundary drains that notification so the
coordinator sees the result without polling; that boundary may be another
provider turn in the current `session.run()` or the start of a later
`session.run()`. With a `RunStore`,
Linch also records an origin-attributed `BackgroundWorkerEvent` audit record;
the run store alone does not deliver the result durably. The default
notification queue is in memory, while
`durability=DurabilityOptions(durable_inbox=True)` can persist a completion
after `Session.notify()` reaches a capable session store. No persistence mode
resumes the active task itself; hosts that need durable execution should own a
queue or workflow. Awaiting `handle.task` yourself (as in turn 1.5 above) is
optional — it only forces the timing, not the notification delivery.


Closing a session cancels its unfinished background workers. A `linch.worker` job
closes its session when the run ends, so a background spawn in a job stops at that
point. Use a foreground subagent there, or enqueue the follow-up as its own run.

---

## Fork/continue

With the factory's default profiles, retained foreground `Subagent` results
include a `[Worker ID: agent-xxxx]` suffix so the coordinator can re-engage
the same worker with its full context using `SubagentContinue`. Background
workers are retained by definition.

The continuation tool input is:

```json
{"to": "agent-a1b2", "message": "Give a one-line code example."}
```

```python
# Turn 1 — spawn foreground worker
async for event in session.run(
    "Call Subagent with description='explain asyncio.gather', "
    "prompt='Explain asyncio.gather in 1 sentence.', "
    "subagent_type='researcher'."
):
    if event.type == "tool_call_end" and event.tool_name == "Subagent":
        print(event.result)   # ToolResult text contains the retained Worker ID.

# Turn 2 — continue the same worker with its full context
async for event in session.run(
    "Use SubagentContinue(to='agent-a1b2', message='Give a one-line code example.')"
):
    if event.type == "result":
        print(event.final_text)
```

Continuing a worker resumes it with its entire prior context, so you can have a
multi-turn delegation without re-explaining the task. When retention is
enabled (as in the factory's default profiles), workers remain attached after
their first run so this remains possible. A worker runs one conversation at a
time: while a background run or a continuation drives it, its status is
`running`, and another `SubagentContinue` to it returns a `WorkerBusy` error
without changing the worker.

`session.workers` is a `dict[str, WorkerHandle]`. Each handle exposes
`handle.child_session_id`, `handle.status`, and `handle.last_result_text`.
Statuses are `running`, `completed`, `failed`, or `killed`. Inspect these from
the host application to track which workers are live, what they last returned,
and which child session backs each one.

---

<a id="generate-a-project-subagent"></a>

## `generate_subagent_definition`

Use `create_subagent_definition()` to turn a natural-language request into a
normal disk-backed project subagent. By default, the SDK writes
`<agent.cwd>/.linch/agents/<name>.md` (or the `Agent`'s configured
`config_dir`), validates it with the same loader used at runtime, and reloads
the `Agent` by default so the new subagent appears in the `Subagent` tool
catalog immediately.

```python
from linch import Agent, FeatureFlags, create_subagent_definition, workspace_tools

agent = Agent(
    model="gpt-5",
    tools=workspace_tools(),
    features=FeatureFlags(
        skills=False,
        subagents=True,
        mcp=False,
        filesystem=False,
    ),
    permissions={"mode": "skip-dangerous"},
    cwd=".",
)

try:
    created = await create_subagent_definition(
        agent,
        "Create a test-runner subagent that runs focused tests after code changes.",
        tools=["Read", "Grep", "Bash"],
    )

    print(created.file_path)            # absolute path under cwd/.linch/agents/
    print(agent.subagent_registry.get("test-runner"))
finally:
    await agent.close()
```

This is the one-call path: generate, validate, write, and reload. For host applications that want more control, call `generate_subagent_definition()` and `write_subagent_definition()` separately — that lets you choose the configuration directory, overwrite policy, and reload timing explicitly.

---

## Coordinator mode

```python
agent = create_deep_agent(
    model="...",
    profile="coordinator",
    coordinator=True,          # parent orchestrates only
    durable=False,
    permissions={"mode": "skip-dangerous"},
)
session = await agent.session()
# Parent omits the built-in Edit/Write/Bash/Grep/Glob/Read tools.
# It still has orchestration tools, and may also expose Skill, supplied tools,
# or configured MCP tools. Workers use the configured worker tool registry.
# At application shutdown:
await agent.close()
```

With `coordinator=True` the parent agent omits the built-in heavy workspace
tools and keeps the orchestration surface. After `agent.session()` loads the
optional catalogs, the default profile also enables the `Skill` tool; caller-
supplied tools and configured MCP servers can add more tools. Workers use the
configured worker registry, which keeps the coordinator's context focused on
task management rather than file contents and command output.

To stop a running background worker, call `TaskStop` with
`{"task_id": "agent-xxxx"}` (and optionally `reason`). The handle stays in
`session.workers` so it can be continued later with `SubagentContinue`.
Stopping cancels the in-flight task but does not discard the worker — you can
re-engage it once you have what you need.

---

## Related pages

- [Workflows](./workflows.md) — the deterministic, journaled alternative to coordinator mode
- [Agent configuration](./agent.md) — durable stores and run budgets
- [Virtual filesystem](./filesystem.md) — the `/memories` partition and composite backend
