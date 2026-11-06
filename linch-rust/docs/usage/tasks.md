# Session tasks

Use session tasks when the model should keep a checklist of its own work that
lasts across the runs of one conversation.

[← Usage guide](./README.md)

`tasks: True` in the agent config gives the model four tools, `TaskCreate`,
`TaskList`, `TaskGet` and `TaskUpdate`. Tasks are rows in the session store,
keyed by namespace and session id: the next run in the same session finds
them, and another session starts with none. Only the tool calls and their
results enter the conversation history.

---

## Turn them on

```python
import asyncio

from linch_rust import AgentConfig, Server, ToolCallEndEvent
from linch_rust.scripted import MODEL, text_turn, tool_turn

# The scripted provider replays these turns: the model plans two tasks,
# links them, finishes the first, and lists what is left.
turns = [
    tool_turn("c1", "TaskCreate", {"subject": "Draft the release notes", "description": "One page"}),
    tool_turn("c2", "TaskCreate", {"subject": "Publish the release", "description": "Tag and push"}),
    tool_turn("c3", "TaskUpdate", {"id": "2", "add_blocked_by": ["1"]}),
    tool_turn("c4", "TaskUpdate", {"id": "1", "status": "completed"}),
    tool_turn("c5", "TaskList", {}),
    tool_turn("c6", "TaskGet", {"id": "2"}),
    text_turn("Release notes are done; publishing is next."),
]

config: AgentConfig = {
    "namespace": "demo",
    "spec": {"model": MODEL},
    "provider": {"id": "scripted", "turns": turns},
    "tasks": True,
}


async def main() -> None:
    async with Server() as server:
        agent = await server.open_agent(config)
        run = await agent.run("release-42", "Plan the release.")
        async for envelope in run:
            event = envelope.event
            if isinstance(event, ToolCallEndEvent):
                text = "".join(part["text"] for part in event.result["content"])
                print(f"{event.tool_name}: {text}")
        result = await run.result()
        print(result.final_text)


asyncio.run(main())
```

```text
TaskCreate: Task #1 created: Draft the release notes
TaskCreate: Task #2 created: Publish the release
TaskUpdate: Task #2 updated: [pending] Publish the release
TaskUpdate: Task #1 updated: [completed] Draft the release notes
TaskList: #1 [completed] Draft the release notes
#2 [pending] Publish the release
TaskGet: #2 [pending] Publish the release
Tag and push
Release notes are done; publishing is next.
```

The config sets no `permissions`, so the mode is `default`, where write tools
ask. The two write tools ran anyway; see [No approval](#no-approval) below.

---

## The four tools

| Tool | Scope | Input | What the model reads |
|---|---|---|---|
| `TaskCreate` | write | `subject`, `description`; optional `active_form`, `metadata` | `Task #1 created: <subject>` |
| `TaskList` | read | none | One `#<id> [<status>] <subject>` line per task, or `No tasks found.` |
| `TaskGet` | read | `id` | That line, then the description; the error `Task not found.` |
| `TaskUpdate` | write | `id`; optional `subject`, `description`, `active_form`, `status`, `owner`, `add_blocks`, `add_blocked_by`, `remove_blocks`, `remove_blocked_by`, `metadata` | `Task #<id> updated: [<status>] <subject>`, or `Task #<id> deleted.` |

- Ids are strings, `"1"`, `"2"`, ..., numbered in order within the session.
- A status is `pending` (a new task), `in_progress` or `completed`.
  `"status": "deleted"` deletes the task instead. Any other value fails the
  input schema, and the model reads why.
- `add_blocked_by` and the other three edge fields record which task waits
  for which. The tools store the edges; neither `TaskList` nor `TaskGet`
  prints them.
- Names, input schemas and result texts are those of Python Linch.

Two write calls in one turn run one after the other; the read calls may run
together.

---

## No approval

The four tools run without asking in every permission mode: opening the
agent adds their names to the policy's `auto_allow`. They touch only the
session's own task rows. A rule that denies or asks for one of them still
applies, since rules are checked before `auto_allow`.

On a [remote server](./server.md), that grant is the reason a tenant needs
`skip_dangerous` among its `permission_modes` to turn `tasks` on.

---

## Where tasks live

| Stores | Tasks |
|---|---|
| Memory (the default) | Inside the opened agent handle; gone when the server exits |
| Postgres | The `linch_tasks` and `linch_task_edges` tables; they survive a restart, and any server on the same DSN and namespace sees them |

A subagent's child runs in a session of its own, so its task tools see the
child's list, which starts empty, not the parent's. With `subagents` on as
well, this run shows it (the scripted provider gives the child the third
turn):

```python
import asyncio
import tempfile

from linch_rust import AgentConfig, Server, ToolCallEndEvent, UnknownEvent
from linch_rust.scripted import MODEL, text_turn, tool_turn

turns = [
    tool_turn("c1", "TaskCreate", {"subject": "Parent task", "description": "x"}),
    tool_turn("c2", "Subagent", {"description": "List", "prompt": "List the tasks"}),
    tool_turn("k1", "TaskList", {}),  # the child's first turn
    text_turn("The child saw no tasks."),  # the child's answer
    text_turn("done"),
]

config: AgentConfig = {
    "namespace": "demo",
    "spec": {"model": MODEL},
    "provider": {"id": "scripted", "turns": turns},
    "permissions": {"mode": "skip_dangerous", "project_root": "/"},
    "tasks": True,
    "subagents": True,
    "config_dir": tempfile.mkdtemp(),  # no project subagents
}


async def main() -> None:
    async with Server() as server:
        agent = await server.open_agent(config)
        async for envelope in await agent.run("s1", "go"):
            event = envelope.event
            if isinstance(event, UnknownEvent) and event.type == "subagent":
                inner = event.raw["event"]
                if inner["type"] == "tool_call_end":
                    print("child:", inner["result"]["content"][0]["text"])
            elif isinstance(event, ToolCallEndEvent):
                print("parent:", event.result["content"][0]["text"])


asyncio.run(main())
```

```text
parent: Task #1 created: Parent task
child: No tasks found.
parent: The child saw no tasks.
```

The same holds for the [deep agent](./deep-agent.md)'s workers.

---

## Related pages

- [Subagents](./subagents.md): child runs and their sessions
- [Deep agent](./deep-agent.md): the preset that turns tasks on
- [Stores](./stores.md): memory and Postgres stores
- Rust: [session task tools](../architecture/tools.md#session-task-tools) and
  the [task store](../architecture/stores.md#task-store)
