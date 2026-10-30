# CLAUDE.md

Guidance for Claude Code working in this repository. Keep it short; read the
linked docs when a task touches a specific subsystem.

## What this is

**Linch** is an async-first, event-driven, provider-agnostic Python SDK for
embedding a software-engineering agent loop inside other applications. Core
flow:

```text
Agent (config) → Session (state) → run_loop() → Events → caller
```

Source lives in `src/linch/` (src-layout package). Tests mirror the source tree
under `tests/`; examples under `examples/`; docs under `docs/`.

## Commands

```bash
pip install -e '.[dev,mcp,anthropic,gemini]'   # dev install
pytest                                          # all tests
pytest tests/test_budget.py::test_name          # one test
ruff check . && ruff format --check .           # lint + format check
ruff check --fix . && ruff format .             # auto-fix
pyright                                          # type check
```

Run `pytest`, `ruff`, and `pyright` before opening a PR. Unit tests must not
depend on live services; live-provider coverage lives in `tests/integration/`
and skips cleanly without credentials.

## Where to read more (progressive disclosure)

Don't guess at a subsystem's internals — read its doc first. Two indexes map
the whole system:

- **`docs/architecture/README.md`** — per-subsystem contracts, data flow, and
  invariants (loop, providers, tools, permissions, memory, filesystem,
  compaction, skills/subagents, events, data types, module inventory).
- **`docs/usage/README.md`** — how to *use* each feature (agent config,
  providers, tools, context/memory, filesystem, hooks, structured output,
  deep agent, coordination, workflows, evals).
- **`docs/versioning.md`** — the semver / public-API contract.

When working on subsystem X, open the matching `docs/` page before editing, and
prefer the `file:line` references there over re-deriving behavior from scratch.

## Non-negotiable design constraints

These hold for *every* change, regardless of subsystem:

- **Async only** — no blocking I/O in the core loop (`loop/`, `scheduler.py`,
  `compaction.py`, `providers/`).
- **Tools are duck-typed protocols** — implement the protocol attributes
  directly; do not introduce a base class to inherit from.
- **Registrations are reversible effects** — a registration returns a
  `Disposable` (`linch.Disposable`) whose call undoes exactly that registration,
  and is idempotent. `ToolRegistry.register/replace` already follow this;
  new registries and extension points do the same. See `docs/architecture/kernel.md`.
- **New behavior hangs off kernel extension points, not loop edits** — extend
  tool execution through the pipeline seams (`tools/pre-execute` →
  `tools/execute` → `tools/post-execute` via `ToolPipeline`), not by threading
  new branches into `scheduler.py`/`loop/`. Each seam is registered as an effect
  (returns a `Disposable`) and stays byte-identical when no listener is attached.
- **Single append-only session log; session-owned model-visible state is logged** —
  `Session` owns one `SessionLog` (`src/linch/session_log.py`). Its
  `provider_view` (model-visible) and `full_history` (audit) are *deep-copied,
  read-only projections* of it; per-request `ContextInjectionHook`/RAG context
  is assembled only into `ProviderRequest` and is intentionally outside the log.
  Never `append`/`extend`/`clear`/`[:]=` the projections. Append via
  `await session.append(...)` (the only durable path — it writes the session
  store *and* the log; direct `SessionLog.append(...)` is in-memory only and is
  lost on reload); record compaction
  as a **logged projection** (`session.session_log.record_projection(...)`),
  never as an untracked in-place mutation.
- **The loop continues while a response has tool calls** and stops on a
  text-only response (or a stop condition).
- **Python 3.11+** (3.0 raises the floor) — `asyncio.timeout`, `TaskGroup`,
  and the unified `TimeoutError` are allowed.
- **Dependencies earn their place** — a library may be added to core or an
  extra when it removes code or measurably speeds the hot path (e.g. fast JSON,
  Postgres driver); say what it buys in the PR.
- **Observability is OpenTelemetry** — core uses `opentelemetry-api` directly
  (GenAI semantic conventions); hosts configure the SDK and exporter
  (Langfuse, Tempo, Datadog...). No custom observer layer.
- **Durable state is Postgres; InMemory is for tests and dev** — do not add
  SQLite code (it is being removed, spec D13).
- **Multi-tenant safe via per-agent `Context`** — no process-global mutable
  state; each `Agent` owns its own `Context` scope (`linch.Context`) holding its
  service registry, event bus, and effect scope, plus its registries and session
  dict, so N agents run concurrently in one process. Shared services are resolved
  through the `Context` (a scoped IoC container), never module globals.
- **Defaults are chosen on merit, with numbers** — a feature may be on by
  default; an off feature must not tax the hot path (measure with
  `scripts/benchmark_runtime.py --scale`).
- **Public API is exactly `linch.__all__`** — import from the top-level package;
  submodule paths and underscore names are private. `tests/test_public_api.py`
  guards this surface; changing it is allowed in 3.0, silently drifting is not.
- **Only read work runs in parallel** — write/exec tools stay serialized; a
  subagent runs in parallel only when every tool it can use is read-scoped.
- **Durable before side effects** — a checkpoint guarding a tool call is
  written through to the store before the tool runs; no write-back caching of
  sessions, checkpoints, events, or permission decisions.

**3.0 compatibility policy:** the owner is the only 2.x user, so 3.0 breaks
freely. Remove deprecated APIs, legacy hook forms, shims, and readers for
pre-3.0 persisted data (old runs are rejected; start a new run) instead of
keeping parallel paths. Record each removal in one line of
`docs/migration-3.0.md`. Do not add new compatibility shims.

Style (imports, formatting, line length) is enforced by `ruff` — let the linter
handle it rather than hand-tuning. Match surrounding code; don't refactor
unrelated code in the same change.
