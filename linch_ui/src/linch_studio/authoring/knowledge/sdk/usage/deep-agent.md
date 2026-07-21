# Deep agent preset

[← Usage guide](./README.md)

`create_deep_agent()` is a convenience factory that wires up a multi-agent configuration with a single call — task tools, a built-in subagent roster, optional durable stores, and a deepened system prompt. Reach for it when you want a coordinator-and-workers setup without assembling the pieces by hand.

Unlike a [workflow](./workflows.md), a deep agent is LLM-driven: the model itself decides which subagents to spawn, when to run them in the background, and when to continue an existing worker. That makes it more flexible but non-deterministic — there is no journaled replay.

---

## `create_deep_agent()`

```python
from linch import create_deep_agent
from linch.providers import OpenAIChatCompletionsProvider

agent = create_deep_agent(
    model="deepseek-v4-pro",
    provider=OpenAIChatCompletionsProvider(...),  # any provider
    cwd=".",                           # workspace root
    durable=True,                      # SQLite session + run + /memories stores
    permissions={"mode": "skip-dangerous"},
)
session = await agent.session()
```

`durable=True` sets up three persistent stores: `SqliteSessionStore`, `SqliteRunStore`, and a `CompositeFileBackend` with a persistent `/memories` partition (SQLite-backed). Everything else in the virtual filesystem is ephemeral (`StateFileBackend`). With `durable=False` all stores are in-memory.

Durability is what lets a deep-agent run survive a process restart and resume mid-task — the session state and run checkpoint are read back from SQLite. See [Agent configuration](./agent.md) for the underlying store types, and [Virtual filesystem](./filesystem.md) for how the `/memories` partition is routed inside the `CompositeFileBackend`.

---

## Background workers

Spawn a worker in the background by passing `run_in_background=True` to the `Subagent` tool. The turn returns immediately with an ack; a `<task-notification>` is injected at the top of the next turn once the worker finishes.

```python
# Turn 1 — spawn in background (returns immediately with ack)
async for event in session.run(
    "Use Subagent with subagent_type='researcher' and run_in_background=True. "
    "Task: summarise Python asyncio.gather in 2 sentences."
):
    if event.type == "result":
        print(event.final_text)  # "Worker agent-xxxx started in background."

# Wait for worker (optional — turn 2 will receive the notification even without this)
for handle in session.workers.values():
    if handle.task and not handle.task.done():
        await handle.task

# Turn 2 — <task-notification> is drained automatically at the top of this turn
async for event in session.run("Summarise what the background researcher found."):
    if event.type == "result":
        print(event.final_text)
```

The key behaviour: a background spawn does **not** block the turn. The worker runs as a detached task while the coordinator keeps going. When it finishes it appends a `<task-notification>` message to the session, and the next `session.run()` drains that notification at the top of the turn so the coordinator sees the result without you having to poll. Awaiting `handle.task` yourself (as in turn 1.5 above) is optional — it only forces the timing, not the delivery.

---

## Fork/continue

Every `Subagent` result includes a `[Worker ID: agent-xxxx]` suffix so the coordinator can re-engage the same worker with its full context intact using `SubagentContinue`.

```python
# Turn 1 — spawn foreground worker
async for event in session.run(
    "Use Subagent(subagent_type='researcher') to explain asyncio.gather in 1 sentence."
):
    if event.type == "result":
        print(event.final_text)   # includes "[Worker ID: agent-a1b2]"

# Turn 2 — continue the same worker with its full context
async for event in session.run(
    "Use SubagentContinue(to='agent-a1b2', message='Give a one-line code example.')"
):
    if event.type == "result":
        print(event.final_text)
```

Continuing a worker resumes it with its entire prior context, so you can have a multi-turn delegation without re-explaining the task. Workers are retained after their first run specifically to make this possible.

`session.workers` is a `dict[str, WorkerHandle]`. Each handle exposes `handle.child_session_id`, `handle.status`, and `handle.last_result_text`. Inspect these from the host application to track which workers are live, what they last returned, and which child session backs each one.

---

## Generate a project subagent

Use `create_subagent_definition()` to turn a natural-language request into a normal disk-backed project subagent. The SDK writes `.linch/agents/<name>.md`, validates it with the same loader used at runtime, and reloads the `Agent` by default so the new subagent appears in the `Subagent` tool catalog immediately.

```python
from linch import Agent, create_subagent_definition

agent = Agent(
    model="gpt-5",
    permissions={"mode": "skip-dangerous"},
    cwd=".",
)

created = await create_subagent_definition(
    agent,
    "Create a test-runner subagent that runs focused tests after code changes.",
    tools=["Read", "Grep", "Bash"],
)

print(created.file_path)              # .linch/agents/test-runner.md
print(agent.subagent_registry.get("test-runner"))
```

This is the one-call path: generate, validate, write, and reload. For host applications that want more control, call `generate_subagent_definition()` and `write_subagent_definition()` separately — that lets you review or edit the generated definition before it touches disk, or write it through your own storage.

---

## Coordinator mode

```python
agent = create_deep_agent(
    model="...",
    coordinator=True,          # parent orchestrates only
    durable=False,
    permissions={"mode": "skip-dangerous"},
)
# Parent has no Edit/Write/Bash/Grep/Glob/Read — only Subagent/SubagentContinue/TaskStop + task tools
# Workers receive full tool access via SubagentTool → build_child_tools
```

With `coordinator=True` the parent agent is stripped of heavy tools and is left to orchestrate only — it plans and delegates, while the workers it spawns get full tool access. This keeps the coordinator's context focused on task management rather than file contents and command output.

To stop a running background worker: `TaskStop(task_id='agent-xxxx')`. The handle stays in `session.workers` so it can be continued later with `SubagentContinue`. Stopping cancels the in-flight task but does not discard the worker — you can re-engage it once you have what you need.

---

## Related pages

- [Workflows](./workflows.md) — the deterministic, journaled alternative to coordinator mode
- [Agent configuration](./agent.md) — durable stores and run budgets
- [Virtual filesystem](./filesystem.md) — the `/memories` partition and composite backend
- [Examples](./examples.md) — `core/deep_agent_resume.py` runs all four modes end to end
