# Deep agent preset

Use the deep agent preset when you want a model that plans, delegates to
subagents and keeps notes, without wiring each piece yourself.

[← Usage guide](./README.md)

`deep_agent` in the agent config turns an agent into a deep agent: the session
task tools, a roster of subagents, kept and background workers, the virtual
filesystem and file memory, and Python Linch's operating instructions. It is
an ordinary agent with those settings; the loop does not change. A bare
config keeps all of them off.

A deep agent is driven by the model: it decides which subagents to start,
when to run them in the background and when to continue one. The
orchestration is not deterministic, and nothing journals it beyond the
ordinary run and session records.

---

## What it turns on

Opening a config with `deep_agent` turns these on, on top of what the config
already sets:

| Part | What you get |
|---|---|
| [`tasks`](./tasks.md) | `TaskCreate`, `TaskList`, `TaskGet`, `TaskUpdate`, which run without asking |
| [Skills](./skills.md) | The `Skill` tool with the built-in `verify`; the config directory's skills only when `skills` is on |
| [Subagents](./subagents.md) | The `Subagent` tool with the roster below; the config directory's subagents too when `subagents` is on |
| Workers | The profile's: background and kept, so `SubagentContinue` and `TaskStop`; a `workers` value in the config wins |
| Virtual filesystem | `ls`, `read_file`, `write_file`, `edit_file`, with no offload unless the config sets `filesystem` |
| File memory | `/memories/MEMORY.md` shown to the model with every request (see [File memory](#file-memory)) |
| Instructions | The preset's, before `spec.system_prompt`; the profile's `max_turns` unless the spec sets one |

It adds no workspace. Without one the agent has no `Read`, `Write`, `Edit`,
`Grep`, `Glob` or `Bash`, and the roster's definitions name tools the agent
does not have (the child does not get them).

---

## A durable deep agent

The built-in profiles are durable: the agent must have Postgres stores, or
`open_agent` raises `OpenError` (`a durable deep agent needs durable
stores`). This snippet reads the DSN from `LINCH_POSTGRES_DSN`; the client
reads no environment variable itself. A second server process then reads the
note the first one kept.

```python
import asyncio
import os

from linch_rust import AgentConfig, InitEvent, Server, ToolCallEndEvent
from linch_rust.scripted import MODEL, text_turn, tool_turn

dsn = os.environ["LINCH_POSTGRES_DSN"]
memory_index = "- /memories/stack.md: the service runs on Postgres 17\n"


def config(turns: list) -> AgentConfig:
    return {
        "namespace": "research",
        "spec": {"model": MODEL},
        "provider": {"id": "scripted", "turns": turns},
        "stores": {"type": "postgres", "dsn": dsn},
        "permissions": {"mode": "accept_edits", "project_root": "/"},
        "deep_agent": {"profile": "balanced"},
    }


async def show(run) -> None:
    async for envelope in run:
        event = envelope.event
        if isinstance(event, InitEvent):
            print("tools:", sorted(event.tools))
        elif isinstance(event, ToolCallEndEvent):
            print(f"{event.tool_name}: {event.result['content'][0]['text'].strip()}")


async def main() -> None:
    # Monday: plan, and keep a note in file memory.
    async with Server() as server:
        agent = await server.open_agent(config([
            tool_turn("c1", "TaskCreate", {"subject": "Survey the stack", "description": "List services"}),
            tool_turn("c2", "write_file", {"path": "/memories/MEMORY.md", "content": memory_index}),
            text_turn("Noted the stack."),
        ]))
        await show(await agent.run("monday", "Survey the stack and remember it."))
    # Tuesday: a new server process and a new session read the same memory.
    async with Server() as server:
        agent = await server.open_agent(config([
            tool_turn("c1", "read_file", {"path": "/memories/MEMORY.md"}),
            text_turn("The service runs on Postgres 17."),
        ]))
        await show(await agent.run("tuesday", "What database do we run?"))


asyncio.run(main())
```

```text
tools: ['Skill', 'Subagent', 'SubagentContinue', 'TaskCreate', 'TaskGet', 'TaskList', 'TaskStop', 'TaskUpdate', 'edit_file', 'ls', 'read_file', 'write_file']
TaskCreate: Task #1 created: Survey the stack
write_file: Written 1 line to '/memories/MEMORY.md'.
tools: ['Skill', 'Subagent', 'SubagentContinue', 'TaskCreate', 'TaskGet', 'TaskList', 'TaskStop', 'TaskUpdate', 'edit_file', 'ls', 'read_file', 'write_file']
read_file: - /memories/stack.md: the service runs on Postgres 17
```

For a demo without Postgres, set `"deep_agent": {"durable": False}`: memory
stores then work, and everything is gone when the server exits.

### Permissions

The preset sets no permission policy. In `default` mode `write_file` and
`edit_file` ask; `accept_edits`, as above, lets them write. `Subagent`, `SubagentContinue` and
`TaskStop` are exec-scoped and ask in both modes, so a deep agent that should
delegate without stopping for approval needs rules that allow them, or
`skip_dangerous`.

### With a real model

This needs `DEEPSEEK_API_KEY` in the environment and makes paid calls; it was
not run for this page. It works on the repository in the current directory,
without `Bash`, and allows the three orchestration tools.

```python
import asyncio
import os

from linch_rust import AgentConfig, Server

orchestration = ["Subagent", "SubagentContinue", "TaskStop"]
config: AgentConfig = {
    "namespace": "research",
    "spec": {"model": "deepseek-flash", "system_prompt": "You maintain the billing service."},
    "provider": {"id": "deepseek"},
    "stores": {"type": "postgres", "dsn": os.environ["LINCH_POSTGRES_DSN"]},
    "workspace": ".",
    "bash": False,
    "permissions": {
        "mode": "accept_edits",
        "project_root": os.getcwd(),
        "layers": [{"name": "host", "rules": [
            {"type": "tool", "tool": name, "decision": "allow"} for name in orchestration
        ]}],
    },
    "deep_agent": {"profile": "coordinator"},
}


async def main() -> None:
    async with Server() as server:
        agent = await server.open_agent(config)
        run = await agent.run("billing-audit", "Find where invoices are rounded and plan a fix.")
        result = await run.result()
        print(getattr(result, "final_text", result))


asyncio.run(main())
```

`run.result()` returns a `SuspendedEvent` instead when a call still needs
approval; see [Runs](./runs.md).

---

## Profiles

`profile` names a built-in profile or spells one out. `durable` and
`coordinator` override the profile's values.

| Profile | `durable` | `coordinator` | `max_turns` | `max_tokens` | Workers |
|---|---|---|---|---|---|
| `balanced` (the default) | yes | no | 64 | 1,000,000 | kept and background |
| `coordinator` | yes | yes | 64 | 1,000,000 | kept and background |
| `unbounded` | yes | no | none | none | kept and background |

Name `unbounded` only when your host enforces its own cost and time limits.
`max_tokens` is kept so the profiles read as in Python Linch, but nothing
enforces it yet. Another name fails `open_agent` with `unknown deep-agent
profile`.

A written-out profile is a `DeepAgentProfile`; every key is required:

```python
from linch_rust import DeepAgent, DeepAgentProfile

review: DeepAgentProfile = {
    "name": "review",
    "durable": False,
    "coordinator": False,
    "max_turns": 24,
    "max_tokens": None,
    "retain_subagents": True,
    "enable_background_subagents": False,
}
preset: DeepAgent = {"profile": review}
```

A coordinator gets the coordinator instructions: plan, delegate, synthesize,
and leave heavy work to workers. Unlike in Python Linch it keeps the
workspace tools; only its instructions tell it not to use them.

---

## The roster

| Subagent | Tools it may use | Role |
|---|---|---|
| `researcher` | `Read`, `Glob`, `Grep`, `TaskList`, `TaskGet`, `ls`, `read_file` | Read-only exploration and evidence |
| `planner` | `Read`, `Glob`, `Grep`, `TaskList`, `TaskGet`, `ls`, `read_file` | An ordered plan with file paths |
| `implementer` | `Read`, `Edit`, `Write`, `Glob`, `Grep`, `Bash`, `TaskList`, `TaskGet`, `TaskUpdate`, `ls`, `read_file`, `write_file`, `edit_file` | Scoped changes |
| `verification` | `Read`, `Glob`, `Grep`, `TaskList`, `TaskGet`, `ls`, `read_file` | A read-only adversarial check ending in a verdict |

The descriptions and instructions are Python Linch's. This `verification`
replaces the core's built-in one, which has `Bash`. With `subagents: True`, a
file of the same name in `<config_dir>/agents/` replaces a roster entry.

Each worker runs in its own session, but its task tools use the coordinator's
task list (see [Session tasks](./tasks.md#where-tasks-live)). The
`researcher`, `planner` and `verification` calls can run alongside each other
when every tool they list is registered and read-scoped, which needs a
workspace.

---

## File memory

Before every model call the agent adds two text blocks to the end of the
request: a short instruction (memories live under `/memories/`, one file per
fact, with `MEMORY.md` as a one-line-per-file index), and the first 200 lines
of `/memories/MEMORY.md` when it exists and is not empty. The index is read
again for each request, so a change the model makes shows on its next turn.

These blocks are not logged and do not appear in the event stream; the model
maintains the files with the virtual filesystem tools.

Where the files live follows the stores:

| Stores | Files |
|---|---|
| Memory | Per session. A worker has its own session, so it does not see the parent's files |
| Postgres | Shared by every session of the namespace, workers included, and kept across restarts |

---

## What differs from Python Linch

| Python Linch | linch-rust |
|---|---|
| `create_deep_agent(...)` | `deep_agent` in the config |
| A coordinator loses `Edit`, `Write`, `Bash`, `Grep`, `Glob`, `Read` | It keeps them; only its instructions differ |
| `max_tokens` bounds the subagent tree | Not enforced |
| `durable=True` needs explicit stores and a filesystem | It needs Postgres stores; the filesystem is turned on for you |
| `cwd="."` gives a workspace | No workspace unless the config sets one |
| Project skills and subagents load by default | Only with `skills` or `subagents` on |
| `features=` can switch parts off | Every part is on |

On a [remote server](./server.md), a tenant needs `skip_dangerous` among its
`permission_modes` to open a deep agent, because the preset turns on the
task tools, which run without asking.

---

## Related pages

- [Subagents](./subagents.md): background workers, `SubagentContinue`,
  `TaskStop`
- [Session tasks](./tasks.md)
- [Domain profiles](./profiles.md): a role added after the preset's
  instructions
- [Stores](./stores.md): Postgres setup
