# Embedding linch-rust in your application

[Tiếng Việt](../vi/usage/embedding.md)

Use this page to see what linch-rust does for your application and what your
application must provide around it.

[<- Usage guide](./README.md)

linch-rust is an SDK, not a hosted service. Your Python application imports
`linch_rust`, which starts `linch-server` as a sidecar (or connects to one
you run) and drives agents through it. The sidecar has no health endpoint, no
metrics endpoint, no TLS and no admin interface: those belong to your application and its
platform.

---

## What the SDK does for you

- **Durable runs.** With Postgres stores, every event and checkpoint is
  durable before the next side effect (a model call, a tool start, the end),
  so a crash loses at most the call in flight ([Runs](./runs.md),
  [Stores](./stores.md)).
- **Leases and takeover.** One server drives a run at a time; a crashed
  server's run is free after the lease's length (30 s by default), and a
  server that stops cleanly releases its runs at once ([Run
  leases](./stores.md#run-leases)). The lease is renewed by a task of its own
  on a dedicated connection that each open agent's store keeps outside its
  pool (count one more per agent in the [connection
  budget](./stores.md#connection-budget)), so a client that stops reading its
  stream, or a pool whose connections are all busy, does not lose the run to
  another server.
- **Group commit.** Writes are committed together at the barriers above, not
  one per event.
- **Tenant admission.** A server with tenants checks every agent config
  against its tenant's allowlists and caps before anything runs ([Server](./server.md)).
- **Redaction.** Regex rules scrub responses, tool results and prompts before
  anything logs, streams or stores them ([Security](./security.md#redaction)).
- **Traces and metrics.** The Python client emits OpenTelemetry spans and
  metrics for runs, steps, model calls and tools ([Observability](./observability.md)).
- **Long results and long conversations.** Large tool results go to a file
  with a preview, and compaction keeps a conversation inside the context
  window ([Tools](./tools.md#long-tool-results), [Memory](./memory.md)).
- **Retention, if you turn it on.** A worker can delete ended runs and idle
  sessions after a window you choose ([Retention](./stores.md#retention)).
  Nothing is deleted by default.
- **Schedules and background jobs.** Cron and interval schedules and queued
  runs, run by a worker ([Scheduler](./scheduler.md)).

## What your application must provide

| Concern | What to do |
|---|---|
| **Health and readiness** | The sidecar has no endpoint for them. Report your own process's health: whether `Server()` started, and, for the database and its schema, `await linch_rust.schema_status(dsn)` (read only; it starts the server binary once, so call it at startup or on a slow probe, not per request). |
| **Metrics** | Use OpenTelemetry. The client emits spans and two metrics (`gen_ai.client.operation.duration`, `gen_ai.client.token.usage`); the server has no metrics. For run counts, lease holders or queue depth, query the database (`linch_runs` has `status`, `owner` and `lease_until`; the queue tables are in the [worker architecture](../architecture/worker.md)) or count in your application. Pool size and use are not exported: size from the [connection budget](./stores.md#connection-budget). |
| **TLS and authentication** | A listening server speaks plain HTTP and a Unix socket: put TLS in front or listen on loopback. Tokens are static per-tenant bearer tokens read at startup, with no expiry or revocation. Rotating one means restarting the server, which drops every live run on it (a Postgres run stays resumable; clients reconnect and resume). Keep an embedded `Server()` on stdio where you can: it needs neither. |
| **Backups** | All durable state is one Postgres schema (runs, events, sessions, files, queue, schedules, memories). Back it up as you back up that database. A restore puts every run back at the state of the snapshot, and nothing replays what happened after it; stop the servers before you restore, and test a restore before you need one. |
| **Log retention** | The server logs to stderr (`LINCH_LOG`). The log is not scrubbed: a provider, a tool or the database may have echoed a credential into an error. Keep logs as private as the database, and do not ship them to a shared system without your own scrubbing ([Server: Logging](./server.md#logging)). |
| **Upgrading** | Run [`migrate`](./stores.md#migrations-as-a-deploy-step) first, then start servers of the new release. A release can change the [run contract](./agent.md#the-run-contract) (a built-in tool's description, an MCP server's tool list, a hook policy version), and then a stored run that has not finished cannot resume. Read the `CHANGELOG.md` before every upgrade and let in-flight runs finish first. An old server on a newer schema refuses with `SchemaNewer` (-32016). |
| **Signals** | SIGTERM and SIGINT stop a server cleanly: runs are dropped at their last durable state, their leases released (waiting up to 5 s), and a worker gives its jobs `worker.drain_timeout_ms` (30 s) before it hands them back ([Scheduler: Stopping a worker](./scheduler.md#stopping-a-worker)). SIGKILL or a crash releases nothing: another server waits one lease (30 s by default; `stores.lease_ttl_ms`) before it takes the runs. Give your supervisor a grace period longer than the drain timeout plus about 15 s. |
| **Sizing** | Count connections with the [connection budget](./stores.md#connection-budget). Every cap is per process. For throughput and memory, see below. |
| **Isolation of commands** | `Bash` runs as the server's user by default. Choose a backend for what your agents run ([Sandbox](./sandbox.md), including [stronger isolation](./sandbox.md#isolation-levels)). |

## Platform support

- **Builds.** The release workflow builds wheels for Linux (x86_64 and aarch64)
  and macOS (x86_64 and aarch64), installs each in a fresh environment and runs
  the client tests against it. Windows is not supported: `linch-tools` and
  `linch-mcp` are Unix-only (see the comment at the top of
  `.github/workflows/rust-release.yml`).
- **Linux only.** The `sandbox` backend (Landlock and seccomp; `server.info`
  lists it only on Linux builds, [Sandbox](./sandbox.md)). On stdio the server
  reopens its pipes non-blocking through `/proc/self/fd`, and `Server()` raises
  the child's stdout pipe to 256 KiB; each falls back (tokio's own stdio, the
  default pipe size) where it cannot apply ([RPC](../architecture/rpc.md),
  [Server](./server.md)).
- **Tested.** The pull-request CI job runs on Linux only. The live `docker`
  cases run on Linux only, and the `docker` backend on macOS is untested.

---

## Sizing: what was measured

These numbers come from one run of the live checks in
`python/tests/live/measurements/durable.md`, on a shared 64-core host, with the
scripted provider (no model latency) and one Postgres. They show the order of
magnitude of the SDK's own cost, not what your agents will do: a real model
call takes seconds. Measure your workload.

| What | Measured |
|---|---|
| Durable runs on Postgres, 20 trivial scripted runs, 1 / 4 / 16 concurrent | 56 / 180 / 258 runs per second (memory stores: 4568 / 6713 / 7516) |
| Resume after a crash, server start to first resumed event (lease already expired) | p50 59 ms |
| Takeover after `kill -9` | 2.7 s on average at a 3 s lease; 28.6 s at the 30 s default (bounded by the lease) |
| Memory stores | about 110 KB of server memory per run, kept for as long as the agent lives |
| 1000 sessions in flight at once on one agent (Postgres, 4 turns each), peak process memory | 268 MB (about 0.25 MB per session; 207 MB with `MALLOC_ARENA_MAX=2`) |

The tool schemas are held once per agent and shared by every session, so a
session costs its own transcript and run state, not a copy of the catalogue. On
glibc, freed memory stays in per-thread arenas: for a process that serves many
sessions at once, set `MALLOC_ARENA_MAX=2` in the server's environment (23 %
lower peak in the same cell, measured 3 times each). A global allocator such as
jemalloc is another option; it saved about 9 % on top of the old per-session
copies and was not adopted.

Use Postgres stores for anything long-lived; memory stores keep every run's
history in the server for as long as the agent lives.

## A minimal deploy

1. Create the schema and two roles: one with DDL rights for `migrate`, one
   without for the servers.
2. `await linch_rust.migrate(admin_dsn)` as a deploy step, once per release.
3. Start your application, which starts `Server()` (or connect to a server you
   run with `linch-server --config`).
4. Stop with SIGTERM and give it the grace period above.

## Related pages

- [Stores](./stores.md): DSNs, migrations, the connection budget, leases, retention.
- [Server](./server.md): running `linch-server` for remote tenants.
- [Observability](./observability.md): OpenTelemetry from the client.
- [Troubleshooting](./troubleshooting.md): errors and their causes.
