# Linch SDK Roadmap

This file is a lightweight roadmap/status page **and** the implementation plan for
Linch as a **pure-mechanism, embeddable agent runtime SDK**.

---

## North star

Linch ships the **harness**, not the agent. It provides **mechanisms, protocols, seams,
and primitives** — never policy, prompts, domain tools, or product UX. A coding agent is
*one* application an embedder builds on Linch; a support agent, a research agent, and a
data agent are equally first-class. If a capability only makes sense for one domain, it
belongs in the embedder's app, not in Linch.

**The discriminating rule for every line of this roadmap:**

> Is it a *mechanism* every embedded agent needs regardless of domain, or a
> *behavior/policy* specific to one kind of agent? Mechanism → SDK. Behavior → out.

When a useful capability is domain-flavored, the SDK ships the **generic seam** and lets
the embedder supply the domain piece — exactly how `execution.py` exposes an
`ExecutionBackend` protocol (`LocalBackend`/`DockerBackend`) rather than hardcoding one
runner.

**Hard constraint:** every item is opt-in and additive. With defaults unchanged, the loop
stays byte-identical — the discipline already used for `loop_guard`, `compaction_ladder`,
verification gates, and `result_offload`.

---

## Two loops on one axis: open ↔ closed

Every agent runs the same cycle — **Discovery → Planning → Execution → Verification →
Iteration** — alone or fanned out across an orchestrator + specialists. What decides
*which you can afford to run* is a single axis, **open ↔ closed**, defined by **who
authors the path**:

| | **Closed loop** | **Open loop** |
|---|---|---|
| Path author | **human** writes it first | **model** discovers it at runtime |
| Shape | bounded, set steps, eval at each step | wide, exploratory, roams and builds new |
| Cost | cheap, repeatable, improves every run | burns tokens, needs a large budget |
| Honesty | the standard keeps it honest | loose standard = a fast slop machine |
| **Linch surface** | **`run_workflow`** (core mechanism) | **`create_deep_agent`** (experimental preset) |

`run_workflow` is the closed loop: a deterministic, journaled, resumable fleet script the
embedder authors. `create_deep_agent` is the open loop: a coordinator that owns the goal
and lets the model author the path. *Single-vs-fleet is an independent axis* — both
surfaces fan out; the line that separates them is open-vs-closed.

**The SDK's job is the control surface that lets an embedder dial any agent along this
axis** — and Linch already owns both knobs:

- **Budget (`RunBudget`)** — the *cost line*. Propagates across the whole subagent tree by
  reference; `wf.budget` shares it. This is the "normal vs unlimited budget" control.
- **Verification + evals (`verification.py`, `ScorerVerifier`, `evals/`)** — the *standard
  that keeps it honest*. It stops an open loop from becoming a slop machine and makes a
  closed loop improve each run.

**Design consequence (pure-mechanism):** Linch ships the *mechanism* for each cycle stage
(Discovery → `ContextBuilder`/`memory`; Planning → task DAG; Execution → tools;
Verification → `verification.py`; Iteration → the loop + verifier retry) and the two
control knobs — but it must **never** bake the DPEVI cycle in as policy. The cycle is
assembled by a preset (`deep_agent`) or the embedder, not the core loop.

**Roadmap implications:**

1. **Unify the control surface across both loops.** Budget and verification must apply
   *identically* to `run_workflow`, `create_deep_agent`, and any hand-rolled loop. Confirm
   `wf` exposes per-step verification (not just budget) so a closed loop can eval at each
   step (panel 3's "eval at each step"); confirm a workflow step can run a `Verifier`/
   `ScorerVerifier`. *Verify:* a workflow step failing its scorer surfaces a typed failure;
   a deep-agent run and a workflow run charge the same shared `RunBudget`.
2. **Graduate the open loop from "experimental."** `create_deep_agent` is open by nature,
   so its safety rails are the difference between "runnable" and "slop machine." But a
   *default budget number* is an arbitrary cost guess and a *default verifier* is a domain
   quality standard — both are policy a pure-mechanism SDK must not own (and forcing them
   would break "default byte-identical"). So instead of fabricating defaults, make the
   rails **first-class and discoverable**: `loop_guard` stays on by default (a domain-
   agnostic mechanism); `budget=`, `verifiers=` (auto-wrapped into the hooks layer),
   `max_verification_retries=`, and `max_turns=` are explicit, documented parameters on
   `create_deep_agent`, with an "Open-loop safety rails" docstring telling the embedder to
   set them. *(Done — `deep_agent/factory.py`.)*
3. **Keep `run_workflow` the closed-loop reference.** Its journaling/resume + deterministic
   replay are the "cheap, repeatable, gets better every run" guarantees — protect them as a
   versioned contract (ties into Phase 5 serialization hardening).
4. **Label the presets honestly.** `run_workflow` = supported core mechanism (closed loop);
   `create_deep_agent` = experimental open-loop preset/example, not a product direction —
   consistent with the pure-mechanism stance.

---

## Where Linch already leads

This roadmap is derived from a section-by-section study of the `learn-claude-code`
harness curriculum (s01–s20), **filtered to mechanism only** and measured against Linch's
actual code. Linch already matches or exceeds the reference on the core runtime:

- **Loop core** — `loop/` split, loop guard, max-turns, budgets, verification gates;
  continuation keys off tool-use *block presence*, not a `stop_reason` string.
- **Tool scheduling** — `scheduler._partition_batches` already does order-preserving
  consecutive-batch partitioning with `ResourceAccess` conflict detection, concurrency
  caps, retry, timeouts, `result_offload`.
- **Permissions** — `PermissionEngine` + **durable HITL decisions in the run checkpoint**.
- **Hooks** — unified `HookDispatcher` with typed `HookResult`/`HookContext` + adapters.
- **Memory storage** — `MemoryStore` protocol, Sqlite/Postgres/keyword/tiered stores.
- **Compaction** — ladder (COW `micro_compact` + forced-compaction circuit breaker).
- **Task model** — `Task`/`TaskPatch` already carry the full dependency graph
  (`owner`, `blocks`, `blocked_by`, edge mutation, `active_form`).
- **Subagents** — child sessions, retain+continue, background workers with notification
  drain, `WorkerHandle` registry, budget inheritance.
- **Provider-agnostic** — OpenAI/Anthropic/Gemini/llama.cpp behind one interface +
  capability downgrade.
- **Embedder testing** — `ScriptedProvider`, `evals/`, deterministic harness.
- **Durable resume** — `RunCheckpoint`/`SqliteRunStore`.

The gaps below are domain-agnostic runtime mechanisms layered on these primitives.

---

## Out of scope (deliberately left to embedders)

These appear in the reference curriculum but are **coding-product policy**, not SDK
mechanism. Linch will *not* ship them; an embedder builds them on the primitives below:

- Git **worktree** provisioning — coding/VCS-specific. The SDK ships a generic
  filesystem-isolation seam (2.2); git-worktree is one embedder implementation.
- **Background bash** as a special case — the SDK ships background-*any-tool* (2.3); Bash
  is just one caller.
- **TodoWrite** UX and planning-drift nudges — assistant ergonomics.
- Coding **system prompts**, git-aware behaviors, and an assumed Read/Edit/Write coding
  toolset as the *default* agent. (The built-in fs/shell tools remain as domain-neutral
  primitives and `deep_agent` remains an optional example — neither is extended as a
  product direction.)
- **Autonomous coding fleets** and **scheduled coding tasks** — choreography/policy the
  embedder composes from the coordination + scheduling primitives (2.x, 3.3).

---

## Implementation plan (phased)

Each item lists **why**, **where** (modules), **approach**, and **verify** (the success
check to loop against, per the repo's TDD rules).

---

### Phase 1 — Runtime resilience & cost

Pure runtime mechanisms; no new subsystems. Highest leverage, fully self-contained.

#### 1.1 Fork-mode subagents (prompt-cache sharing) — *mechanism*
- **Why:** every child gets fresh context today — correct for isolation, expensive for
  fan-out. A cache-friendly forked prefix makes any parallel agent tree cheaper.
- **Where:** `subagents/` (`SubagentTool`, `RunSubagentArgs`, `_drive_child`),
  `AnthropicProvider` (already supports `prompt_cache=True`).
- **Approach:** `mode="fork"` builds a byte-identical cache prefix from the parent
  `provider_view` (system + tools + model + message prefix + thinking config identical);
  clone the parent `file_read_tracker` into the child `ToolContext`.
- **Verify:** a forked child over a long parent context yields cache-read tokens > 0 on a
  stub caching provider; `mode="normal"` still produces a fresh prefix.
- **Status: done.** `RunSubagentArgs(fork=True)` (and `wf.agent(..., fork=True)`) seeds the
  child with the parent's `provider_view`, system blocks, tools, and `file_read_tracker`,
  so the request prefix is cache-identical; default (`fork=False`) stays isolated. Exposed
  as a boolean rather than `mode="fork"` (KISS). 2 tests assert the seeded prefix + cloned
  tracker structurally (a live cache-read count depends on the provider).

#### 1.2 Compaction fidelity: post-recovery + LLM-free rungs — *mechanism*
- **Why:** forced compaction is summary-only and lossy for in-flight work.
- **Where:** `compaction.py`, `CompactionLadder`, `memory/`.
- **Approach:** three additive rungs, all domain-agnostic —
  1. **Post-compaction recovery:** after summarizing, re-attach the most recent *tool
     results* and any caller-pinned context (generalized from the reference's
     "re-read files" — Linch keys off `file_read_tracker`/recent `ToolResultBlock`s, not a
     coding assumption).
  2. **`sessionMemoryCompact`:** try an LLM-free summary from `MemoryStore` before paying
     for the LLM summary.
  3. **Snip rung:** COW middle-message elision (keep head + recent tail) between
     `micro_compact` and forced compaction.
  Switch the proactive trigger to a token threshold
  (`provider.context_window(model) − max_output_tokens − buffer`); add summary-prompt
  guardrails (`TEXT ONLY, no tools`, `<analysis>`/`<summary>` tags).
- **Verify:** after a forced compaction, recent tool results survive in `provider_view`;
  the memory rung makes zero provider calls when memory suffices.
- **Status: done (scoped to the one real gap).** Audited against the existing ladder and
  found most of this sub-item already shipped, so — per YAGNI/KISS — only the genuine gap
  was built:
  - **Post-compaction read-tracker reset (new).** A compaction (micro elision *or* forced
    summary) can remove a file's contents from `provider_view` while
    `session.file_read_tracker` still records it as read; the `Edit` tool gates on
    `has_read`, so a stale entry would permit a *blind* edit on content the model can no
    longer see. `reset_read_tracker_after_compaction` (`compaction.py`) clears the tracker
    after a compaction, gated on `CompactionLadder(reset_read_tracker=True)` (ladder
    default). Wired at the proactive (`loop/runner.py`) and reactive (`loop/streaming.py`
    ladder path) chokepoints; the legacy non-ladder path is intentionally left untouched so
    defaults stay byte-identical. This is the domain-agnostic generalization of the
    reference's "re-read files after compaction" — keyed off the core `file_read_tracker`,
    not a coding assumption. (3 unit + 3 integration tests.)
  - **Already present (not rebuilt):** recent tool results already survive forced
    compaction (`DefaultCompaction`/`DetailedCompaction` keep the last *N* turns verbatim);
    the proactive trigger is already a token threshold (`estimate + reserve ≥ 0.8·limit`);
    the detailed-summary guardrails (`TEXT ONLY`, `<analysis>`/`<summary>`) already live in
    `_DETAILED_SUMMARY_PROMPT`.
  - **Deferred as speculative (YAGNI):** the `sessionMemoryCompact` rung (an LLM-free
    summary sourced from `MemoryStore`) and a separate COW "snip" rung — `micro_compact`
    already covers LLM-free tool-result reduction, and a memory-sourced summary needs a
    concrete embedder use case before it earns its complexity. Revisit if a real need
    surfaces.

#### 1.3 Output-truncation & model-fallback recovery — *mechanism*
- **Why:** `loop/streaming.py` recovers from `context_length_error` but not output
  truncation or provider overload.
- **Where:** `loop/streaming.py`, `providers/with_retry`, loop State/checkpoint.
- **Approach:** **`max_tokens` escalation** — on a truncated finish, retry the identical
  request with a raised cap (8K→64K) *without* appending the truncated block; only then
  append + inject a continuation system-reminder, capped at 3 with a `<500`-token
  diminishing-returns stop. **Model fallback** — `Agent(fallback_models=[...])`: after K
  consecutive overload errors, swap the active model for the run and emit an event.
- **Verify:** a provider that truncates once then completes drives exactly one escalated
  retry with no duplicated output; K overloads trigger one model swap + event.
- **Status: model fallback done; truncation escalation deferred.**
  - **Model fallback (new, shipped).** `Agent(fallback_models=[...])` — there was *no*
    overload resilience in the loop (`with_retry` exists but is unwired, so a `ProviderError`
    killed the run). On a retryable `ProviderError` (overload, e.g. 529) the recovery path
    swaps the active model to the next fallback **for the rest of the run** and retries,
    emitting a `ModelFallbackEvent`. The swap is run-level: persisted on
    `session.active_model` (read in `loop/request.py`) and reset at run start, with
    `session.fallback_index` tracking consumption (robust to duplicate model names). Wired
    into both streaming attempt-loops (`_stream_turn_with_ladder` + the legacy retry path,
    now a fallback while-loop); with `fallback_models` unset both stay byte-identical.
    Overload raises *before* any token streams, so there is no duplicated output. Simplified
    from the roadmap's "after K consecutive overloads" to "swap on overload" (KISS — backoff
    is `with_retry`'s job, not the fallback's). (4 tests: fallback, no-fallback byte-identical,
    run-level persistence across turns, exhaustion → error.)
  - **Truncation `max_tokens` escalation/continuation — deferred (YAGNI/policy).** Auto-raising
    the output cap past the embedder's configured `max_output_tokens` is policy a
    pure-mechanism SDK should expose as an explicit opt-in, and the continuation half
    (append-truncated + diminishing-returns `<500`-token stop) entangles with
    partial-event duplication for streaming UIs. Lower real-world frequency than overload
    (a misconfigured cap is embedder-fixable). Revisit as a focused opt-in
    (`Agent(truncation_recovery=...)`) when a concrete need surfaces.

#### 1.4 Task coordination primitives: claim / ready / release — *mechanism*
- **Why:** the DAG **model** already exists; the *coordination verbs* don't. These are
  store-level primitives any multi-agent embedder needs — not a choreography.
- **Where:** `sessions/store.py` (protocol), `sessions/sqlite.py`, `sessions/postgres.py`.
- **Approach:** add to `SessionStore`:
  - `claim_task(session_id, task_id, owner)` — atomic, owner-guarded (SQL conditional
    `UPDATE … WHERE owner IS NULL` in a transaction; TOCTOU-safe, no lockfile).
  - `ready_tasks(session_id)` — `pending AND owner IS NULL AND all blocked_by completed`.
  - On complete, surface newly-unblocked downstream ids.
  - **Release-on-failure:** when an owning worker aborts
    (`_cancel_background_workers`/`agent.close()`), clear `owner` + reset `status→pending`.
  The SDK ships these verbs; *how* an embedder distributes work over them is the
  embedder's choreography.
- **Verify:** two concurrent claims on one task → exactly one wins; completing an upstream
  task moves dependents into `ready_tasks`; killing an owner releases its tasks.
- **Status:** the store *verbs* (`claim_task`/`ready_tasks`/`release_task`) are **done**
  across all three `SessionStore` backends + protocol (7 parametrized tests). The
  *auto-release-on-worker-death wiring* is deferred to Phase 2.3 — it needs the
  `owner == worker_id` convention the autonomous board establishes; wiring it before then
  would be dead code (nothing claims tasks by worker id yet).

---

### Phase 2 — Coordination & isolation primitives

The SDK ships the **substrate**; the embedder writes the choreography (autonomous loops,
team protocols, coding fleets). No domain policy in core.

#### 2.1 Mailbox substrate + protocol-correlation seam — *mechanism*
- **Why:** workers report only *up* to the parent today; there is no peer-addressable
  inbox or shared coordination space. This is the substrate under any multi-agent pattern.
- **Where:** new `mailbox/` module; reuse `WorkerHandle`/`session.workers`.
- **Approach:** a `Mailbox` protocol with `SqliteMailbox` (reuse `sessions/` infra) and an
  optional lock-guarded `FileMailbox`. A `send_message(to, content, type)` function-tool
  for parent and workers; on a worker's next `_drive_child` turn, drain its mailbox into
  `provider_view` exactly like `pending_notifications`. Provide a neutral
  **request/response correlation helper** (`request_id` + `pending → resolved` FSM) as a
  primitive — *not* specific protocols (shutdown/plan-approval are embedder choreography).
- **Verify:** worker A → worker B message is visible on B's next turn; concurrent writes to
  one inbox don't drop messages; the correlation helper matches a response to its request.
- **Status:** done (in-process substrate). `mailbox/` ships `Mailbox` protocol +
  `InMemoryMailbox` (per-recipient FIFO, async-lock guarded so concurrent sends never drop;
  destructive/atomic `drain`), `MailboxMessage` (neutral `type` + `request_id`/`in_reply_to`),
  and a non-blocking `Correlator` (pending→resolved FSM — a turn-based agent polls across
  turns rather than blocking its turn). `Agent(mailbox=...)` auto-registers the
  `send_message` tool; the loop drains a session's `mailbox_address` into `provider_view`
  each turn at the same chokepoint as `pending_notifications` (so parent and workers are
  served uniformly — workers default `mailbox_address` to their display_name). Opt-in:
  no mailbox → no tool, no drain (byte-identical). **Deferred (YAGNI):** durable
  `SqliteMailbox`/`FileMailbox` adapters — the live drain is in-process (workers are
  `agent._sessions`), so durability is an embedder concern reachable through the same
  protocol, mirroring the app-owned `MemoryStore`/`SessionStore` pattern. Add when a
  cross-process delivery need is concrete.

#### 2.2 Generic filesystem isolation backend — *seam (generalizes worktrees)*
- **Why:** parallel subagents share one cwd — real-disk edit collisions are unaddressed.
  The SDK must not hardcode `git`; it exposes the *isolation seam*.
- **Where:** new `IsolationBackend` protocol alongside `tools/execution.py`;
  `run_subagent`, `workflow/`.
- **Approach:** `IsolationBackend.acquire() -> cwd` / `release(cwd, keep=False)`. Add
  `isolation=<backend>` to `run_subagent` and `wf.agent(...)`, running the child with
  `ToolContext.cwd` overridden to the acquired path. Ship a trivial `TempDirIsolation`
  (copy/scratch dir). **Git-worktree is an embedder implementation of the protocol**, not
  shipped in core. Document two tiers: `ResourceAccess` (cheap, serialize same-resource
  writes) vs isolation backend (strong, parallel branches).
- **Verify:** two parallel subagents writing the same relative path under
  `TempDirIsolation` don't collide; `keep=False` cleans up; a custom backend slots in via
  the protocol.
- **Status:** done for `run_subagent`. `tools/isolation.py` ships the `IsolationBackend`
  protocol + `TempDirIsolation` (fresh scratch dir per `acquire`, optional `source` seed
  copy, blocking fs work off-loop via `asyncio.to_thread`). `RunSubagentArgs` gains
  `isolation` + `isolation_keep`; the child runs under an acquired cwd via the new
  `Session.cwd_override`, released in the `finally` (leak-safe — acquired inside the try).
  The scheduler routes execution **and** permission path-rule matching through
  `_effective_cwd(session, agent)` (override → `agent.cwd`), so isolation is honored at all
  three cwd sites. Git-worktree stays an embedder impl of the protocol; the two-tier
  guidance (`ResourceAccess` vs isolation) is in the module docstring. Opt-in: no
  `isolation` → child uses `agent.cwd` (byte-identical). `wf.agent(isolation=, isolation_keep=)`
  threads the backend through to `run_subagent`, so workflow fan-outs get per-branch cwds too.

#### 2.3 Background-any-tool — *mechanism (generalizes background bash)*
- **Why:** background execution is hard-wired to the subagent path; the substrate
  (detached task + notification drain) is general but unexposed for arbitrary tools.
- **Where:** `scheduler.py`, `tools/` (a `run_in_background` call hint), `loop/runner.py`.
- **Approach:** let the scheduler background *any* tool call carrying a `run_in_background`
  hint — detached `asyncio` task, output streamed to the virtual `FileBackend`
  (reuse `result_offload`), completion delivered through the existing
  `pending_notifications` chokepoint, tracked via a `WorkerHandle`-like record. Bash gains
  nothing special — it's just one tool that may set the hint.
- **Verify:** a backgrounded long-running tool returns an immediate ack, its completion
  notification arrives on a later turn, and `session.abort()` cancels it.
- **Status:** done (core mechanism). `Agent(enable_background_tools=True)` lets the scheduler
  background **any** allowed tool call carrying a `run_in_background` hint: the hint is
  stripped before validation (so no tool needs to declare it), the call is detached as an
  `asyncio` task tracked in `session.background_tasks`, an immediate ack becomes its
  tool-result block, and completion is posted as a `<task-notification>` through the existing
  `pending_notifications` chokepoint (drained next turn). `session.abort()` (and the loop's
  abort cleanup) cancel the detached tasks. Denied / errored / hook-blocked calls fall
  through to normal foreground handling. Opt-in: with the flag off the hint is passed through
  untouched and the tool runs inline (byte-identical). Bash gets no special path — it's just
  one tool that may set the hint. **Deferred (YAGNI):** streaming live tool output to the
  virtual `FileBackend` via `result_offload` — the completion notification already carries the
  result; live-tail streaming is an enhancement to add when a concrete long-output need appears.

---

### Phase 3 — Context, memory & scheduling mechanisms

#### 3.1 Memory lifecycle hooks: extraction + consolidation — *mechanism*
- **Why:** retrieval is strong, but memory only accumulates on explicit writes. The
  extract→consolidate *loop* is the missing mechanism — the embedder supplies the
  extraction prompt/policy; the SDK supplies the wiring.
- **Where:** `hooks/` (a `MemoryExtractionHook` seam), `memory/` (add `consolidate()`).
- **Approach:** a hook on terminal turns that runs a caller-provided side-query over the
  pre-compaction `full_history` tail, dedups against existing entries, and `upsert`s —
  skipped if the agent already wrote memory this turn. A `consolidate()` store capability
  gated like the reference (time + change-count + lock). The *prompt* and *what counts as
  a memory* are embedder-supplied; Linch ships the lifecycle seam.
- **Verify:** with a stub extractor, a durable fact stated across turns is upserted once
  without an explicit tool call and isn't duplicated on re-run.
- **Status (done):** `MemoryExtractionHook` (`hooks/memory.py`) fires on the `Stop` hook of a
  successful terminal turn, runs a caller-supplied extractor over the `full_history` tail
  (`MemoryExtractionContext`), content-dedups each candidate against the store
  (`search` score ≥ `dedup_threshold`), and `upsert`s the survivors. It sits out turns where
  the agent already called a memory-write tool (`memory_write_tools`, default `UpsertMemory`)
  and never alters the answer (always returns `None`; the dispatcher isolates a raising
  extractor). Consolidation is a neutral `ConsolidationGate` (`memory/lifecycle.py`) — time +
  change-count + an in-process `asyncio.Lock` single-flight — running an embedder-supplied
  `consolidator(store, ctx)` thunk only when gated. The extractor/consolidator (the LLM
  side-query, the prompt, what counts as a memory) are embedder policy; Linch ships only the
  wiring. Opt-in via `Agent(hooks=[MemoryExtractionHook(...)])`; with no such hook the loop is
  byte-identical. **YAGNI deferrals:** no `consolidate()` method was added to the `MemoryStore`
  protocol (the gate + thunk keeps stores untouched and the capability optional); a
  multi-process consolidation lock (a store lock row) is left to durable store adapters.

#### 3.2 System-prompt section assembly + cache boundary — *mechanism*
- **Why:** the prompt is one config string; any dynamic block (memory/MCP) risks
  invalidating the whole cached prefix.
- **Where:** new `SystemPromptBuilder` (mirror `ContextBuilder`), `loop/request.py`.
- **Approach:** an ordered registry of named sections, each `static`/`dynamic` with a
  `condition(state)` predicate; `Agent(system_prompt=...)` becomes one section. Emit
  `(static_block, dynamic_block)` and wire a cache boundary in
  `apply_provider_capabilities` so caching providers cache only the static prefix. Move
  volatile facts to a prepended `<system-reminder>` user message.
- **Verify:** changing a dynamic section leaves the cached static prefix intact
  (cache-read tokens unchanged on a stub caching provider).

#### 3.3 Scheduling primitive — *mechanism*
- **Why:** no time-trigger exists. A neutral scheduler is a mechanism; *what* gets
  scheduled is embedder policy.
- **Where:** new `scheduler/` module (distinct from the tool `scheduler.py`); persist via
  `SqliteRunStore`.
- **Approach:** a `Schedule` abstraction (interval + a neutral `cron_matches`/
  `validate_cron` utility), an async `SchedulerLoop` (`asyncio.create_task`, 1s tick — not
  a thread), and a `ScheduleStore` protocol for durability + a multi-process lock row. On
  fire, enqueue into `session.pending_notifications` (reuse the existing drain). Expose
  register/list/cancel as `@tool` functions, auto-registered when a `ScheduleStore` is
  configured. Emit a `ScheduleEvent`. The firing *payload/policy* is the embedder's.
- **Verify:** a `* * * * *` schedule fires once/minute as a UserEvent; durable schedules
  survive a store reload; an invalid expression is rejected at register time.

---

### Phase 4 — Extension-surface hardening

Small, high-confidence additions that complete the pure-SDK extension contract.

#### 4.1 Hook contract: input mutation, allow-invariant, more events
- **Where:** `hooks/`, `scheduler.py`, `subagents/runner.py`, `compaction.py`,
  `loop/terminals.py`.
- **Approach:** confirm/add **`updated_input`** on `HookResult` (a `PRE_TOOL_USE` hook
  rewrites tool input before execution, wired through validate→execute); enforce the
  **allow-invariant** (a hook `allow` cannot bypass a configured deny/ask rule — add a
  regression test at the hook↔permission seam); add `SUBAGENT_START/STOP`,
  `PRE_COMPACT/POST_COMPACT`, `POST_TOOL_USE_FAILURE` events; add a final-answer
  **re-entry guard** so a blocking hook can't loop.
- **Verify:** a pre-tool hook's `updated_input` reaches `execute`; hook-allow + config-deny
  → denied; a blocking final-answer hook fires once.

#### 4.2 Permissions: layered sources, passthrough, subagent bubbling
- **Where:** `permissions/`, `subagents/`.
- **Approach:** a `PermissionRuleSet` merging ordered sources
  (defaults < project < local < runtime/session) with policy-wins semantics; a
  `passthrough` decision; **bubble** a subagent's `PermissionRequestEvent` to the parent's
  event stream/HITL instead of auto-denying.
- **Verify:** a project deny overrides a runtime allow; a subagent permission request
  surfaces to the parent caller.

#### 4.3 MCP: mid-run registration + annotation→permission bridge
- **Where:** `mcp/`, `loop/request.py`, `permissions/`.
- **Approach:** support connecting a server **during a run** so its tools appear next turn
  (request assembly already rebuilds per turn — ensure registry mutation is picked up and
  stale cache dropped); map server `readOnly`/`destructive` annotations to `ToolRule`
  tiers. Defer OAuth/PKCE and reverse channels (YAGNI).
- **Verify:** a server connected mid-run is callable next turn; a `destructive` MCP tool
  triggers a permission prompt.

#### 4.4 Input-aware concurrency seam *(Low)*
- **Where:** tool protocol, `scheduler._tool_parallel`.
- **Approach:** let `parallel` be a callable `parallel(input) -> bool` so a tool can decide
  concurrency-safety per call (a generic seam; any tool, not just Bash, can use it).
  Ordering/safety already handled by `_partition_batches`.
- **Verify:** a tool returning `parallel(input)=True` for read-only inputs runs those
  concurrently while mutating inputs serialize; ordering preserved.

---

### Phase 5 — SDK-grade hardening

The reference curriculum is a *product* tutorial, so it never teaches these — yet they are
exactly what separates an embeddable SDK from an app. Several Linch already does well;
this track makes them explicit guarantees.

- **Curated public API + semver** — an explicit `__all__`/public-surface contract and a
  versioning policy embedders can pin to; mark internal modules clearly.
- **No global state / multi-tenancy / cancellation** — audit for process-global state so N
  agents run in one host process safely; verify `session.abort()`/`agent.close()` fully
  drain tasks (the `_cancel_background_workers` guarantees) under concurrency.
- **Versioned serialization/resume** — treat `RunCheckpoint`/stored-event formats as a
  stable, versioned contract with forward-compat handling.
- **Streaming/backpressure ergonomics** — confirm the event `AsyncIterator` applies
  backpressure correctly to a slow host consumer; document the contract.
- **Domain-agnostic proof** — ship a **non-coding** recipe (e.g. support or research agent)
  under `examples/` to prove the SDK isn't coding-shaped and to exercise the seams above.
- **Embedder docs for every seam** — each protocol added above (`IsolationBackend`,
  `Mailbox`, `ScheduleStore`, `SystemPromptBuilder`, memory lifecycle) ships with a "how to
  implement your own" doc, matching the existing `ExecutionBackend`/`MemoryStore` pattern.

---

## Sequencing summary

| Phase | Theme | Items | Gate |
|---|---|---|---|
| 1 | Runtime resilience & cost | fork subagents · compaction fidelity · output/fallback recovery · task claim/ready/release | none — start here |
| 2 | Coordination & isolation | mailbox substrate · isolation backend · background-any-tool | needs 1.4 for multi-agent |
| 3 | Context/memory/scheduling | memory lifecycle hooks · prompt assembly · scheduling primitive | independent |
| 4 | Extension surface | hook contract · permission layering · MCP · concurrency seam | independent |
| 5 | SDK-grade hardening | API/semver · multitenancy · serialization · backpressure · non-coding recipe · seam docs | continuous |

Everything is a **mechanism or seam**: an embedder assembles a coding agent — or any other
agent — on top, and Linch never ships the domain policy.

**Mapped to the open↔closed axis:** Phase 1 (resilience/cost) + Phase 2 (coordination)
harden the **open loop** (`create_deep_agent`) so it can roam without becoming a slop
machine; the budget + verification control surface and Phase 5's serialization contract
keep the **closed loop** (`run_workflow`) cheap, honest, and repeatable. Both loops draw
from the same primitives — the axis is a dial, not two codebases.

---

## Docs

Topic-split usage guide under `docs/usage/` (agent, providers, events, tools,
structured-output, hooks, context-and-memory, filesystem, workflows, deep-agent,
skills, examples); `docs/usage.md` retained as a redirect index.
