# Scale-ready Linch 3.0 — architecture decisions

Status: accepted by the repo owner's delegate (Claude), 2026-10-30. Workers
implement; they do not change these decisions. A worker that finds a decision
wrong stops and reports evidence instead of working around it.

Baseline: `integration/3.0` @ `da9bcd7`. Review that motivated this:
https://claude.ai/artifact/CJHkJQrTK7BbHWqDMGNp6T (section 8).

## Direction

Linch is a headless **agent kernel**: excellent core loop, scales out, easy
to customize through seams and protocols. No UI — Linch Studio was dropped
(owner, 2026-10-31). Work that does not serve kernel quality, scale, or
customization is out of scope.

## Target deployment

A virtual-assistant product embeds Linch in a host service: N stateless API
pods accept messages and stream events; M worker pods run agent loops; Postgres
is the source of truth for sessions, runs, events and leases. Tools are RAG,
SQL, and a few read/write tools with per-tenant dependencies via
`RunOptions(deps=...)`.

Linch stays embeddable, but (owner decision 2026-10-31) it may ship an
optional runtime module: `linch.worker` — claim/renew/release loop over a
leasing RunStore, graceful drain, resume of orphaned runs — so every host does
not rebuild it. The HTTP API and queue choice stay with the host (example
only). Dependencies may be added when they pay for themselves (CLAUDE.md).

Compatibility: 3.0 breaks freely (CLAUDE.md "3.0 compatibility policy").
Pre-3.0 runs and checkpoints are rejected rather than read through legacy
paths; immutable message types are allowed if they remove the SessionLog
deep-copy cost.

Out of scope for 3.0: tool discovery (deferred, measured need first),
cross-process rate limiting in core (host `Limiter` adapter), partial-text
persistence.

## Decisions

**D1 — One owner per run (lease + fencing).** Add an *optional* RunStore
capability, detected with `getattr` like other optional protocol members:
`claim(run_id, owner, ttl_s) -> RunLease` (fails if another live lease exists),
`renew(lease)`, `release(lease)`. A lease carries a monotonically increasing
`epoch`. Every write made under a lease (checkpoint, event, status) is fenced:
the store applies it only when the stored epoch equals the lease epoch and
otherwise raises a typed `LeaseLostError`. Stores without the capability and
runs started without a lease behave exactly as today. Renewal/heartbeat is
driven by the host worker, not by the loop.

**D2 — Postgres is the source of truth.** Ship `PostgresRunStore` under the
existing `[postgres]` extra (asyncpg, same style as `sessions/postgres.py`):
runs table with lease columns, events table with `(run_id, seq)` unique. Lease
claim is a single conditional `UPDATE ... RETURNING`. One shared RunStore
conformance suite (see `/home/duy/Downloads/duy_dev/agent_kit/plans/04-run-store-observer-conformance.md`,
an untracked draft) runs against InMemory, Sqlite, and Postgres (Postgres tests skip
cleanly without `AGENT_KIT_TEST_PG_DSN`, the existing convention in `tests/storage/test_postgres.py`). NoSQL is not used; Redis appears
only in examples.

SQLite stays as the zero-dependency single-host backend (local, CLI, tests,
one-process apps) and keeps passing the conformance suite, but it is not on the
scale path: multi-worker deployments use Postgres, and no further scale work
targets SQLite.

The runtime's hot path is write-heavy (WP1: ~16.5 store calls per turn), and a
checkpoint must be durable before the side effects it guards. Therefore **no
write-back cache** for sessions, checkpoints, events, or permission decisions:
writes stay write-through, and the lever is fewer, smaller, batched writes
(WP8). Reads are already served from the owning process's memory while a run
is leased (single writer ⇒ coherent); the store is read only on cold load,
resume, and stream reconnect. Read caches belong at the tool layer (RAG/SQL
results, embeddings) as host adapters, not in core.

**D3 — Split the loop before adding behavior.** `_run_loop_impl` is split into
phase modules with **no behavior change**, proven by event-trace equivalence on
existing loop/characterization tests. `runner.py` keeps entry points and turn
choreography only. New loop behavior arrives through seams, never new branches
in `runner.py`/`scheduler.py`.

**D4 — One new generic seam now: `loop/pre-finalize`.** Runs at both
finalization routes before finalizers and before any terminal-tool ACK.
Outcomes: accept / continue (append feedback, next turn) / fail. With no
listener the path is byte-identical. `loop/response` and the participant
coordinator from `generic-loop-seams-2026-10-30` wait until the reliability
port needs them.

**D5 — Run-scoped listeners use a run-local dispatcher.** A child `Context`
shares its parent's EventBus, so run code never registers unfiltered listeners
on the agent bus. Leaking a listener across runs is a tenant-isolation defect.

**D6 — Bounded memory per process.**
- `session.background_tasks` drops each task when it completes.
- `Agent` gains an opt-in bound on cached *idle* sessions (LRU; default `None`
  keeps today's behavior). Sessions with an active run are never evicted.
- `SessionLog` read projections stop deep-copying the whole history on every
  read: keep an immutable snapshot cached per log revision, invalidated on
  append/projection. Model-visible state stays a logged projection. The deep
  copy exists so callers cannot mutate the log: first measure its cost
  (50 and 200 turns) and list every in-repo reader; a reader that mutates what
  it reads copies just that part itself. Tests must prove that mutating a
  returned value can change neither the log nor a later read. If no safe design
  beats the deep copy measurably, keep it and report the numbers.

**D7 — Subagents run in parallel.** `SubagentTool` is `scope="exec"` with
`parallel=True`, but the scheduler only parallelizes `scope == "read"`, so
subagents serialize. Do **not** widen the scheduler rule: write-scoped tools
such as `tools/tasks.py` also declare `parallel = True` and share state, so a
scope-independent bool would race them. Principle (owner): **only read work
runs in parallel.** A subagent is "read" only when every tool it can use is
read-scoped; two write-capable subagents in parallel would race on the same
files/DB. So `SubagentTool.parallel` becomes an input-aware callable that
returns `True` only when `subagent_type` resolves to a definition whose
allowed tools are an explicit list, all resolvable, all `scope == "read"`.
Anything else — general-purpose (full tool access), unknown type, inherited
or unresolvable tools, background mode — returns `False` (fail closed).
`scheduler.py` stays unchanged; result ordering stays provider order.

**D8 — Failures are visible.** A run-store/session-store write failure during a
run ends the run as `failed` with a typed `ErrorEvent`, never a silent stop.
`LeaseLostError` ends the run without further writes. Resuming a run that is
already terminal raises a typed error naming its status instead of returning
silently (3.0 breaking change, documented in `docs/migration-3.0.md`).

**D9 — Stream reconnect by sequence.** Every persisted event has a per-run
monotonic `seq` in every store; `load_events(run_id, after_seq)` is part of the
conformance suite. The production guide documents reconnect-by-seq.

**D10 — Measure before claiming "faster".** `scripts/benchmark_runtime.py`
gains concurrency cases (fake provider, fake RAG/SQL tools, no network):
200 and 1,000 concurrent sessions × short multi-turn runs. Report p50/p99 run
latency, max event-loop lag, peak RSS per session, checkpoint bytes per turn,
and store writes per turn. A baseline is recorded first; each work package
reports its delta.

## Work packages

Wave 1 (parallel, disjoint files):

| WP | Branch | Decisions | Owns files |
|---|---|---|---|
| 1 | `scale/bench-baseline` | D10 | `scripts/benchmark_runtime.py`, its tests |
| 2 | `scale/runtime-bounds` | D6, D7 | `agent.py`, `session.py`, `session_log.py`, `tools/subagent.py` |
| 3 | `scale/loop-split` | D3, D4, D5 | `loop/` (runner split, new seam), hooks/kernel wiring for the seam |
| 4 | `scale/durable-runs` | D1, D2, D9 | `run_store.py` (or a new `run_stores/` package), Postgres store, conformance tests; move `SqliteRunStore.save` serialization off the event loop |

Wave 2 (after WP3 merges): WP5 failure surfacing + lease-lost handling in the
loop (D8); WP6 worker-pool recipe in `examples/recipes/` and a "scale out"
section in `docs/usage/production.md`; WP7 reliability port onto the seams;
WP8 hot path, driven by the WP1 baseline
(`docs/superpowers/measurements/2026-10-30-scale-baseline.md`):
- One process saturates near 35 four-turn runs/s with 2 ms fake I/O; at 1,000
  concurrent sessions the in-memory case shows 4.3 s event-loop lag. CPU per
  turn grows with history (1 × 50 turns ≈ 24 ms/turn).
- Checkpoints are written ~6 times per turn and grow from 2.3 KB to 39 KB over
  50 turns although `RunCheckpoint` holds no message history: find which field
  grows and bound it. 16.5 store writes per turn: batch where the phase model
  allows.
- `SessionLog` getters deep-copy (3.2 ms/read at 50 turns, 14.8 ms at 200; see
  `docs/architecture/session-log-performance.md`). The copy stays; cut the
  number of whole-history reads per turn instead — e.g. an incremental history
  digest maintained on append replaces re-fingerprinting `full_history` at
  every checkpoint.
- Profile (cProfile) the 200 × 4 and 1 × 50 cases first; every change reports
  its delta against the baseline.
- Add Postgres cases to the `--scale` benchmark (skipped without a DSN) and
  report store round-trips and bytes per turn on Postgres, since that is the
  production backend.

## Wave 3 — harness kernel surface (owner target, 2026-10-31)

Target picture: one Harness Kernel (shared context/service registry, typed
events/lifecycle, dependency resolution, plugin mount/unmount, run state and
trace boundary) with plugin groups around it — Model Adapter, RAG Connector,
Tool & Skill Pack, Session & Memory, Policy & Security, Scheduler/Sub-agent,
Observability — and **Domain Profiles** selecting a plugin set per domain so
one runtime serves many domains. Gaps against today's code:

| Target | Linch today | Gap → WP |
|---|---|---|
| Plugin mount/unmount + dependency resolution | `Context`, `Disposable`, scattered per-kind registration APIs; no plugin unit (0 hits for a plugin/use API); `Agent.__init__` takes 70 parameters | WP10: `Plugin` protocol (name, version, provides, requires, `setup(ctx) -> Disposable`), `agent.use(plugin, config)`; a plugin activates when its required services exist and is disposed when one disappears; built-in tools/hooks/providers/memory/skills become plugins; most constructor parameters move into plugins/profile |
| Domain Profile | `DeepAgentProfile` only (authority/retention presets) | WP11: versioned declarative profile (role/prompt, knowledge scope, plugin set + config, tool permissions, workflow/subagents, output schema, limits) selected per run (`RunOptions(profile=...)`); profile id + version recorded in the RunContract so resume pins the version (controlled rollout/rollback) |
| Typed events / lifecycle | typed run events, string-named EventBus, separate HookDispatcher | WP12: one typed lifecycle (run start/end, turn request/response, tool pre/execute/post, pre-finalize) on the kernel bus, keeping typed security ordering; built on WP3 seams |
| Run state / trace boundary | run_id, OTel observer; no `trace_id` anywhere | WP12: a trace id per run propagated into events, store rows, `ToolContext`, logs and OTel spans |
| Policy & Security (RBAC, approvals) | rule-based permissions + durable approvals; no per-run principal/role | WP13: per-run principal (tenant, user, roles) in `RunOptions`, permission rules over roles, approval routing as a plugin |
| RAG Connector with grounding | memory/RAG hooks, citation events in reports | WP14: RAG connector protocol returning sourced chunks with ids; grounding check as a pre-finalize plugin (answer must cite retrieved sources) |

Order: WP3 → WP9b → WP10 → WP11 → WP12 → WP13/WP14 (parallel). WP5/WP6/WP8 run
alongside where files do not overlap. Plugins and profiles are tested with
`linch.evals` business scenarios before a profile version is promoted.

### Ideas borrowed from DeepSeek Harness (ideas, not code)

Read at `deepseek-ai/deepseek-harness@ce7073d` (TypeScript, Cordis-based,
single-user product: no multi-worker lease or Postgres story, so the scale
work above stays ours). Docs paths are relative to that repo.

| Idea | Where (dsh) | Use in Linch |
|---|---|---|
| Everything, including the loop, tool registry and session log, is a plugin; a plugin declares `inject` (required services) and activates only when they exist | `docs/architecture.md` "Cordis", `docs/cordis-primer.md` | WP10 `Plugin` with `requires`/`provides`; built-ins become plugins |
| Activation audit: a mount fails on missing services or on a plugin leaking a service outside its scope | `packages/preset/agent-preset-registry/README.md` | WP10: reject a plugin that registers outside its scope (tenant isolation check) |
| Profiles = ordered layers of config rows with stable ids; later layers replace a row by id or insert rows; `--dump-config` prints the composed tree | `docs/architecture.md` "Profiles and bundles" | WP11: profile = base bundle + domain layer + tenant patch; `profile.resolve()` shows the final tree |
| Preset registry with revisions: an edit retires the old revision, running agents keep theirs until released; failed definitions stay visible but unbindable | `agent-preset-registry/README.md` | WP11; Linch goes further and pins profile id+revision in the RunContract |
| Scoped registry layers: global layer + per-scope overlay; allow/deny tool restrictions intersect along the scope chain, a scope's own tools stay exempt | `docs/subsystems/scope.md`, `docs/subsystems/tools.md` §ToolRestriction | WP11/WP13 tool permissions per profile, role, and subagent |
| Dispatch mode is part of each event's contract (`emit`/`waterfall`/`parallel`/`serial`/`bail`, `@mode` tag); a generated catalog lists producers/consumers and CI checks it is fresh | `docs/cordis-primer.md`, `docs/event-producer-consumer.md` | WP12 typed lifecycle + generated event catalog; also stops doc drift found by the WP9 audit |
| Turn/step lifecycle: `turn/start` → `agent/pre-step` (accept/rewrite/reject) → `step/start` → request → `llm/stream` → `tools/*` → `step/end` → `agent/turn-stopping` (serial, may stop) | `docs/architecture.md` "Turn flow" | WP3/WP12 naming; `turn-stopping` is our `pre-finalize` |
| Session projections are pure fold units (`init`, `apply(state, event)`); an uninterested unit returns the same reference (zero work); state is cached as `(session, key, version, seq, value)` and rebuilt by checkpoint + tail replay | `docs/subsystems/session-projection.md` | WP8: maintain `provider_view`/`full_history` incrementally instead of deep-copying the whole log per read; smaller checkpoints |
| Storage formats are versioned generations; each `vN → vN+1` migration is its own module; core reads only the current format | `docs/architecture.md` "Session log" | WP9b: core reads only 3.0 data; any future upgrade is a separate migration step, never a legacy branch in core |
| Permission presets: named bundles of sandbox mode + approval policy | `docs/subsystems/permission-presets.md` | WP13: presets per role |

Not adopted: vendoring Cordis, TypeScript declaration merging, HMR, plugin
market, desktop/web UI, Remote/Typert gateway, JSONL file sessions.

## Rules for every worker

- Own git worktree and branch from `integration/3.0`; never commit to
  `integration/3.0`, `master`, or `main`; never push.
- Touch only the files your WP owns; if another file must change, keep it
  minimal and say why in the report.
- TDD: a failing test first for each behavior change; pure refactors prove
  equivalence with existing tests.
- Follow `CLAUDE.md`/`AGENTS.md` constraints (async only, Python 3.10, public
  API = `linch.__all__`, opt-in zero overhead, registrations return
  `Disposable`, no module globals).
- Gates, run from the worktree with the main checkout's venv
  (`/home/duy/Downloads/duy_dev/agent_kit/.venv/bin/`): `python -m pytest -q`,
  `ruff check .`, `ruff format --check .`, `pyright`. All green before the final
  commit. If a gate fails for a reason outside your WP, report it; do not fix it.
- Final report (under 250 words): branch, HEAD SHA, gate results with counts,
  files changed, decisions implemented, anything deviating from this spec.
