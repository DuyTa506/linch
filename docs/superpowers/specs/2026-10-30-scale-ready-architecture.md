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
not rebuild it. The HTTP API stays with the host; the job queue is PgQueuer
inside `linch.worker` (D14). Dependencies may be added when they pay for themselves (CLAUDE.md).

Compatibility: 3.0 breaks freely (CLAUDE.md "3.0 compatibility policy").
Pre-3.0 runs and checkpoints are rejected rather than read through legacy
paths; immutable message types are allowed if they remove the SessionLog
deep-copy cost.

Out of scope for 3.0: cross-process rate limiting in core (host `Limiter`
adapter), partial-text persistence. Tool discovery moved back in scope (owner
decision 2026-10-31): WP19, a Wave 3 plugin after WP10.

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

~~SQLite stays as the single-host backend.~~ Superseded by D13 (2026-10-31):
SQLite is removed; durable state is Postgres only, InMemory serves tests and
dev.

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

Status (2026-10-31): WP3 showed the seam is needed (terminal-tool ACK runs
before legacy hooks; `BeforeFinalAnswer` skips the forced-final route), but
its first implementation (`scale/loop-split` 54d9951, 748 + 2,056 test lines)
was not merged. It returns as a slim typed `turn-stopping` event (serial,
veto) when WP7 or WP14 first needs it: a `continue` appends its feedback
through `session.append` before the next turn, so no separate pending state
is persisted; budget about 200 source lines.

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

**D11 — Observability is OpenTelemetry.** Core depends on `opentelemetry-api`
only; without a host-configured SDK the API is a no-op. The kernel opens spans
where I/O happens: `invoke_agent` per run, one step span per turn, `chat` per
provider call, `execute_tool` per tool, one span per compaction. Store calls
are not spans: they feed a `linch.store.duration` histogram (attribute: op).
Attributes follow the GenAI semantic conventions (`gen_ai.*`, still
experimental, so every attribute name lives in one module) plus `linch.*`;
metrics are `gen_ai.client.token.usage` and `gen_ai.client.operation.duration`.
Prompt and tool content is recorded only when the standard
`OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` is true. Tracer and meter
are resolved through the agent's `Context` (default: the global providers), so
tests and tenants can inject their own without touching process globals. A run's trace id is the OTel trace id; `traceparent` travels in
job payloads and into subagents. Removed: the Observer protocol,
`ObserverDispatcher`, `LoggingObserver`, `SpanCollector`,
`OpenTelemetryObserver`, `RunTelemetryHook`; tests use the SDK's
`InMemorySpanExporter`. The no-op cost is measured with `--scale` and reported.

**D12 — Memory is files.** Each scope (tenant, user, agent) owns
`/memories/<scope>/` with `MEMORY.md` as its index (at most 200 lines) and one
file per fact, stored on the filesystem backend (Postgres in production). The
index is loaded into the system prompt when a session opens (a stable,
cacheable prefix); the model reads and writes memory with the existing file
tools, so writes are write-scoped, serialized, permission-checked and logged.
Semantic recall is an optional plugin (extra `mem0`: mem0 `AsyncMemory` scoped
by `user_id`/`agent_id`/`run_id` on pgvector), off by default because every
`add` calls an LLM. Removed: the keyword memory stores, `TieredMemoryStore`,
`MemoryContextBuilder`, memory lifecycle, memory tools and hook.

**D13 — Durable state is Postgres only.** Backends are Postgres (production)
and InMemory (tests, dev). Every SQLite store (sessions, runs, files, memory,
mailbox, scheduling) and `storage/_executor.py` are removed; conformance suites
run InMemory + Postgres. Local durable development uses Docker Postgres or
pgserver. Cost accepted by the owner: durability needs Postgres even on one
machine.

**D14 — Run dispatch and cron use PgQueuer.** `linch.worker` (extra `worker`)
runs PgQueuer on the stores' asyncpg pool: one job per run, dequeued with
`FOR UPDATE SKIP LOCKED` and woken by LISTEN/NOTIFY; a per-entrypoint
`concurrency_limit` (enforced in the database across the fleet) caps
per-provider or per-tenant concurrency; retries use
`DatabaseRetryEntrypointExecutor`; cron uses `@schedule`. The D1 lease still
fences writes — the queue only dispatches. Core never imports PgQueuer. The
hand-written scheduling package (cron parser, stores, loop) is removed;
`schedule_*` tools enqueue jobs. NOTIFY takes a global lock at commit: WP6
measures it, and stream readers can fall back to polling `run_events` by seq.

**D15 — msgspec for persisted types, only if measured.** The hand-written
event/receipt/checkpoint codecs (about 1,330 lines of
`to_dict`/`from_dict`/validation) move to msgspec Structs (a tagged union for
events) only if `--scale` shows a clear encode/decode CPU win; otherwise they
are consolidated without the dependency. Semantic validation stays
hand-written; fingerprints stay deterministic.

**D16 — Small kernel, extensions outside it.** The kernel is `kernel/`,
`loop/`, tools, providers, sessions, run store, permissions, hooks and
filesystem. Workflow, evals, deep_agent, reports, the CLI scaffold and the
mailbox move to `linch.ext.*`: imported explicitly, absent from
`linch.__all__`. Gemini moves to `google-genai` (the `google-generativeai` SDK
is deprecated).

Design page for D11–D16 (approved by the owner 2026-10-31):
https://claude.ai/artifact/2rwtNaRU62hv6Dk9wG3h58

## Work packages

Wave 1 (parallel, disjoint files):

| WP | Branch | Decisions | Owns files |
|---|---|---|---|
| 1 | `scale/bench-baseline` | D10 | `scripts/benchmark_runtime.py`, its tests |
| 2 | `scale/runtime-bounds` | D6, D7 | `agent.py`, `session.py`, `session_log.py`, `tools/subagent.py` |
| 3 | `scale/loop-split` | D3, D4, D5 | `loop/` (runner split, new seam), hooks/kernel wiring for the seam |
| 4 | `scale/durable-runs` | D1, D2, D9 | `run_store.py` (or a new `run_stores/` package), Postgres store, conformance tests; move `SqliteRunStore.save` serialization off the event loop |

Merged into `integration/3.0`: WP1, WP2, WP4, WP3 (loop split only; see D4
status), WP9a, WP9b, WP16, WP17, WP15, WP6, WP5, WP8 (f23d726, plus 33cb316: every terminal commits its pending events in the same statement as its status). WP18 baseline (90720e4, report `docs/superpowers/measurements/2026-10-31-load-test.md`): stable 6.7 runs/s with one worker, 13.7 with four, no gain at eight, measured on a10aa04, before WP8. One contributor, every job rechecking the schema under a shared advisory lock (76 waiters at M8), is removed by 2ebbc1f (merged 498627f: one verified store pair per worker, zero schema statements per job); on current code M4 and M8 both reach an offered 16 runs/s, so the gate rerun re-measures the knee. Resilience gate open on two defects: a takeover of a run that has no checkpoint yet fails it without terminal events, and a three-second DB outage makes workers exit with jobs left. Both defects fixed in ce6ac56 (adoption writes the initial checkpoint in the same statement as pending -> running and startup preparation is durable, so takeover completes the run; worker setup failures commit Error + Result atomically; Worker.run reconnects with fresh PgQueuer managers): SIGKILL before the first checkpoint 146/146, 3 s DB blip 23/23. WP7 merged as 15859f9: one generic serial-veto turn-stopping seam, reliability as listeners on it, prerequisites as a PreToolUse hook, success derived from final batch results, scheduler.py untouched; src +548; clean-run cost +1.0% Postgres / +2.0% memory elapsed over three interleaved pairs, writes and checkpoint bytes unchanged. Wave 2 is complete. WP18 gate merged as 5d029c9 (report `docs/superpowers/measurements/2026-10-31-scale-gate.md`, measured 8d32b51): every chaos acceptance line passes (SIGKILL 1489/1489 with all 34 orphans reclaimed within 5.945 s; 3 s outage 1395/1395 with zero worker exits; drain 1479/1479 with zero post-signal claims). Knees (stable/first-bad runs/s): M1 20/24, M2 24/32, M4 24/32, M8 40/48, so throughput does not scale with workers (efficiency 61/31/24%). At a fixed 16 runs/s, fleet worker CPU per run grows 2.6x and Postgres CPU per run 3.8x from M1 to M8 while WAL per turn stays about 28 KB: coordination cost grows with workers times fleet rate (suspected: every worker wakes and dequeues on every queue insert and delete notification). PgBouncer transaction mode stays correct but loses LISTEN (dispatch p99 40 ms -> 4.3 s) and saved no backend connections in this fixture; guidance is direct or session pooling for workers, no code change. WP8 r8-r11 merged (last d6eed0b): DELETE wakes filtered on unlimited queues, commits per turn halved, M8/M1 cost now 1.55x worker CPU and 1.47x Postgres CPU per run (accepted; remaining levers under "Later optimization"), chaos at the new knee SIGKILL 1903/1903 and 3 s outage 1835/1835; transient store errors during a run go through `durable_call` and leave the run to takeover instead of failing it. Wave 3 merged: WP10a (`linch.ext` split), WP10b (Gemini on google-genai), WP10c (plugin protocol, `agent.use`, Mailbox/AskUser plugins), WP13 (per-run principal, role rules), WP14 (RAG connector, grounding check). WP19 merged 1feeba9 (ToolSearchPlugin mounted by default, generic `tools/resolve` veto before validation; 60 tools: about 2,182 -> 270 prompt tokens per request), WP11 merged 5710e28 (versioned layered `Profile` per run, pinned id/version/hash in the RunContract, `enqueue_run(profile=, principal=)` and `Worker(profiles=)`), WP12 merged f8f116c (typed lifecycle catalog in the kernel with emit/serial/waterfall/hook modes, generated event catalog, trace id on run events, `ToolContext.trace_id` and `TraceIdFilter`). Integration f8f116c: 2772 passed, src 44,248 lines. Docs pass merged a36a0be (module tree, indexes, CHANGELOG 3.0, stale-reference test; Session.notify/append/update_meta go through durable_call) and Google-style public API docstrings merged d307809/34b1451 (documentation only, AST-equivalent). Store parity merged f1155af: InMemory and Postgres stores honor one contract (all-or-nothing batches, Postgres commit_inbox decodes before commit, guarded stale release, KeyError for missing sessions, detached copies), each rule a conformance test over both backends. Integration 2841 passed. Review: stacked PRs `linch-3.0/01..28` against master.
Order (one merge at a time):

| WP | Branch | Decisions | Scope |
|---|---|---|---|
| 9b | `scale/wire-3` | 3.0 policy | one `WIRE_VERSION = 3`, reject pre-3.0 data, delete legacy readers, loop legacy items (running) |
| 16 | `scale/file-memory` | D12 | memory package, memory hook, memory exports/docs (may run beside WP9b: disjoint files) |
| 17 | `scale/postgres-only` | D13 | delete SQLite stores and executor; conformance on InMemory + Postgres |
| 15 | `scale/otel` | D11 | `observability/`, `RunTelemetryHook`, spans at loop phases |
| 5 | `scale/failures` | D8 | failure surfacing, `LeaseLostError` in the loop |
| 6 | `scale/worker` | D14 | `linch.worker` on PgQueuer, scheduling package replaced, "scale out" section in `docs/usage/production.md` |
| 8 | `scale/hot-path` | D15 + below | profile-driven hot path |
| 7 | `scale/reliability` | D4 | reliability port onto the seams (brings the slim `turn-stopping` event) |
| 18 | `scale/load-test` | acceptance | multi-process load test of the target deployment (enqueuer + M workers + Postgres behind injected network latency): capacity knee, bottleneck, chaos (worker kill, DB blip, drain). Built and baselined now on integration; rerun after WP8 and WP7 as the scale-ready gate. Owner decision 2026-10-31: no hand-written infrastructure where a maintained tool exists, so faults and latency come from Toxiproxy, process CPU from psutil, and percentiles and exactly-once checks from SQL; harness at most 600 lines |
| 19 | `tools/search` | Wave 3, right after WP10 | slim tool search as a plugin (section below); replaces the frozen `wip/tool-discovery-freeze-20261030` design |

WP19 tool search (owner decision 2026-10-31: slim, a plugin, opens on its own
past a size threshold; modelled on Claude Code's deferred tools):
- A WP10 plugin, `ToolSearchPlugin(max_tools=30, max_skills=50)`, mounted by
  default; unmounting it restores today's behavior. At or below the thresholds
  requests stay byte-identical: no `tool_search` tool, nothing deferred.
- Above `max_tools` offered tools (native + MCP), every tool without
  `always_load = True` (duck-typed attribute) is deferred: the `tool_search`
  description lists deferred names only, no schemas. `tool_search(query)`
  takes `select:a,b` or keywords (plain scoring over name + description, no
  embeddings, no dependency), returns the matched schemas and loads them; they
  join the offered tools from the next request for the rest of the session.
  Above `max_skills`, the skill listing is deferred the same way and search
  returns skill name + description (the Skill tool still loads the body).
- Search changes what is offered, never what is allowed: permissions, hooks
  and validation govern loaded tools as before. No activation or catalog
  policy, no activation records, no contract fingerprints.
- The loaded set survives resume and later runs of the session. Derive it
  from durable history only if that is provably sufficient (see WP8's
  loop-guard finding about bypass paths); otherwise keep a bounded name set in
  checkpoint/session state.
- Budget: one public name (the plugin) plus the `always_load` attribute, about
  400 lines of src. Measure prompt tokens per request with 60 tools before and
  after, and how often the offered tool list (the provider cache prefix)
  changes. Reuse from the frozen branch only what fits.

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
- Session projections as pure folds with a cached state and tail replay
  (DeepSeek Harness idea below), so reads and checkpoints cost O(new entries).
- D15: msgspec codecs only with a measured win.

Status (2026-10-31): merged as f23d726. Postgres writes are one fenced
statement each (owner and epoch in the WHERE, events through a data-modifying
CTE, seqs from `last_event_seq` / `last_message_seq`), checkpoint and events are
one required RunStore write, and store-owned pools skip the reset query.
Postgres 200 x 4 went from 94.9 to 15.5 protocol cycles per turn and from
12.1 to 19.9 runs/s; memory 200 x 4 from 23.0 to 30.8; checkpoints at turns
0/24/49 are 1.6/2.2/2.8 KB (the residual is ~25 B per distinct tool call in
the digest-keyed loop guard). msgspec was dropped (under 1%). src +298 net.
See `docs/superpowers/measurements/2026-10-31-hot-path.md`.

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
| Run state / trace boundary | run_id, OTel observer; no `trace_id` anywhere | WP15 (D11) makes the run's trace id the OTel trace id; WP12 propagates it into events, store rows, `ToolContext` and logs |
| Policy & Security (RBAC, approvals) | rule-based permissions + durable approvals; no per-run principal/role | WP13: per-run principal (tenant, user, roles) in `RunOptions`, permission rules over roles, approval routing as a plugin |
| RAG Connector with grounding | memory/RAG hooks, citation events in reports | WP14: RAG connector protocol returning sourced chunks with ids; grounding check as a pre-finalize plugin (answer must cite retrieved sources) |

Order: after Wave 2, WP10 (with D16) → WP11 → WP12 → WP13/WP14. Plugins and profiles are tested with
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

## Later optimization (owner 2026-10-31: note, do not chase now)

- Broadcast LISTEN floor: after WP8 r8, per-run cost at 8 workers is still about 1.5x one worker, because Postgres opens an internal transaction per listening backend per notification. Options when needed: fewer listeners per database (one dispatcher per host feeding local workers), or short jittered polling instead of LISTEN.
- Write commits per turn (about 18 after r8) and WAL flush waits set the single-database ceiling; batching more writes per turn is the next lever.
- Worker CPU per run (about 35-40 ms for a 4-turn run; `_json_safe`, checkpoint save and telemetry wrappers show in the profile).
- PgBouncer transaction mode: a separate direct connection for the queue LISTEN, only if a deployment needs the connection savings.

## Rules for every worker

- Own git worktree and branch from `integration/3.0`; never commit to
  `integration/3.0`, `master`, or `main`; never push.
- Touch only the files your WP owns; if another file must change, keep it
  minimal and say why in the report.
- TDD: a failing test first for each behavior change; pure refactors prove
  equivalence with existing tests.
- Follow `CLAUDE.md`/`AGENTS.md` constraints (async only, Python 3.11+, public
  API = `linch.__all__`, opt-in zero overhead, registrations return
  `Disposable`, no module globals).
- Gates, run from the worktree with the main checkout's venv
  (`/home/duy/Downloads/duy_dev/agent_kit/.venv/bin/`): `python -m pytest -q`,
  `ruff check .`, `ruff format --check .`, `pyright`. All green before the final
  commit. If a gate fails for a reason outside your WP, report it; do not fix it.
- Final report (under 250 words): branch, HEAD SHA, gate results with counts,
  files changed, decisions implemented, anything deviating from this spec.
