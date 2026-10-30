# Scale-ready Linch 3.0 — architecture decisions

Status: accepted by the repo owner's delegate (Claude), 2026-10-30. Workers
implement; they do not change these decisions. A worker that finds a decision
wrong stops and reports evidence instead of working around it.

Baseline: `integration/3.0` @ `da9bcd7`. Review that motivated this:
https://claude.ai/artifact/CJHkJQrTK7BbHWqDMGNp6T (section 8).

## Target deployment

A virtual-assistant product embeds Linch in a host service: N stateless API
pods accept messages and stream events; M worker pods run agent loops; Postgres
is the source of truth for sessions, runs, events and leases. Tools are RAG,
SQL, and a few read/write tools with per-tenant dependencies via
`RunOptions(deps=...)`.

Linch stays a library. No daemon, server, queue, or Redis dependency enters
core. Linch ships *mechanisms* (lease, fencing, conformance, seams, bounds);
the topology (queue, worker pool, HTTP) is the host's, shown in a recipe.

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
