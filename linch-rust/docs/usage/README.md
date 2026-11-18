# Using linch-rust from Python

[Tiếng Việt](../vi/usage/README.md)

Use this guide to build agents in Python with the `linch_rust` client, and to
find the page for each feature.

linch-rust has the same concepts as Python Linch (agents, sessions, runs,
events, tools, permissions, durable runs) with a faster kernel written in
Rust. The kernel runs in `linch-server`, a sidecar the `linch_rust` client
starts and drives over stdio, or one running for many clients that it reaches
over a Unix socket or WebSocket ([Server](./server.md)). You write Python
only; the Rust side is described in the
[architecture pages](../architecture/README.md).

The docs ship with the package: `linch-rust docs path` prints where they are
installed, so you can read or search them offline. `linch-rust skill install`
puts a skill in your project that points Claude Code or Codex at them
([CLI](./cli.md#linch-rust-skill-install)); `linch-rust new` adds it for you.

---

## Install

```bash
pip install linch-rust              # the client and the linch-server binary
pip install 'linch-rust[otel]'      # with OpenTelemetry spans and metrics
pip install 'linch-rust[websocket]' # to reach a remote server over WebSocket
```

Python 3.11 or newer. The only other runtime dependency is `orjson`. From a
checkout, see [Quickstart](./quickstart.md#1-install).

## Core concepts

```text
Server ─────── one linch-server child process, or a connection to a running one; many agents and runs
  └── Agent ── opened from an AgentConfig (plain data) plus host tools and callbacks
        └── session id ── one conversation; at most one live run
              └── Run ── one prompt: an async stream of Envelopes
```

- An **agent is a config**: a typed dict naming the model, provider, stores,
  workspace and switches. Open it once per server and reuse it for every
  session.
- **Host tools and callbacks** are async Python functions; the server calls
  back into your process to run them.
- A **run** streams typed events. It can suspend for approval and be resumed
  later, from another process when the stores are Postgres.

## Pages

| Page | What it covers |
|---|---|
| [Quickstart](./quickstart.md) | Install, an offline run, a live provider, a host tool, an approval, Postgres |
| [Agent config](./agent.md) | Every `AgentConfig` field with its default, `spec`, the run contract, system sections and the context budget, loop guard, reliability policy, intercept callbacks, sessions |
| [Providers](./providers.md) | Each provider id, keys from the server's environment, self-hosted server profiles, the scripted provider, what a provider supports and which models it serves, temperature, retries and fallback, the concurrency limit |
| [Sandbox](./sandbox.md) | Where `Bash` runs: `local`, `sandbox` (Landlock, seccomp, rlimits), `docker` or `remote` (the client runs the commands); what each confines |
| [Remote execution](./remote-execution.md) | `Bash` on a host your client supplies: what runs where, interrupted calls, the file tools, the protocol |
| [Tools](./tools.md) | Host tools, the workspace tools, scopes, permission modes and rules, approvals, retries and the result cache, schedule tools, the virtual filesystem, MCP tools |
| [Runs](./runs.md) | Start, stream, resume, approve, cancel, steer a run that is going on; errors at the end of a stream; what is durable and when |
| [Events](./events.md) | Every event type the client decodes, with its fields; sequence numbers and replay; usage and cost |
| [Structured output](./structured-output.md) | A JSON Schema the final answer must satisfy: on the agent or one run, retries, callbacks, what each provider receives, `tool_choice` and the final tool |
| [Memory](./memory.md) | Compaction and micro-compaction, a prompt longer than the context window, cut-off answers, file memory, built-in long-term memory, external memory through `context_build` |
| [Security](./security.md) | Permission modes and auto-allow, redaction of responses, tool results and prompts, final-answer verifiers and their budget, tool order and completion gates |
| [Stores](./stores.md) | Memory and Postgres stores, DSNs, schemas, migrations as a deploy step, namespaces, pools and the connection budget |
| [Embedding](./embedding.md) | What the SDK does for your application and what your application must provide: health, metrics, TLS, backups, upgrades, signals, sizing |
| [CLI](./cli.md) | `linch-rust new`, `linch-rust add tool`, `linch-rust docs path`, `linch-rust skill install` |
| [Troubleshooting](./troubleshooting.md) | Errors from the server binary to runs, tools, schedules, workflows and provider questions, with causes and fixes |
| [Skills](./skills.md) | The `Skill` tool, the built-in `verify` skill, skills in `config_dir/skills` |
| [Subagents](./subagents.md) | The `Subagent` tool: foreground and background workers, continue, stop, fork, the mailbox |
| [Tasks](./tasks.md) | The session task tools, and the task ledger shared by sessions |
| [Long-running agents](./long-running.md) | What differs for an agent that runs for hours: goal contract, task ledger, artifacts, steering, an independent done check, the harness pattern |
| [Deep agent](./deep-agent.md) | The `deep_agent` preset and its profiles |
| [Profiles](./profiles.md) | Versioned domain profiles for an agent's runs |
| [Observability](./observability.md) | OpenTelemetry spans and metrics from the client |
| [Monitoring](./monitoring.md) | What is traced, how cost is computed and priced, running an eval, reading a run report |
| [Server](./server.md) | Running `linch-server` for remote tenants, and `Server.connect`: tokens, allowlists, what admission refuses, limits, logging (`LINCH_LOG`), liveness checks for vanished clients |
| [Workflows](./workflows.md) | A Python function that directs a known sequence of agent calls: fan-out, pipeline, steps, interrupts; journaled in Postgres so a crash resumes without repeating finished calls; handoff between steps; `run_or_resume` |
| [Scheduler](./scheduler.md) | Cron and interval schedules in a time zone, heartbeats on one session, one-off background runs, schedule tools the model calls, job status, the worker that runs them |

Coming later:

| Topic | Will cover |
|---|---|
| Catalog changes and tool search | Changing an agent's tools and skills mid-session while keeping the prompt cache, and deferred tool schemas for large catalogs |

## How it differs from Python Linch

| Python Linch | linch_rust |
|---|---|
| `Agent(model=..., tools=..., ...)` in your process | `AgentConfig` dict opened on a `Server` sidecar |
| `session = await agent.session()`; `session.run(prompt)` | `agent.run(session_id, prompt)` with a session id you choose |
| `ToolRegistry`, `@tool`, class tools | `@tool(scope=...)` host tools; `scope` has no default |
| `permissions={"mode": "acceptEdits"}`, `can_use_tool` | `{"mode": "accept_edits", "project_root": ...}`; suspend and `agent.approve`, or a `permission_request` callback |
| Hooks in-process | `@callback` at twenty-three hook points, declared at open |
| `InMemorySessionStore`, Postgres stores | `{"type": "memory"}` or `{"type": "postgres", "dsn": ...}` |
| `ScriptedProvider` from `linch.ext.evals` | `{"id": "scripted", "turns": [...]}` with `linch_rust.scripted` |
| `session.align(text)` | `agent.steer(run_id, text)` ([Runs](./runs.md#steer)) |
| `ToolPipeline` listeners (retry, cache), `register_schedule_tools` | `spec.tool_retry`, `spec.tool_cache`, a `tool_execute` callback, `schedule_tools` ([Tools](./tools.md#retries-and-the-cache)) |
| `LoopGuard`, `ContextBudget`, `SystemPromptConfig` | `spec.loop_guard`, `spec.context_budget`, `spec.system` ([Agent config](./agent.md#spec)) |
| `run_workflow`, `WorkflowContext`, the workflow journal | `run_workflow` runs in the client, and the server keeps the journal in Postgres ([Workflows](./workflows.md)) |
| `RemoteExecutionBackend` | `execution: {"backend": "remote"}` with a client `ExecutionHost`, `Bash` only unless the workspace is shared ([Remote execution](./remote-execution.md)) |
| `default_headers`, `extra_body`, `timeout` of the OpenAI SDK | the provider's `request` and `chat` settings, which a tenant needs `allow.provider_request` to set ([Providers](./providers.md#request-settings)) |
| `linch` CLI | `linch-rust` CLI |
| `CompactionLadder`, `TruncationRecovery`, `RagConnector` | `spec.compaction`, `spec.truncation_recovery`, a `context_build` callback ([Memory](./memory.md)) |

## Related

- [Architecture](../architecture/README.md): the Rust crates, for
  maintainers.
