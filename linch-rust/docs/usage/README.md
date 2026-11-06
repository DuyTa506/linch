# Using linch-rust from Python

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
installed, so you can read or search them offline.

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
| [Agent config](./agent.md) | Every `AgentConfig` field with its default, `spec`, the run contract, intercept callbacks, sessions |
| [Providers](./providers.md) | Each provider id, keys from the server's environment, self-hosted server profiles, the scripted provider |
| [Tools](./tools.md) | Host tools, the workspace tools, scopes, permission modes and rules, approvals, the virtual filesystem, MCP tools |
| [Runs](./runs.md) | Start, stream, resume, approve, cancel; errors at the end of a stream; what is durable and when |
| [Events](./events.md) | Every event type the client decodes, with its fields; sequence numbers and replay; usage and cost |
| [Stores](./stores.md) | Memory and Postgres stores, DSNs, schemas, migrations, namespaces, pools |
| [CLI](./cli.md) | `linch-rust new`, `linch-rust add tool`, `linch-rust docs path` |
| [Troubleshooting](./troubleshooting.md) | Errors from the server binary to runs and tools, with causes and fixes |
| [Skills](./skills.md) | The `Skill` tool, the built-in `verify` skill, skills in `config_dir/skills` |
| [Subagents](./subagents.md) | The `Subagent` tool: foreground and background workers, continue, stop, fork, the mailbox |
| [Tasks](./tasks.md) | The session task tools |
| [Deep agent](./deep-agent.md) | The `deep_agent` preset and its profiles |
| [Profiles](./profiles.md) | Versioned domain profiles for an agent's runs |
| [Observability](./observability.md) | OpenTelemetry spans and metrics from the client |
| [Server](./server.md) | Running `linch-server` for remote tenants, and `Server.connect`: tokens, allowlists, limits |

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
| Hooks in-process | `@callback` at seven hook points, declared at open |
| `InMemorySessionStore`, Postgres stores | `{"type": "memory"}` or `{"type": "postgres", "dsn": ...}` |
| `ScriptedProvider` from `linch.ext.evals` | `{"id": "scripted", "turns": [...]}` with `linch_rust.scripted` |
| `linch` CLI | `linch-rust` CLI |

## Related

- [Architecture](../architecture/README.md): the Rust crates, for
  maintainers.
