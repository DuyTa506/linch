# linch-rust architecture

Developer reference for linch-rust, the Rust port of the Linch agent kernel.
It describes the contracts, data flow and invariants of each crate, and how
to extend them in Rust. It is for people who maintain linch-rust or add
providers, tools and stores to it. Applications use the Python client, which
has its own usage docs.

---

## Conventions

- `file:line` references are relative to `linch-rust/` (for example
  `crates/linch-core/src/tool.rs:213`) and match the revision the page was
  written against. Prefer them over re-deriving behavior.
- Every Rust snippet compiles against the workspace crates at that revision;
  the ones with a `main` that needs no network or database also run.
- Python Linch's architecture docs live at the repository root, in
  [`docs/architecture/`](../../../docs/architecture/README.md). A page links
  them only where a concept is shared, and says what differs.

## Pages

| Page | What it covers |
|---|---|
| [Providers](./providers.md) | The provider contract in `linch-core`, the event vocabulary and stream rules, errors and retries, the adapters in `linch-providers` (DeepSeek, OpenAI Chat, OpenAI Responses, vLLM, SGLang, llama.cpp, Anthropic, Gemini), the single SSE loop, pricing, adding a provider |
| [Tools](./tools.md) | The `Tool` trait and scopes, the registry and reversible registration, how a batch runs and what runs in parallel, the workspace tools and confinement, the virtual filesystem and result offload, the session task tools, writing a tool |
| [MCP client](./mcp.md) | `linch-mcp`: stdio and HTTP transports, tool mapping, relist, reconnect, cancel, process-group kill, bounded close, limits and redaction |
| [Stores](./stores.md) | Run, session, task and file store contracts and their conformance suites; memory stores; the Postgres stores: schema, migrations, one live run per session, fencing, single-statement writes, refusal classification, pool sizing; group-commit write counts |
| [Worker](./worker.md) | `linch-worker`: the Postgres job queue, the worker loop, leases and fencing, back-off and dead letters, one active job per run, cron schedules |
| [Extensions](./ext.md) | `linch-ext`: the eval harness and scorers, run reports, journaled workflows on memory and Postgres journals |
| [OpenTelemetry](./otel.md) | `linch-otel`: spans and metrics built from the event stream, the mapping to Python Linch's names, content capture, what hosts wire |

Coming later (these pages are not written yet; read the source and its doc
comments until they are):

| Page | Will cover | Python counterpart |
|---|---|---|
| Core and kernel | `Agent`, `AgentBuilder`, `AgentSpec`, the run contract digest, hooks, permissions | [kernel](../../../docs/architecture/kernel.md) |
| Turn lifecycle | The run state machine in `crates/linch-core/src/runner/`, barriers, suspend and resume, reconcile | [turn lifecycle](../../../docs/architecture/turn-lifecycle.md) |
| Events | `Event`, `Envelope`, persistence classes and the wire format | [events](../../../docs/architecture/events.md) |
| Compaction | When and how the provider view is summarized | [compaction](../../../docs/architecture/compaction.md) |
| Skills and subagents | The `Skill` and `Subagent` tools and child runs | [skills and subagents](../../../docs/architecture/skills-subagents.md) |
| RPC protocol and server | `linch-rpc` (JSON-RPC 2.0 protocol, server and client) and `linch-server` (the sidecar over stdio, a Unix socket, or HTTP with WebSocket) | none |

The Python pages describe the shared concepts only. Each Rust page will say
where Rust differs; until then the Rust source is the reference. The main
difference in the kernel is under [Shared invariants](#shared-invariants).

## Crates

| Crate | Role | Page |
|---|---|---|
| `linch-core` | Python-free core: types, wire codecs, the provider and tool contracts, the registry, scheduler, runner, journal and store traits | Providers, Tools, Stores (the rest later) |
| `linch-providers` | HTTP model provider adapters and pricing | [Providers](./providers.md) |
| `linch-tools` | Workspace tools, Bash, the virtual filesystem, result offload, session task tools | [Tools](./tools.md) |
| `linch-mcp` | MCP client: server tools as registry tools | [MCP client](./mcp.md) |
| `linch-postgres` | Postgres run, session, task and file stores | [Stores](./stores.md) |
| `linch-worker` | Postgres job queue, worker fleet and cron schedules | [Worker](./worker.md) |
| `linch-ext` | Evals, run reports, workflows | [Extensions](./ext.md) |
| `linch-otel` | OpenTelemetry traces and metrics from the event stream | [OpenTelemetry](./otel.md) |
| `linch` | Rust facade: the public API plus one serde config that opens an agent | later |
| `linch-rpc`, `linch-server` | The JSON-RPC protocol and the sidecar | later |
| `linch-python` | PyO3 extension behind selected internals of the Python package | later |

## Shared invariants

These hold across the pages above, as in Python Linch's
[key invariants](../../../docs/architecture/invariants.md):

- **Async only on the hot path.** Blocking file work runs on Tokio's blocking
  pool (`crates/linch-tools/src/fs/local.rs:228`,
  `crates/linch-tools/src/vfs/disk.rs:57`); the core spawns no task, and a
  run advances only while the caller polls its stream
  (`crates/linch-core/src/runner.rs:37-38`).
- **Registrations are reversible.** Every registry and hook registration
  returns a `Registration` whose `dispose` undoes exactly that registration
  and is idempotent ([tools](./tools.md#reversible-registration)).
- **Only read work runs in parallel.** Write and exec calls run alone
  ([tools](./tools.md#only-read-work-runs-in-parallel)).
- **Durable before side effects.** Run state is committed at barriers: before
  each provider dispatch, each tool start, each session write and the end
  ([stores](./stores.md#group-commit-write-counts)).
- **Durable state is Postgres; memory stores are for tests and development.**
  Both pass the same conformance suites ([stores](./stores.md#conformance-suites)).
- **Tenancy by namespace.** Records are keyed by a host-chosen namespace plus
  an id, so tenants never share a key space
  ([stores](./stores.md#keys-and-tenancy)).
- **Observability is OpenTelemetry**, built from the event stream with Python
  Linch's names ([OpenTelemetry](./otel.md)).

What differs from Python: there is no IoC `Context`, event bus or tool
pipeline; extension points are traits (`Provider`, `Tool`, `RunStore`,
`SessionStore`, `TaskStore`, `FileBackend`, `JournalStore`) and hook chains
(`crates/linch-core/src/hooks.rs`) given to `AgentBuilder::hooks`
(`crates/linch-core/src/agent/builder.rs:78`).
