# Agent config

Use this page to write the config an agent opens from, with every field and
its default, and to give the agent host tools and callbacks.

[<- Usage guide](./README.md)

In `linch_rust` an agent is data. You write an `AgentConfig` (a typed dict),
hand it to a running `Server`, and get back an `Agent` handle. Everything a
turn does runs inside the `linch-server` sidecar; it calls back into your
process only for the host tools and callbacks you declared.

```text
Server ─────── one linch-server child process, spoken to over stdio
  └── Agent ── opened from an AgentConfig; has a handle and a contract
        └── session id ── one conversation, a string you choose
              └── Run ── one prompt; iterate it for its envelopes
```

- Models and keys are on [Providers](./providers.md).
- Host tools, the workspace tools, permissions and approvals are on
  [Tools](./tools.md).
- Starting, resuming, approving and cancelling runs is on [Runs](./runs.md).
- Memory and Postgres stores are on [Stores](./stores.md).

---

## Open an agent

```python
import asyncio
import tempfile

from linch_rust import AgentConfig, ResultEvent, Server
from linch_rust.scripted import MODEL, text_turn

workspace = tempfile.mkdtemp()

config: AgentConfig = {
    "namespace": "support",
    "spec": {
        "model": MODEL,
        "system_prompt": "You answer support questions. Be brief.",
        "max_turns": 20,
        "max_output_tokens": 2048,
        "call_timeout_ms": 60_000,
        "compaction": {"threshold_tokens": 100_000, "keep_recent_turns": 6},
    },
    "provider": {"id": "scripted", "turns": [text_turn("Hello.")]},
    "workspace": workspace,
    "bash": False,
    "permissions": {"mode": "accept_edits", "project_root": workspace},
    "filesystem": {"offload_threshold_tokens": 20_000},
    "tasks": True,
}


async def main() -> None:
    async with Server() as server:
        agent = await server.open_agent(config)
        run = await agent.run("ticket-1", "Hi")
        result = await run.result()
        assert isinstance(result, ResultEvent)
        print(agent.handle, result.final_text)


asyncio.run(main())
```

```text
agent-1 Hello.
```

`Server(binary=None, *, env=None)` spawns the sidecar. `binary` overrides
where the server binary is found, and `env` is the server's whole environment,
where it reads provider keys (this process's environment when absent). Use it
as an async context manager: leaving the block closes the server's input, the
server drops its runs where they stand and exits, and it is killed if it has
not exited after 10 seconds. `Server.connect(address, token=...)` reaches a
server that is already running for many clients instead
([Server](./server.md#connect-from-python)); everything below works the same
on it.

`server.open_agent(config, tools=(), callbacks=(), *, telemetry=None)` opens
one agent:

| Argument | What it is |
|---|---|
| `config` | The `AgentConfig` below. A plain dict works at run time; the typed dict lets a type checker catch a misspelt field |
| `tools` | Host tools made with `@tool`, run in this process ([Tools](./tools.md#host-tools)) |
| `callbacks` | Intercept callbacks made with `@callback`, answered in this process ([below](#callbacks)) |
| `telemetry` | OpenTelemetry providers for this agent's runs ([Observability](./observability.md)) |

It returns an `Agent` with three attributes: `handle` (the server's name for
it, such as `agent-1`), `contract` (a digest of what its runs depend on; see
[The run contract](#the-run-contract)) and `skipped` ([below](#skipped-definitions)).
One server holds many agents; open them once and reuse them for every
session. `await agent.close()` frees one on the server: its stores and its
MCP servers, which are disconnected and waited for. With memory stores its
runs and sessions are gone; with Postgres stores they stay for another agent
of the same config. A closed agent raises `AgentNotFound`, and closing an
agent while one of its runs is still going raises `RunBusy`.

### Skipped definitions

With `skills` or `subagents` on, a skill directory or subagent file that
cannot be used is left out and the others load. `agent.skipped` lists each
one as a `Skipped(path, reason, detail)`:

```python
import asyncio
import tempfile
from pathlib import Path

from linch_rust import AgentConfig, Server
from linch_rust.scripted import MODEL

root = Path(tempfile.mkdtemp()).resolve()
(root / "skills" / "notes").mkdir(parents=True)  # no SKILL.md
(root / "agents").mkdir()
(root / "agents" / "reviewer.md").write_text("Review the code.")  # no frontmatter

config: AgentConfig = {
    "namespace": "demo",
    "spec": {"model": MODEL},
    "provider": {"id": "scripted", "turns": []},
    "skills": True,
    "subagents": True,
    "config_dir": str(root),
}


async def main() -> None:
    async with Server() as server:
        agent = await server.open_agent(config)
        for skipped in agent.skipped:
            print(Path(skipped.path).relative_to(root), skipped.reason)


asyncio.run(main())
```

```text
skills/notes missing_skill_md
agents/reviewer.md missing_frontmatter
```

`reason` is one of `missing_skill_md`, `missing_frontmatter`,
`invalid_frontmatter`, `io_error` (a symlink, or a file that could not be
read), `name_collision_tool`, `name_collision_skill` and `name_collision` (a
second subagent of the same name); `detail` says exactly what is wrong.

The server reads the config strictly. An unknown field at any level is a
`ProtocolError` (code -32602) naming the fields it accepts, so a misspelt or
unsupported option never goes unnoticed. A config never carries a provider
key: the server reads keys from its environment.

## Top-level fields

Only `namespace`, `spec` and `provider` are required. Every switch is off when
absent, except `bash`.

| Field | Default | What it controls |
|---|---|---|
| `namespace` | required | The store key space of this agent's runs and sessions; not blank. Two agents with the same namespace on one Postgres database see the same sessions |
| `spec` | required | The model, instructions and loop limits ([below](#spec)) |
| `provider` | required | The model provider, by `id` ([Providers](./providers.md)) |
| `stores` | `{"type": "memory"}` | Where runs and sessions live ([Stores](./stores.md)) |
| `workspace` | none: no workspace tools | An existing directory, the root of `Read`, `Write`, `Edit`, `Grep`, `Glob` and `Bash` ([Tools](./tools.md#workspace-tools)). A relative path resolves against the server's working directory, which is this process's |
| `bash` | `True` | Whether the workspace tools include `Bash`. Unused without a workspace |
| `permissions` | mode `default`, project root the workspace (or `/`) | Which calls run, ask or are denied ([below](#permissions)) |
| `mcp_servers` | `[]` | MCP servers connected in order when the agent opens ([below](#mcp_servers)) |
| `skills` | `False` | The `Skill` tool, with the built-in `verify` skill and those in `config_dir/skills` ([Skills](./skills.md)) |
| `subagents` | `False` | The `Subagent` tool, with the built-in `verification` subagent and those in `config_dir/agents` ([Subagents](./subagents.md)) |
| `workers` | none | `{"background": bool, "retain": bool}`, both `False` when absent: background workers and kept workers. Needs `subagents` ([Subagents](./subagents.md)) |
| `mailbox` | `False` | An in-process mailbox and the `send_message` tool between sessions of this agent ([Subagents](./subagents.md)) |
| `tool_search` | none | `{"max_tools": int, "max_skills": int}`, 30 and 50 when absent. Past `max_tools` a new session is offered only the tools that always load and `tool_search`, which the model calls to load the rest by keyword or `select:a,b` |
| `config_dir` | `.linch` | Directory holding `skills/` and `agents/`. A relative path resolves against the workspace, or the server's working directory without one. Unused unless `skills` or `subagents` is on |
| `filesystem` | none | The virtual filesystem tools and large-result offload ([below](#filesystem)) |
| `tasks` | `False` | The session task tools `TaskCreate`, `TaskList`, `TaskGet`, `TaskUpdate`, which run without approval in every mode ([Tasks](./tasks.md)) |
| `deep_agent` | none | The deep agent preset ([below](#deep_agent)) |
| `profile` | none | A versioned domain role for the agent's runs ([below](#profile)) |

## `spec`

| Field | Default | What it controls |
|---|---|---|
| `model` | required | Model id sent with every request; not blank |
| `system_prompt` | `""` | The system prompt; empty sends no system block |
| `system_blocks` | `[]` | Your own system blocks, `{"text": str, "cacheable": bool}` (`cacheable` `False` when absent), sent in order before the system prompt ([below](#system-blocks)) |
| `max_turns` | none: no limit | Most provider calls one run may make. A run that needs one more ends with a `loop_guard` event (`reason: "max_turns"`) and a `result` of subtype `error` |
| `thinking` | none: the provider's default | `{"type": "disabled"}`, `{"type": "adaptive", "display"?}` or `{"type": "enabled", "budget_tokens"?, "display"?}`; `display` is `summarized` or `omitted`. How each provider maps it is in the [architecture providers page](../architecture/providers.md#the-adapters) |
| `effort` | none | Reasoning effort: `low`, `medium`, `high`, `xhigh` or `max`, for providers that support it |
| `max_output_tokens` | none: the provider's default | Most tokens one response may generate |
| `max_tool_concurrency` | `4` | Most read-scope tool calls that run at once; `0` counts as 1 |
| `call_timeout_ms` | none: wait | Longest one tool call may run, in milliseconds; past it the call ends with a timeout error the model reads |
| `compaction` | none: never compact | When to summarize the older part of the conversation ([below](#compaction)) |

### System blocks

Every request sends the system blocks in this order: `system_blocks` as you
wrote them, then `system_prompt` (never cacheable), then the skill listing
when `skills` is on (cacheable). A session keeps the blocks of its first run
for all its requests, and a subagent gets the same blocks followed by its
definition's instructions. No block may be blank, and the blocks are part of
the [run contract](#the-run-contract), flags included.

Mark a block `cacheable` when it is stable: a provider that takes cache
breakpoints (Anthropic) puts the system breakpoint on the last cacheable block,
so everything up to it may be served from the prompt cache. Blocks after it,
such as the system prompt when no skill listing follows, sit outside that
breakpoint.

```python
spec = {
    "model": "claude-sonnet-4-6",
    "system_prompt": "Answer billing questions.",
    "system_blocks": [
        {"text": "Company policy: ...", "cacheable": True},
        {"text": "Today's promotions: ..."},
    ],
}
```

### `compaction`

| Field | Default | What it controls |
|---|---|---|
| `threshold_tokens` | none: follow the context window | Compact before a turn whose conversation is estimated at this many tokens or more (about four characters per token, 1000 per image); at least 1 |
| `keep_recent_turns` | `10` | Assistant turns kept verbatim after the summary; at least 1 |
| `prompt` | the built-in prompt | Instructions for the summary call |
| `max_output_tokens` | `4096` | Most tokens the summary may use; at least 1 |

Without `threshold_tokens` (`"compaction": {}` turns it on), the run compacts as
Python Linch does: when the estimate plus an output reserve reaches 80% of the
provider's [context window](./providers.md#context-window) for the spec's model.
The reserve is `spec.max_output_tokens` when set, else a fifth of the window,
at least 1 and at most 32768. A `threshold_tokens` replaces that rule and
ignores the window.

Compaction makes one extra provider call on the agent's provider and model,
and the run reports it with a `compaction` [event](./events.md).

## `permissions`

| Field | Default | What it controls |
|---|---|---|
| `mode` | `default` | The decision when no rule matches: `default` (write and exec calls ask), `accept_edits` (writes inside the project root run; exec asks), `skip_dangerous` (every call runs) |
| `project_root` | required when `permissions` is given | An absolute path (a relative one is refused at open, unlike `workspace`) that relative paths and path patterns resolve against |
| `layers` | `[]` | Named lists of rules, from least to most specific |
| `auto_allow` | `[]` | Tool names allowed in every mode (rules still apply first) |
| `path_fields` | built in | Which input field holds the path, per tool, added to the built-in table |
| `command_fields` | built in | Which input field holds a shell command, per tool, added to `Bash` -> `command` |

Read-scope calls run in every mode. Rules and layers are on the
[Tools](./tools.md#permission-rules) page.

## `mcp_servers`

Each entry connects one MCP server when the agent opens. Its tools are named
`mcp__<name>__<tool>`; the tools it has at that moment enter the run contract.
Durations are in milliseconds.

| Field | Default | What it controls |
|---|---|---|
| `name` | required | The `<name>` in `mcp__<name>__<tool>`: ASCII letters, digits, `-`, and single `_` between them |
| `transport` | required | `{"type": "stdio", "command", "args": [], "env": {}}` or `{"type": "http", "url", "headers": {}}` |
| `trust_annotations` | `False` | Whether a tool the server marks read-only gets the `read` scope instead of `exec` |
| `call_timeout_ms` | `300000` | Longest wait for one tool call before it is cancelled |
| `reconnect` | see below | How a lost connection is retried |

`reconnect` fields: `attempts` (`5`; `0` never reconnects), `initial_delay_ms`
(`500`, doubling), `max_delay_ms` (`30000`) and `attempt_timeout_ms` (`30000`,
the first connect included). A stdio server's process sees only `HOME`,
`LOGNAME`, `PATH`, `SHELL`, `TERM` and `USER` from the server's environment,
plus its own `env`. Connection, relisting and limits are in the
[architecture MCP page](../architecture/mcp.md). An example is on the
[Tools](./tools.md#mcp-tools) page.

## `filesystem`

`{}` turns on the virtual filesystem: the `ls`, `read_file`, `write_file` and
`edit_file` tools over text files that are not the workspace, and a short text
after the system prompt that teaches the model to use them.

| Field | Default | What it controls |
|---|---|---|
| `offload_threshold_tokens` | none | Move a text tool result estimated above this many tokens (four bytes per token) into the filesystem, leaving its first lines and its path for the model to `read_file` |
| `offload_threshold_fraction` | none | Offload above this fraction of the provider's [context window](./providers.md#context-window), at least 1000 tokens; Python Linch uses 0.1. Used only without `offload_threshold_tokens`. With neither, results are never offloaded |

With memory stores each session has its own files, gone with the server. With
Postgres stores every session of the namespace shares them. See
[Tools](./tools.md#virtual-filesystem).

## `deep_agent`

The deep agent preset plans with tasks, delegates to a roster of subagents and
keeps notes in the virtual filesystem. Behaviour and profiles are on the
[Deep agent](./deep-agent.md) page.

| Field | Default | What it controls |
|---|---|---|
| `profile` | `balanced` | A built-in profile by name (`balanced`, `coordinator`, `unbounded`) or a full profile dict (`name`, `durable`, `coordinator`, `max_turns`, `max_tokens`, `retain_subagents`, `enable_background_subagents`, all required) |
| `durable` | the profile's (`True` for the built-in ones) | Whether the agent needs Postgres stores; a durable deep agent on memory stores does not open |
| `coordinator` | the profile's | Whether the agent runs as a coordinator |

## `profile`

A domain profile is a versioned role. See [Profiles](./profiles.md).

| Field | Default | What it controls |
|---|---|---|
| `id` | required | Stable profile id; not blank |
| `version` | required | Revision of the profile; not blank |
| `system_prompt` | none | Role instructions, after the agent's system prompt |
| `tool_deny` | none | Tools whose calls are always denied, whatever the mode; the model is still offered them |
| `max_turns` | none | Replaces `spec.max_turns`; at least 1 |

## The run contract

`agent.contract` is a digest of what a run depends on. A run stores it when it
starts, and `resume` and `approve` refuse an agent whose contract differs
(`ContractMismatch`, code -32025). It covers:

- the `spec`, with what `profile`, `deep_agent` and `filesystem` add to it;
- every tool registered at open, in order (host tools, then workspace, task,
  filesystem and MCP tools), each with its name, description, schema and
  scope;
- each callback's `point`, `policy_id` and `policy_version`, in declaration
  order;
- the skills and subagent definitions loaded at open.

It does not cover the namespace, the stores, the provider config, a host
tool's `timeout`, the scripted turns, or a profile's id, version and denied
tools. Turning offload on or off changes it; changing the threshold does not.
To change an agent while runs are in flight, let them finish first, or keep the
old config for resuming them.

## Callbacks

A callback is an async function the server asks at one hook point of a run.
Declare it with `@callback(point, policy_id, policy_version, matcher=None,
timeout=None, fail_mode=None)` and pass it to `open_agent(callbacks=...)`.

```python
import asyncio
from typing import Any

from linch_rust import AgentConfig, Server, ToolCallEndEvent, callback, tool
from linch_rust.scripted import MODEL, text_turn, tool_turn


@tool(scope="read")
async def lookup(email: str) -> str:
    """Find a customer by email."""
    return "Ada Lovelace"


@callback("pre_tool_use", "no-external-emails", 1, matcher=["lookup"])
async def guard(request: dict[str, Any]) -> dict[str, Any]:
    if not request["input"]["email"].endswith("@example.com"):
        return {"action": "block", "reason": "only example.com customers"}
    return {"action": "continue"}


config: AgentConfig = {
    "namespace": "support",
    "spec": {"model": MODEL},
    "provider": {
        "id": "scripted",
        "turns": [tool_turn("call_1", "lookup", {"email": "eve@other.org"}), text_turn("Sorry.")],
    },
}


async def main() -> None:
    async with Server() as server:
        agent = await server.open_agent(config, tools=[lookup], callbacks=[guard])
        run = await agent.run("s1", "Who is eve@other.org?")
        async for envelope in run:
            if isinstance(envelope.event, ToolCallEndEvent):
                print(envelope.event.is_error, envelope.event.result["content"][0]["text"])


asyncio.run(main())
```

```text
True only example.com customers
```

The function takes the request dict and returns the point's answer. Every
request has `session_id` and `run_id`:

| `point` | The request also has | Answers |
|---|---|---|
| `pre_tool_use` | `tool_use_id`, `tool_name`, `input` | `{"action": "continue"}`, `{"action": "rewrite", "input"}`, `{"action": "block", "reason"}`, `{"action": "resolve", "result"}` |
| `permission_request` | `tool_use_id`, `tool_name`, `input`, `reason?`, `principal?` | `{"decision": "allow"}`, `{"decision": "deny", "reason"}` |
| `post_tool_use` | `tool_use_id`, `tool_name`, `input`, `result` | `{"action": "continue"}`, `{"action": "replace", "result"}` |
| `user_prompt_submit` | `content` (content blocks) | `{"action": "continue"}`, `{"action": "rewrite", "content"}`, `{"action": "block", "reason"}` |
| `context_build` | `messages` (the conversation the model will see) | `{"blocks"?: [{"text"}], "metadata"?: {...}}`; `metadata` is for observers and evals: it lands on the call's `ContextBuildEvent`, a later callback's key winning, and the model never sees it |
| `stop` | `text` | `{"action": "allow"}`, `{"action": "continue", "feedback"}` |
| `post_turn` | `turn`, `stop_reason` | `{}` |

A tool result in a request or an answer is `{"output": {"value",
"metadata"?}}` or `{"error": {"message", "code"?, "details"?}}`.

| Argument | Default | What it controls |
|---|---|---|
| `point` | required | One of the seven points above |
| `policy_id`, `policy_version` | required | Enter the run contract. Bump the version whenever the behaviour, matcher, timeout or fail mode changes |
| `matcher` | every tool | Tool-name globs (`*`, `?`), only at the three tool points; a miss never reaches this process |
| `timeout` | `30` seconds | How long the server waits for an answer |
| `fail_mode` | `closed` at `pre_tool_use` and `permission_request`, `open` elsewhere | What an exception, a bad answer or a timeout does: `closed` gives the point's blocking answer, `open` skips the callback. `context_build` and `post_turn` cannot fail closed |

Callbacks at one point run in declaration order. A `pre_tool_use` rewrite
feeds the next callback and is checked against the tool's schema again. At
`permission_request` a deny stops the chain and an allow does not, so a later
deny wins; with no answer the call asks the host as usual
([approvals](./tools.md#approvals)). A blocked `user_prompt_submit` ends the
run with `PromptBlocked` before anything is written. Each call shows up as a
stream-only `hook` [event](./events.md).

## Sessions

A session is any string id. The first run on an id creates it; later runs on
the same id continue the conversation with its full history. A session has at
most one run that is not finished ([Runs](./runs.md#one-live-run-per-session)).

`await agent.close_session(session_id)` tells the agent you are done with a
session for now: the workspace tools forget what it read (so a later write
must read the file again), and with memory stores its virtual files are
dropped. Nothing durable is deleted; the session and its runs stay.

## Related pages

- [Quickstart](./quickstart.md): the same shapes, step by step.
- [Providers](./providers.md): every `provider` config.
- [Tools](./tools.md): host tools, workspace tools, permissions, approvals.
- [Runs](./runs.md): start, stream, resume, approve, cancel.
