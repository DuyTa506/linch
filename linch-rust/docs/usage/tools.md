# Tools

Use this page to give an agent tools: Python functions in your process, the
built-in workspace tools, the virtual filesystem and MCP servers, and to decide
which calls run, ask or are denied.

[<- Usage guide](./README.md)

| Tools | Where they run | Turned on by |
|---|---|---|
| Host tools | In your Python process, called back by the server | `open_agent(config, tools=[...])` |
| Workspace tools: `Read`, `Write`, `Edit`, `Grep`, `Glob`, `Bash` | In the server, over one directory | `workspace` (and `bash`) |
| Virtual filesystem: `ls`, `read_file`, `write_file`, `edit_file` | In the server, over text files kept in the stores | `filesystem` |
| MCP tools: `mcp__<server>__<tool>` | In the MCP server | `mcp_servers` |
| Session tasks: `TaskCreate`, `TaskList`, `TaskGet`, `TaskUpdate` | In the server | `tasks` ([Tasks](./tasks.md)) |
| `Skill`, `Subagent`, `SubagentContinue`, `TaskStop`, `send_message` | In the server | `skills`, `subagents`, `workers`, `mailbox` ([Skills](./skills.md), [Subagents](./subagents.md)) |

The `init` event of each run lists the tool names the model is offered, in
order: host tools first, then the others.

---

## Host tools

A host tool is an async function decorated with `@tool`. The server validates
the model's input against the tool's schema, then calls the function in your
process with that input as keyword arguments.

```python
from typing import Any

from linch_rust import tool


@tool(scope="read")
async def search_orders(customer: str, limit: int = 10, status: str | None = None) -> list[str]:
    """Search a customer's orders, newest first."""
    return []


@tool(
    scope="exec",
    name="run_report",
    description="Run a named report.",
    input_schema={
        "type": "object",
        "properties": {"report": {"type": "string", "enum": ["daily", "weekly"]}},
        "required": ["report"],
        "additionalProperties": False,
    },
    timeout=30,
)
async def report(**input: Any) -> dict[str, Any]:
    return {"report": input["report"], "rows": 0}


print(search_orders.spec())
print(report.spec())
```

```text
{'name': 'search_orders', 'description': "Search a customer's orders, newest first.", 'input_schema': {'type': 'object', 'properties': {'customer': {'type': 'string'}, 'limit': {'type': 'integer'}, 'status': {'type': 'string'}}, 'additionalProperties': False, 'required': ['customer']}, 'scope': 'read'}
{'name': 'run_report', 'description': 'Run a named report.', 'input_schema': {'type': 'object', 'properties': {'report': {'type': 'string', 'enum': ['daily', 'weekly']}}, 'required': ['report'], 'additionalProperties': False}, 'scope': 'exec', 'timeout_ms': 30000}
```

`@tool` returns a `HostTool`; `spec()` is what the server receives and
`fn` is your function, so a unit test can `await search_orders.fn(customer="ada")`.

| Argument | Default | What it controls |
|---|---|---|
| `scope` | required, no default | `read`, `write` or `exec`. It decides approval and parallelism ([Scopes](#scopes)) |
| `name` | the function's name | The tool name: 1 to 64 ASCII letters, digits, `_` or `-`, unique in the agent |
| `description` | the docstring | What the model is told |
| `input_schema` | built from the type hints | A JSON Schema object for the input |
| `timeout` | `300` seconds | How long the server waits for an answer |

`@tool` raises `ValueError` for an unknown scope or a timeout that is not
positive, and `TypeError` for a function that is not `async` or a parameter it
cannot type.

### Schemas from type hints

| Hint | JSON type |
|---|---|
| `str` | `string` |
| `int` | `integer` |
| `float` | `number` |
| `bool` | `boolean` |
| `list`, `list[...]` | `array` (the item type is not kept) |
| `dict`, `dict[...]` | `object` |
| `X \| None`, `Optional[X]` | `X`, not required |

A parameter without a default and not optional is required, and the schema
sets `additionalProperties: false`. Any other hint, a parameter with no hint,
or `*args` raises `TypeError`; pass `input_schema` instead, with a function
that takes `**input` as above. The server compiles every schema at open and
refuses an invalid one, a duplicate name or a bad name with a
`ProtocolError` (code -32602).

### What the model reads

- The return value can be any JSON value. A string reaches the model as is;
  anything else as compact JSON (`{"q":"x"}`).
- An exception is a tool error the model reads, as
  `"<ExceptionType>: <message>"`, so it can correct its input or explain.
  The run goes on.
- At the timeout, the server stops waiting, the model gets a timeout error,
  and the function's task is cancelled (`asyncio.CancelledError`).
- Cancelling the run (`run.cancel()`) cancels the task of a call in flight.

A host tool cannot return metadata for the host only, and the
`ToolCallEndEvent` this client decodes has no `metadata` field. Keep host-side
details in your own code.

## Scopes

| Scope | Mode `default` | Mode `accept_edits` | Mode `skip_dangerous` | Runs in parallel |
|---|---|---|---|---|
| `read` | runs | runs | runs | yes, with neighbouring read calls, up to `spec.max_tool_concurrency` (4) |
| `write` | asks | runs inside the project root, asks outside | runs | no |
| `exec` | asks | asks | runs | no |

A tool's scope is part of the [run contract](./agent.md#the-run-contract).
Give `read` only to a tool that changes nothing: it runs without asking and
next to other calls.

## Workspace tools

`workspace` names an existing directory. The agent then has these tools over
it (`bash: False` leaves out `Bash`):

| Tool | Scope | What it does |
|---|---|---|
| `Read` | read | UTF-8 text with numbered lines; `offset` and `limit`, 2000 lines by default; files over 10 MiB are refused |
| `Write` | write | Replaces a whole file atomically, creating parent directories |
| `Edit` | write | Replaces exact text, once unless `replace_all` |
| `Grep` | read | ripgrep's engine; modes `files_with_matches`, `count`, `content` |
| `Glob` | read | ripgrep glob rules, newest first, at most 1000 paths |
| `Bash` | exec | `/bin/sh -c` in the workspace root |

```python
import asyncio
import tempfile
from pathlib import Path

from linch_rust import AgentConfig, InitEvent, Server, ToolCallEndEvent
from linch_rust.scripted import MODEL, text_turn, tool_turn

root = Path(tempfile.mkdtemp())
(root / "notes.txt").write_text("status: draft\n")

config: AgentConfig = {
    "namespace": "workspace-demo",
    "spec": {"model": MODEL},
    "provider": {
        "id": "scripted",
        "turns": [
            tool_turn("c1", "Edit", {"file_path": "notes.txt", "old_string": "draft", "new_string": "final"}),
            tool_turn("c2", "Read", {"file_path": "notes.txt"}),
            tool_turn("c3", "Edit", {"file_path": "notes.txt", "old_string": "draft", "new_string": "final"}),
            text_turn("Updated."),
        ],
    },
    "workspace": str(root),
    "bash": False,
    "permissions": {"mode": "accept_edits", "project_root": str(root)},
}


async def main() -> None:
    async with Server() as server:
        agent = await server.open_agent(config)
        async for envelope in await agent.run("s1", "Mark the notes final"):
            event = envelope.event
            if isinstance(event, InitEvent):
                print("tools:", event.tools)
            elif isinstance(event, ToolCallEndEvent):
                text = event.result["content"][0]["text"]
                print(event.tool_name, "error" if event.is_error else "ok", "|", text.splitlines()[0])
    print((root / "notes.txt").read_text(), end="")


asyncio.run(main())
```

```text
tools: ['Read', 'Write', 'Edit', 'Grep', 'Glob']
Edit error | Read notes.txt before changing it.
Read ok | 1	status: draft
Edit ok | Edited notes.txt
status: final
```

- **Read before write.** `Write` over an existing file and `Edit` need a
  `Read` of that file in the same session, at its current modification time
  and size; a change by anyone voids it. The record is in the server's memory:
  after a restart, or after `agent.close_session(session_id)`, the model must
  read again before it writes.
- **Confinement.** Paths are relative to the workspace. `..`, absolute paths
  outside it and symlinks that lead out are refused.
- **`Bash` is not confined.** It starts in the workspace but can reach
  anything the server's user can. It asks in modes `default` and
  `accept_edits`; only nine variables of the server's environment (`PATH`,
  `HOME`, `TERM`, `LANG`, `LC_ALL`, `LC_CTYPE`, `USER`, `SHELL`, `TMPDIR`)
  reach the command, so provider keys do not. Its timeout is 120 s by default.

Limits, the process-group kill and the output caps are in the
[architecture tools page](../architecture/tools.md#workspace-tools).

## Permissions

Each call is decided before it runs: allow, ask or deny. A denied call never
runs; the model gets an error naming the rule. A call that asks suspends the
run until you [approve](#approvals) it.

| Mode | When no rule matches |
|---|---|
| `default` | `read` runs; `write` and `exec` ask |
| `accept_edits` | `read` runs; `write` runs when its path is inside the project root, else asks; `exec` asks |
| `skip_dangerous` | Every call runs |

Without `permissions`, the mode is `default` and the project root is the
workspace (or `/`). With `permissions`, `project_root` is required and must be
absolute. The spellings are `accept_edits` and `skip_dangerous`, not Python
Linch's `acceptEdits` and `skip-dangerous`; another spelling is a
`ProtocolError` at open.

### Permission rules

Rules live in named layers, from least to most specific:

```python
import asyncio
import tempfile

from linch_rust import AgentConfig, Server, SuspendedEvent, ToolCallEndEvent, tool
from linch_rust.scripted import MODEL, text_turn, tool_turn

root = tempfile.mkdtemp()


@tool(scope="write")
async def save(text: str) -> str:
    """Save a note."""
    return "saved"


config: AgentConfig = {
    "namespace": "rules-demo",
    "spec": {"model": MODEL},
    "provider": {
        "id": "scripted",
        "turns": [
            tool_turn("c1", "save", {"text": "x"}),
            tool_turn("c2", "Bash", {"command": "rm -rf build"}),
            tool_turn("c3", "Write", {"file_path": "secrets/key.txt", "content": "x"}),
            tool_turn("c4", "Write", {"file_path": "notes.txt", "content": "x"}),
            text_turn("Done."),
        ],
    },
    "workspace": root,
    "permissions": {
        "mode": "default",
        "project_root": root,
        "layers": [
            {
                "name": "app",
                "rules": [
                    {"type": "tool", "tool": "save", "decision": "allow"},
                    {"type": "bash", "patterns": ["rm *"], "decision": "deny"},
                    {"type": "path", "paths": ["secrets/**"], "decision": "deny"},
                ],
            }
        ],
    },
}


async def main() -> None:
    async with Server() as server:
        agent = await server.open_agent(config, tools=[save])
        async for envelope in await agent.run("s1", "go"):
            event = envelope.event
            if isinstance(event, ToolCallEndEvent):
                print(event.tool_name, "|", event.result["content"][0]["text"])
            elif isinstance(event, SuspendedEvent):
                print("asks:", event.pending_request_ids)


asyncio.run(main())
```

```text
save | saved
Bash | Tool call denied: Bash denied by bash rule in layer "app"
Write | Tool call denied: Write denied by path rule in layer "app"
asks: ['c4']
```

| Rule `type` | Fields | Matches |
|---|---|---|
| `tool` | `tool`, `decision`, `skill?` | A tool name, `*` for every tool, or `mcp__<server>__*`; `skill` narrows a `Skill` rule to one skill |
| `path` | `paths`, `decision`, `tools?` | Globs relative to the project root (absolute when they start with `/`); `**` spans directories, a leading `!` inverts. Without `tools`, every write-scope tool with a path field |
| `bash` | `patterns`, `decision` | Each command segment, as a glob or by its leading words (`npm install` matches `npm install lodash`) |

`decision` is `allow`, `deny` or `ask`. Every rule also takes `role` and
`unless_role`; this client starts runs without a principal, so a rule with
`role` never matches and one with `unless_role` always applies.

How rules combine:

- In one layer the first matching rule wins (bash rules are pooled: any
  segment denied denies, any asked asks).
- Across layers a deny from any layer is final; otherwise the last layer that
  decided wins. When no layer decides, the mode decides.
- A shell command the splitter cannot model (for example with `$(...)`) can be
  denied or asked by rules but never allowed by one.
- A path outside the project root never matches an allow rule.

`auto_allow` (a list of tool names) allows those tools in every mode unless a
rule says otherwise. `tasks` and a [profile](./profiles.md)'s `tool_deny` add
to the policy for you.

## Approvals

A call that asks ends the run's stream with a `permission_request` event (the
calls, with a one-line `summary` each) and then a `suspended` event (their
`tool_use_id`s). Nothing waits in memory: answer when you are ready with
`agent.approve(run_id, decisions)`, which continues the run and returns its
new stream. This loop answers every approval until the result:

```python
import asyncio

from linch_rust import (
    Agent,
    AgentConfig,
    Allow,
    Decision,
    Deny,
    PermissionRequestEvent,
    ResultEvent,
    Run,
    Server,
    tool,
)
from linch_rust.scripted import MODEL, text_turn, tool_turn


@tool(scope="write")
async def refund(order: str, amount: float) -> str:
    """Refund an order."""
    return f"refunded {amount} on {order}"


def decide(request: dict) -> Decision:
    # Your policy, or a human's answer from a UI.
    if request["tool_name"] == "refund" and request["input"]["amount"] <= 50:
        return Allow()
    return Deny("refunds over 50 need a manager")


async def drive(agent: Agent, run: Run) -> ResultEvent:
    """Run until the result, answering every approval on the way."""
    while True:
        requests: list[dict] = []
        async for envelope in run:
            if isinstance(envelope.event, PermissionRequestEvent):
                requests.extend(envelope.event.requests)
            elif isinstance(envelope.event, ResultEvent):
                return envelope.event
        # The stream ended with `suspended`: answer what it asked.
        for request in requests:
            print("asked:", request["summary"], request["input"])
        decisions = {request["tool_use_id"]: decide(request) for request in requests}
        run = await agent.approve(run.id, decisions)


config: AgentConfig = {
    "namespace": "approvals",
    "spec": {"model": MODEL},
    "provider": {
        "id": "scripted",
        "turns": [
            tool_turn("c1", "refund", {"order": "A-1", "amount": 20.0}),
            tool_turn("c2", "refund", {"order": "B-7", "amount": 500.0}),
            text_turn("One refund done, one needs a manager."),
        ],
    },
}


async def main() -> None:
    async with Server() as server:
        agent = await server.open_agent(config, tools=[refund])
        result = await drive(agent, await agent.run("s1", "Refund A-1 and B-7"))
        print(result.final_text)


asyncio.run(main())
```

```text
asked: refund {'order': 'A-1', 'amount': 20.0}
asked: refund {'order': 'B-7', 'amount': 500.0}
One refund done, one needs a manager.
```

- `Allow()` runs the call. `Deny(reason)` refuses it, and the model reads the
  reason in the call's error result.
- A pending call with no decision is asked again: the run suspends once more.
- `agent.resume(run_id)` on a run waiting for approval suspends it again with
  the same pending calls; nothing runs.
- With Postgres stores the approval can come from another process or a later
  day; see [Runs](./runs.md#approve).

To decide in code without suspending, declare a `permission_request`
[callback](./agent.md#callbacks): it answers `allow` or `deny` before the run
would suspend, and when it gives no answer the call asks as usual.

## Virtual filesystem

`filesystem: {}` adds `ls`, `read_file`, `write_file` and `edit_file` over
text files that live in the stores, not on disk: scratch notes, plans, and
tool results too large for the conversation. With
`offload_threshold_tokens`, a text result estimated above that many tokens is
written to `/offload/<tool>_<tool_use_id>.txt` and the model gets its first
lines and the path:

```python
import asyncio

from linch_rust import AgentConfig, Server, ToolCallEndEvent, tool
from linch_rust.scripted import MODEL, text_turn, tool_turn


@tool(scope="read")
async def export_logs(service: str) -> str:
    """Return a service's recent log lines."""
    return "\n".join(f"{service} line {n}" for n in range(1, 2001))


config: AgentConfig = {
    "namespace": "vfs-demo",
    "spec": {"model": MODEL},
    "provider": {
        "id": "scripted",
        "turns": [
            tool_turn("c1", "export_logs", {"service": "api"}),
            tool_turn("c2", "read_file", {"path": "/offload/export_logs_c1.txt", "offset": 1999}),
            text_turn("Read the tail."),
        ],
    },
    "permissions": {"mode": "default", "project_root": "/"},
    "filesystem": {"offload_threshold_tokens": 1000},
}


async def main() -> None:
    async with Server() as server:
        agent = await server.open_agent(config, tools=[export_logs])
        async for envelope in await agent.run("s1", "What is in the api logs?"):
            if isinstance(envelope.event, ToolCallEndEvent):
                text = envelope.event.result["content"][0]["text"]
                print(f"{envelope.event.tool_name}: {len(text)} chars, ends {text[-60:]!r}")


asyncio.run(main())
```

```text
export_logs: 371 chars, ends 'read the rest, or ls("/offload") to list offloaded results.]'
read_file: 27 chars, ends 'api line 1999\napi line 2000'
```

`ls` and `read_file` are read-scope; `write_file` and `edit_file` are
write-scope, so they ask in mode `default`. Errors, results whose value is
not a string (a host tool's dict, say) and the filesystem tools' own results
are never offloaded. Where the files live
follows the stores: per session in memory, or shared by every session of the
namespace in Postgres ([Stores](./stores.md#the-virtual-filesystem)).

## MCP tools

Each entry of `mcp_servers` is connected when the agent opens; a server that
cannot be connected fails the open with `OpenError` (code -32015) naming it.
Its tools are offered as `mcp__<name>__<tool>`, with scope `exec` unless
`trust_annotations` is on and the server marks the tool read-only. This needs
an MCP server (it was checked against a test server whose `echo` tool is
read-only):

```python
import asyncio

from linch_rust import AgentConfig, InitEvent, Server, ToolCallEndEvent
from linch_rust.scripted import MODEL, text_turn, tool_turn

COMMAND = "/path/to/your-mcp-server"

config: AgentConfig = {
    "namespace": "mcp-demo",
    "spec": {"model": MODEL},
    "provider": {
        "id": "scripted",
        "turns": [tool_turn("c1", "mcp__demo__echo", {"text": "hi"}), text_turn("Done.")],
    },
    "mcp_servers": [
        {
            "name": "demo",
            "transport": {"type": "stdio", "command": COMMAND, "args": []},
            "trust_annotations": True,
            "call_timeout_ms": 30_000,
        }
    ],
}


async def main() -> None:
    async with Server() as server:
        agent = await server.open_agent(config)
        async for envelope in await agent.run("s1", "Echo hi"):
            event = envelope.event
            if isinstance(event, InitEvent):
                print("tools:", event.tools)
            elif isinstance(event, ToolCallEndEvent):
                print(event.tool_name, "|", event.result["content"][0]["text"])


asyncio.run(main())
```

```text
tools: ['mcp__demo__echo', 'mcp__demo__fail', 'mcp__demo__image', 'mcp__demo__pid', 'mcp__demo__slow']
mcp__demo__echo | hi
```

An HTTP server is `{"type": "http", "url": "http://127.0.0.1:8000/mcp",
"headers": {"Authorization": "Bearer ..."}}`. The tools a server has when the
agent opens enter the run contract; one it adds later does not. Every field
and default is in [Agent config](./agent.md#mcp_servers); reconnects,
cancellation and limits are in the [architecture MCP page](../architecture/mcp.md).

## Related pages

- [Agent config](./agent.md): `workspace`, `permissions`, `mcp_servers`,
  `filesystem`, callbacks.
- [Runs](./runs.md): approve, resume and cancel.
- [Events](./events.md): `tool_call_start`, `tool_call_end`,
  `permission_request`, `suspended`.
- [Architecture: tools](../architecture/tools.md): the Rust side.
