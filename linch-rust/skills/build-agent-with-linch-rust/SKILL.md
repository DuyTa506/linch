---
name: build-agent-with-linch-rust
description: Design, build, extend, debug, and test AI agents on linch-rust, the Rust kernel of Linch that Python drives through the linch_rust client and its linch-server sidecar. Use this skill whenever a task touches linch-rust, linch_rust, `import linch_rust` or `from linch_rust import`, or asks for an agent with host tools, callbacks, approvals, MCP servers, skills, subagents, a deep agent, profiles, durable resume on Postgres, a shared server for tenants, scaling out sidecars, steering a run while it works, agents that run for hours, compaction, long-term memory, loop guards, tool retry and caching, sandboxed commands, schedules, cost per model or per subagent, tenant prompt-cache isolation, upgrading a deployed server, or choosing an architecture for an agent pipeline, even if the request only says "make an agent" inside a project that depends on linch-rust.
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
| Rendering a stream, replay, usage and cost (`usage_by_model`, `subagent_usage`, `cumulative_by_model`, `cache_diagnostic`) | `usage/events.md` |
| A final answer another program parses (a JSON Schema) | `usage/structured-output.md` |
| Context-window errors, compaction, cut-off answers, file and long-term memory | `usage/memory.md` |
| An agent that works for hours; correcting a run while it works | `usage/long-running.md`, `usage/runs.md` (Steer) |
| Permission modes, redacting secrets, checking the final answer, tool order and completion gates | `usage/security.md` |
| Where `Bash` runs: `local`, `sandbox`, `docker`; on the client's machine (`remote`) | `usage/sandbox.md`, `usage/remote-execution.md` |
| Memory or Postgres stores, DSNs, pools; upgrading and rolling back between builds | `usage/stores.md` |
| Skills, subagents and workers for your agent | `usage/skills.md`, `usage/subagents.md` |
| A model that plans, delegates and keeps notes | `usage/deep-agent.md`, `usage/tasks.md` |
| Versioned roles on one setup | `usage/profiles.md` |
| Traces and metrics | `usage/observability.md` |
| One server for many processes or tenants; the `server.info` handshake; its logs and what it refuses; tenant cache isolation; many runs on one sidecar | `usage/server.md` |
| Health, backups, signals, sizing, memory, which platforms run it | `usage/embedding.md` |
| Whether the numbers a claim rests on were measured, and what was not | `benchmarks.md` |
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
- Entering `Server()` or `Server.connect(...)` runs the `server.info` handshake: a server outside
  the client's protocol range, or one older than `server.info`, raises `IncompatibleServer`, and a
  silent one `HandshakeTimeout` (15 s). `server.info.has("cache_isolation")` tests an optional
  feature by name (`usage/server.md`, `architecture/rpc.md`).
- Host tools are `async def` functions under `@tool(scope=...)`, passed as a list. `scope` is
  `read`, `write` or `exec` with no default; it decides approval and parallelism. A parameter
  typed `ToolContext` gets the call's ids, `principal` and `idempotency_key`
  (`run_id:tool_use_id`). A tool runs at most once, never exactly once: a call whose start was
  durable when a crash came is not run again, its end is an error with `error_code`
  `tool_interrupted`, and its effect is unknown. Make a tool that changes things check the key
  (`usage/runs.md`, "Tools run at most once").
- Callbacks are `@callback(point, policy_id, policy_version)` at one of the hook points in
  `usage/agent.md`, such as `pre_tool_use`. Bump `policy_version` whenever the behaviour changes.
- Modes are `default`, `accept_edits` and `skip_dangerous`. In `default`, a `write` or `exec`
  call ends the stream with `SuspendedEvent`; `agent.approve(run.id, decisions)` continues it
  with `Allow()` or `Deny(reason)` per pending `tool_use_id`.
- `ask_user: True` gives the model `AskUser`. Its call ends the stream with `InputRequestEvent`
  (the `questions`) and a `SuspendedEvent` of status `awaiting_input`;
  `agent.answer(run.id, {request_id: Answer({question_id: choice})})` continues it.
- A session is an id you choose with one live run at most (`SessionBusy`). `agent.run` returns
  only after the run is stored (Postgres: the create has committed), so an id it handed you
  survives a crash. An id you chose that is already stored is `RunExists` (-32023): resume it, or
  use `run_or_resume`. `LeaseLost` (-32022) means another server took the run over while this
  one drove it (`usage/runs.md`). Refusals such as
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
| A person corrects a run while it works | `await agent.steer(run.id, "skip file c")` | It lands at the next step, only the server driving the run takes it, memory stores lose it. At most 16 unread steers per run (`LimitReached`, `kind` `steers`), text up to 16 KiB, and an idle run (queued, or running with no server driving it) is `InvalidSteer`: resume it first |
| The conversation outgrows the context | `spec.compaction: {}` | The goal goes in `spec.system` `sections`, not the first message; what must survive goes in tasks, files or memory. A summary the model answers with a tool call, nothing, or a checkpoint no smaller than what it replaces is rejected and the view stays as it was; after 2 rejections in a row the run asks again only when the view has grown by a tenth of the window. Each rejection is a `compaction` event with `rejected` |
| A spending cap | `spec.budget: {"max_tokens": ..., "max_cost_usd": ...}` | `max_cost_usd` needs a price for every model that can serve (the primary and each `fallback_models` entry), or `agent.open` fails (`-32011`). vLLM, DeepSeek, SGLang and llama.cpp have no built-in price: set `pricing` (USD per million tokens, keyed by the id the provider reports) or cap `max_tokens` |
| A subagent must not spend without limit | `max_tokens`, `max_cost_usd`, `warn_ratio` in the frontmatter of `<config_dir>/agents/<name>.md` | The cap is per child run (`SubagentContinue` gets a fresh one) and still counts against the parent's `budget`. Read the split from `subagent_usage` and `usage_by_model` on the result (`usage/subagents.md`, `usage/events.md`) |
| Tenants share one provider key and one prompt cache | `allow.cache_isolation` plus the server's `cache_salt_secret` | Sends a salt per tenant: `cache_salt` to `vllm` and `sglang`, `prompt_cache_key` to `openai-chat` and `openai-responses`. An agent on `deepseek`, `anthropic`, `gemini` or `llamacpp` is refused (`NotAllowed`, `kind` `cache_isolation`). `monitoring: {"cache_diagnostic": True}` explains a cache miss (`usage/providers.md`, `usage/server.md`) |
| Tool calls must not hang | `spec.call_timeout_ms` | At least 100, or the open fails. With a `remote` execution backend keep it at 12 s or more |
| A program parses the answer | `spec.output_schema` | DeepSeek, vLLM, SGLang and llama.cpp get a final tool instead of a native schema; `server.provider_capabilities(config)` says which (`output_schema`), keyless |
| A read tool is flaky or repeated | `spec.tool_retry` (tools with `@tool(idempotent=True)` only); `spec.tool_cache` (`{"tools": [...], "ttl_ms": ...}`) | Retry a tool that changes things and its effect doubles. A cache stores results as the tool returned them: set `spec.redaction` first |
| The model runs shell commands | `execution: {"backend": "sandbox"}` or `{"backend": "docker"}` | `local`, the default, confines nothing; the sandbox is Linux only, needs Linux 6.12 and a server that is not root |
| Commands must run on your machine or a box you operate | `execution: {"backend": "remote"}` and `server.open_agent(config, execution=SubprocessHost(...))` | The host runs commands with its own privileges, outside every server confinement; `exec.start` carries `idempotency_key` when `server.info.has("exec_idempotency_key")`, so make the host deduplicate on it (`usage/remote-execution.md`); the model gets only `Bash`, `BashOutput`, `BashKill` unless `shared_workspace: True`; no schedules or workers; a tenant needs `allow.execution` to list it |
| Check-ins and follow-ups | `agent.heartbeat(...)`, `agent.create_schedule(...)`, `schedule_tools: True` | Needs Postgres stores, no host tools or callbacks, no `remote` execution, and a worker |
| A fixed sequence of agent calls that resumes after a crash | `run_workflow(agent, fn, run_id=..., principal=...)` with `wf.agent`, `wf.step`, `wf.interrupt` | Resume needs Postgres stores (with memory stores the journal dies with the connection, and `resume` answers are refused: `WorkflowNoJournal`). Pass the same `principal` on every start of one `run_id`. A `max_cost_usd` budget needs `pricing`. `fn` runs again from the top: keep code outside the journaled calls deterministic |

Memory, from the cheapest: **working** memory is the history, shortened by compaction;
**internal** notes the model keeps itself are `filesystem: {"memory": True}` (add `memory_scope`);
**long-term** facts across sessions, and for scheduled agents that have no callbacks, are
`memory: {"recall": {"k": 5}}` on Postgres stores (a run without a `principal` has no memory
under the default scope); **external** documents are a `context_build` callback built with
`linch_rust.memory.retrieval`. A long run keeps its goal, a `task_ledger` and its files outside the
conversation (`usage/long-running.md`).

On a shared `linch-server --config`, most of these are denied until the tenant's `allow` lists
them: `exec_tools`, `execution`, `tool_cache`, `memory` with `max_memories`, `task_ledger`,
`max_schedules`, `provider_info`, `provider_request`, `workflow`. A refusal is `NotAllowed` with
`.kind`; a config asking for `Bash` without `exec_tools` opens without it, silently. Every tenant
agent must also carry `max_turns` or a token or USD cap in `spec.budget`, or the open fails
(`kind` `run_bound`); the same holds for a stored schedule or job definition when its job runs.
Read the list in `usage/server.md`.

A WebSocket client must keep its event loop running: the server pings every 30 s and ends a
connection that sent nothing for 60 s (`liveness` in the server config), so run a long blocking
host tool in `asyncio.to_thread`. The server logs warnings to stderr; set `LINCH_LOG=info` (or
`RUST_LOG`) to see skipped fires, retries and failed jobs, which show nowhere else.

## Pitfalls that bite

- **The run contract is a digest.** Spec, tool descriptions, callbacks' `policy_version`, skills and
  subagents feed it; a different digest cannot resume an unfinished run (`ContractMismatch`).
  Keep old configs until their runs finish, and read the CHANGELOG before an upgrade. A new
  optional field that is unset stays off the wire and leaves digests alone; rely on that when
  adding one (`usage/agent.md`, "The run contract").
- **Fields are additive.** Read events and results by name and ignore ones you do not know; a
  client older than the server sees new fields, and an unknown field you send is `-32602`.
- **Durability is Postgres only.** Memory stores lose runs with the server, `resume` from another
  process and schedules need Postgres (`usage/stores.md`).
- **A shared server denies by default.** Tenant admission refuses what could be abused until
  `allow` lists it (list above, plus `cache_isolation`). A config that works on `Server()` can fail
  with `NotAllowed` on a tenant: test with the tenant's real config.
- **Host tools run at most once, not exactly once.** Anything that changes the world uses
  `ctx.idempotency_key` or checks first (see section 3).
- **Offload thresholds are your choice.** A tool result is never moved into files unless
  `filesystem.offload_threshold_tokens` (or `offload_threshold_fraction`) is set, so one big result
  can fill the context (`usage/agent.md`, `usage/tools.md`).
- **A timing number can be a frequency level.** On a host with a `powersave` governor the same work
  ran at 2.0 or 3.4 GHz, 1.7x apart. Record the CPU frequency per iteration, compare cells from one
  run, and say when a gap was not explained (`benchmarks.md`, "Known gaps").
- **Upgrading.** Run `migrate` with the new binary, start new servers, drain the old, and let
  suspended runs finish first. A rollback past a migration needs a restored database, so finish the
  later build's runs first (`usage/stores.md`, "Rolling deploy").

## Platform support

Wheels are built and client-tested for Linux and macOS (x86_64 and aarch64). Windows is not
supported. The `sandbox` backend, the non-blocking stdio reopen and the 256 KiB stdout pipe are
Linux only (the last two have fallbacks); pull-request CI runs on Linux only, and `docker` on macOS
is untested. `server.info.backends` lists what a build implements (`usage/embedding.md`).

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
   them. See `usage/providers.md`. Never print or commit a key. The repository's live suites
   (`pytest -m live`) skip without `LINCH_TEST_VLLM_URL`; `LINCH_LIVE_KIND=sglang` drives them as
   SGLang, and the `docker` backend cases also need `LINCH_LIVE_DOCKER=1` (`usage/sandbox.md`).
   A local SGLang server needs `--reasoning-parser` for reasoning and `--enable-cache-report`
   for cache usage: without the latter `cache_read_tokens` is 0 because it is unknown
   (`usage/monitoring.md`).

## 5. Debug

- Grep the exact message in `usage/troubleshooting.md`. A server error is an `RpcError` with
  `code`, `message` and `data`; its class names the family.
- A documented field refused as unknown means the `linch-server` binary is older than the client
  (`LINCH_SERVER_BIN` wins, then the wheel's binary, then a cargo build).
- `InitEvent.tools` lists what the model is offered; `agent.skipped` lists skill and subagent
  files that did not load; `agent.events(run_id, after_seq=n)` replays the durable events.

## 6. Scale out when one sidecar is the limit

- A `Server()` sidecar speaks stdio and runs its kernel on one thread (`architecture/rpc.md`),
  about 700 to 750 turns per second. Above roughly 500 runs in flight, either set
  `LINCH_STDIO_THREADS=N` (2 to 256) in `Server(env=...)`, which costs CPU and 1 to 3 ms per small
  run, or spread sessions over several sidecars, on Postgres stores so any can resume a run
  (`usage/server.md`, "Many runs on one stdio sidecar"). Give each sidecar its share of the database's connections
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
  limit (`usage/stores.md`). Measure your own load before and after. For memory with many
  sessions set `MALLOC_ARENA_MAX=2` in the server's environment (`usage/embedding.md`).

## 7. Finish

Before you call it done, run the project's tests and read the output. Then report what you built,
which test commands passed, and what ran against a real model. If the live run did not happen, say
so plainly, because offline tests do not prove the model works with your prompts and tools.

If you hit a place where the docs were wrong or silent, say which page.
