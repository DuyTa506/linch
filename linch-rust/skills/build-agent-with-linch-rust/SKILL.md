---
name: build-agent-with-linch-rust
description: Design, build, extend, debug, and test AI agents on linch-rust, the Rust kernel of Linch that Python drives through the linch_rust client and its linch-server sidecar. Use this skill whenever a task touches linch-rust, linch_rust, `import linch_rust` or `from linch_rust import`, or asks for an agent with host tools, callbacks, approvals, MCP servers, skills, subagents, a deep agent, profiles, durable resume on Postgres, a shared server for tenants, scaling out sidecars, steering a run while it works, agents that run for hours, compaction, long-term memory, loop guards, tool retry and caching, sandboxed commands, schedules, or choosing an architecture for an agent pipeline, even if the request only says "make an agent" inside a project that depends on linch-rust.
---

# Build agents with linch-rust

linch-rust is the Linch kernel written in Rust, and you still write Python: the `linch_rust`
client drives a `linch-server` sidecar that runs the loop, stores and built-in tools. It is not
Python Linch, whose skill fits a project that imports `linch`. Much of what you remember about
Python Linch is wrong here, so read the docs that ship with the installed version instead.

## Design first when the architecture is open

Most tasks skip this. Read [references/design.md](references/design.md) before building when the
system has several stages or agents, runs for a long time, takes actions that are hard to undo,
serves several tenants, or the user is asking how to structure it.

## 1. Find the docs

```bash
DOCS="$(linch-rust docs path)"
ls "$DOCS/usage" "$DOCS/architecture"
```

If `linch-rust` is not on `PATH`, activate the project's environment or run
`python -m linch_rust.cli docs path`. Search with `grep -rn`, not `rg`: installed docs sit in
`site-packages`, and `rg` can skip them silently because of ignore rules.

```bash
grep -rn "open_agent" "$DOCS/usage"
grep -rn "is busy with run" "$DOCS/usage/troubleshooting.md"
```

- Every config field with its default: `usage/agent.md`.
- An error message or exception: grep its exact text, starting with `usage/troubleshooting.md`.
- What differs from Python Linch: the table in `usage/README.md`; most pages end with one too.
- How the kernel works inside, or extending it in Rust: `architecture/README.md`.

Read the `linch_rust` source only when the docs do not answer, and report the gap.

## 2. Read before you write

Most tasks need more than one row. Paths are relative to `$DOCS`.

| You need | Read |
|---|---|
| First agent, project setup | `usage/quickstart.md`, `usage/cli.md` |
| Config fields, sessions, callbacks, the run contract | `usage/agent.md` |
| Models, keys, self-hosted servers, the scripted provider | `usage/providers.md` |
| Host tools, workspace tools, permissions, approvals, MCP, retry and cache of tool calls | `usage/tools.md` |
| Start, stream, resume, approve, cancel; what is durable | `usage/runs.md` |
| Rendering a stream, replay, usage and cost | `usage/events.md` |
| A final answer another program parses (a JSON Schema) | `usage/structured-output.md` |
| Context-window errors, compaction, cut-off answers, file and long-term memory | `usage/memory.md` |
| An agent that works for hours; correcting a run while it works | `usage/long-running.md`, `usage/runs.md` (Steer) |
| Permission modes, redacting secrets, checking the final answer, tool order and completion gates | `usage/security.md` |
| Where `Bash` runs: `local`, `sandbox`, `docker` | `usage/sandbox.md` |
| Memory or Postgres stores, DSNs, pools | `usage/stores.md` |
| Skills, subagents and workers for your agent | `usage/skills.md`, `usage/subagents.md` |
| A model that plans, delegates and keeps notes | `usage/deep-agent.md`, `usage/tasks.md` |
| Versioned roles on one setup | `usage/profiles.md` |
| Traces and metrics | `usage/observability.md` |
| One server for many processes or tenants | `usage/server.md` |
| Cron or interval schedules, heartbeats, background runs | `usage/scheduler.md` |
| A known sequence of agent calls (fan-out, pipeline, approval, a signing step) that must resume after a crash; one run started once under a chosen id | `usage/workflows.md` |

## 3. Build it the way linch-rust expects

```python
async with Server() as server:  # or Server.connect(address, token=...)
    agent = await server.open_agent(config, tools=[lookup], callbacks=[guard])
    run = await agent.run("session-1", "Find ada@example.com")
    async for envelope in run:  # envelope.seq, envelope.event
        ...
    result = await run.result()  # ResultEvent, or SuspendedEvent when a call asks
```

- An agent is an `AgentConfig` dict; `namespace`, `spec` and `provider` are required. An unknown
  field fails the open with `ProtocolError` (-32602), so take names from `usage/agent.md`. Keys
  never go in the config: the server reads its environment, which `Server(env=...)` sets.
- Host tools are `async def` functions under `@tool(scope=...)`, passed as a list. `scope` is
  `read`, `write` or `exec` with no default; it decides approval and parallelism. A parameter
  typed `ToolContext` gets the call's ids, `principal` and `idempotency_key`.
- Callbacks are `@callback(point, policy_id, policy_version)` at one of the hook points in
  `usage/agent.md`, such as `pre_tool_use`. Bump `policy_version` whenever the behaviour changes.
- Modes are `default`, `accept_edits` and `skip_dangerous`. In `default`, a `write` or `exec`
  call ends the stream with `SuspendedEvent`; `agent.approve(run.id, decisions)` continues it
  with `Allow()` or `Deny(reason)` per pending `tool_use_id`.
- `ask_user: True` gives the model `AskUser`. Its call ends the stream with `InputRequestEvent`
  (the `questions`) and a `SuspendedEvent` of status `awaiting_input`;
  `agent.answer(run.id, {request_id: Answer({question_id: choice})})` continues it.
- A session is an id you choose with one live run at most (`SessionBusy`). Refusals such as
  `RunNotFound` and `ContractMismatch` arrive at the end of the stream. A provider or tool failure
  is an `ErrorEvent` and a `ResultEvent` of subtype `error`, not an exception.
- Pass `run_id=` to find a run again after a crash, then `agent.resume(run_id)`: on memory stores
  through the same `Agent`, on Postgres from any agent with the same `agent.contract`. The
  contract covers the spec, tools, callbacks, skills and subagents, so keep old configs until
  their runs finish. On Postgres a run another server still drives (or that crashed less than a
  lease ago, 30 s by default) is refused with `RunLeased`; retry after `.expires_in` seconds.
- Open each agent once and reuse it; `await agent.close()` at the end. Read each run to its end
  or `run.cancel()` it, because an unread run goes on and its events queue without a bound.
- Start a project with `linch-rust new <name>` and add tools with `linch-rust add tool <name>`.
- A new provider adapter, tool kind or store is Rust work: start at `architecture/README.md`.

### Pick the control by the problem

Each is a spec or config field in the Python client; the page holds the fields. Ask whether the
problem is real before adding one.

| The problem | Use | Pitfall |
|---|---|---|
| The run repeats one call | `spec.loop_guard`, on by default: ends the run `failed` with a `loop_guard` event | A host tool that polls needs `polls=True`, or each poll counts as a repeat |
| Calls in the wrong order, or "done" too early | `spec.policy`: `{"prerequisites": [{"tool": "Edit", "requires": ["Read"]}], "gates": [...]}` | An ordering aid for the model, not security: permissions and the sandbox are |
| The model must call a tool, or none | `spec.tool_choice` or `agent.run(..., tool_choice=...)` | A forced choice holds every turn: give the run a final tool or `max_turns`. DeepSeek refuses it while thinking |
| A person corrects a run while it works | `await agent.steer(run.id, "skip file c")` | It lands at the next step, only the server driving the run takes it, memory stores lose it |
| The conversation outgrows the context | `spec.compaction: {}` | The goal goes in `spec.system` `sections`, not the first message; what must survive goes in tasks, files or memory |
| A program parses the answer | `spec.output_schema` | DeepSeek, vLLM, SGLang and llama.cpp get a final tool instead of a native schema; `server.provider_capabilities(config)` says which (`output_schema`), keyless |
| A read tool is flaky or repeated | `spec.tool_retry` (tools with `@tool(idempotent=True)` only); `spec.tool_cache` (`{"tools": [...], "ttl_ms": ...}`) | Retry a tool that changes things and its effect doubles. A cache stores results as the tool returned them: set `spec.redaction` first |
| The model runs shell commands | `execution: {"backend": "sandbox"}` or `{"backend": "docker"}` | `local`, the default, confines nothing; the sandbox needs Linux 6.12 and a server that is not root |
| Check-ins and follow-ups | `agent.heartbeat(...)`, `agent.create_schedule(...)`, `schedule_tools: True` | Needs Postgres stores, no host tools or callbacks, and a worker |

Memory, from the cheapest: **working** memory is the history, shortened by compaction;
**internal** notes the model keeps itself are `filesystem: {"memory": True}` (add `memory_scope`);
**long-term** facts across sessions, and for scheduled agents that have no callbacks, are
`memory: {"recall": {"k": 5}}` on Postgres stores (a run without a `principal` has no memory
under the default scope); **external** documents are a `context_build` callback built with
`linch_rust.memory.retrieval`. A long run keeps its goal, a `task_ledger` and its files outside the
conversation (`usage/long-running.md`).

On a shared `linch-server --config`, most of these are denied until the tenant's `allow` lists
them: `exec_tools`, `execution`, `tool_cache`, `memory` with `max_memories`, `task_ledger`,
`max_schedules`, `provider_info`. A refusal is `NotAllowed` with `.kind`; a config asking for
`Bash` without `exec_tools` opens without it, silently. Read the list in `usage/server.md`.

## 4. Test it like it is real

A test that replaces the part you depend on proves nothing about that part. Fake only the model.

1. Use the `scripted` provider with `MODEL`, `text_turn` and `tool_turn` from
   `linch_rust.scripted`; each model call takes the next turn, subagents' included. Run it on a
   real `Server()` so tools, permissions, callbacks and events execute, not just `my_tool.fn`.
2. Use real boundaries: a temp workspace, a real MCP subprocess, and real Postgres for anything
   durable, skipped cleanly without a DSN. Close agents and servers in the test.
3. Test the failure each feature exists for: a write suspends and a `Deny` reason reaches the
   model, a blocking callback blocks, `max_turns` stops a run, a scripted `{"error": {...}}` turn
   shows as an `ErrorEvent`, and a run resumed after killing its sidecar (`server.pid`, Postgres)
   does not repeat its side effects.
4. Assert on events such as `ToolCallEndEvent`, on `agent.events(run_id)`, and on your tools'
   effects. A scripted final text is text you wrote.
5. Run end to end with a real model when credentials exist, in a separate test that skips without
   them. See `usage/providers.md`. Never print or commit a key.

## 5. Debug

- Grep the exact message in `usage/troubleshooting.md`. A server error is an `RpcError` with
  `code`, `message` and `data`; its class names the family.
- A documented field refused as unknown means the `linch-server` binary is older than the client
  (`LINCH_SERVER_BIN` wins, then the wheel's binary, then a cargo build).
- `InitEvent.tools` lists what the model is offered; `agent.skipped` lists skill and subagent
  files that did not load; `agent.events(run_id, after_seq=n)` replays the durable events.

## 6. Scale out when one sidecar is the limit

- A `Server()` sidecar speaks stdio and runs its kernel on one thread (`architecture/rpc.md`).
  With hundreds of sessions at once, spread them over several sidecars, on Postgres stores so any
  can resume a run. Give each sidecar its share of the database's connections
  (`stores.max_connections`, below): four at the default pool size can pass Postgres's limit of
  100. In the linch-rust repository's concurrent-sessions bench (case 14), four sidecars of 20
  connections each cut the overhead per turn 3.5 to 4.5 times against one, from 100 to 2000
  sessions.
- For many processes, machines or tenants, run one multi-threaded `linch-server --config FILE`
  and use `Server.connect`. A tenant drives 16 runs at once unless `max_runs` is raised.
- A server process has one Postgres pool per target (per tenant on a listening server) of
  `stores.max_connections`: when absent, the machine's cores between 10 and 32. A tenant may
  ask for at most its `max_pool_connections`.
  Plan two connections per run in flight, and keep the sum over sidecars under the database's
  limit (`usage/stores.md`). Measure your own load before and after.

## 7. Finish

Before you call it done, run the project's tests and read the output. Then report what you built,
which test commands passed, and what ran against a real model. If the live run did not happen, say
so plainly, because offline tests do not prove the model works with your prompts and tools.

If you hit a place where the docs were wrong or silent, say which page.
