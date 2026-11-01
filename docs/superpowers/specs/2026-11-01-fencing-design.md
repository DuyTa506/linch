# Holder-carried run-store fencing

Status: **proposal for Claude's review; Phase 2 has not started**. Phase 1a
store fixes are committed separately. Source references below describe
`fix/fencing` at `da9903034096ff839ea1fa95f4dca7c44bace3af`.

Authority: [D1, D2, D8 and worker rules](2026-10-30-scale-ready-architecture.md).
This note compares the two requested designs; it does not change the accepted
lease/epoch model or implement either design.

## Problem and required guarantees

The immutable `RunLease` is already returned to the holder, but writes obtain
their fence from mutable maps on the store. There are two independent defects:

1. A release attempt removes matching local maps in `finally`, even when the
   durable release raises `LeaseLostError`. The next write finds no held epoch
   and becomes unfenced. See `src/linch/run_store.py:2002-2039` and
   `src/linch/run_stores/postgres.py:776-814,355-362` (U69 release finding).
2. A later claim on the same store overwrites the maps for that run. An older
   coroutine then borrows the new holder's fence. The worker intentionally
   shares its store pair across jobs and reconnect cycles. See
   `src/linch/run_store.py:1967-1969`,
   `src/linch/run_stores/postgres.py:729-731`, and
   `src/linch/worker/runtime.py:424-465` (U69 per-instance-state finding).

Both options below must satisfy these invariants:

- Every leased write uses the **holder's immutable run ID, owner and epoch**.
  A store instance is a backend handle, never the current-holder authority.
- Successful release advances the durable epoch. Failed/stale release cannot
  erase a holder's token or alter another holder's token. Every subsequent
  mutation through a lost holder raises `LeaseLostError` and changes nothing.
- Postgres checks owner and epoch in the transaction/statement that mutates the
  run, events or snapshots. InMemory checks against shared durable lease state
  before mutation. Atomic checkpoint/event and terminal/event boundaries remain.
- Expiry permits takeover; write fencing remains based on owner/epoch, not a
  local expiry-clock check. Renew requires a live matching token.
- Per D1, genuinely unleased calls and custom stores without leases retain
  their existing behavior. No implicit fallback from a leased call to an
  unleased call is permitted.

## Inventory

### Mutation entry points

Counts are semantic entry points, not overloads or upstream helper invocations:
**10 mutations on both backends**, **1 Postgres dispatch mutation**, and
**3 lease commands** = **14 distinct durable mutation/lease methods**.
There are **27 concrete method implementations** (13 InMemory, 14 Postgres).
`close` adds one semantic lifecycle method, two implementations; it does not
mutate durable run data. Read methods and schema initialization are excluded.

| Method | Protocol in `src/linch/run_store.py` | InMemory in same file | `src/linch/run_stores/postgres.py` | Fencing responsibility |
|---|---:|---:|---:|---|
| `create_run` | 538 | 1872 | 168 | Fresh unleased creation; leased pending-row adoption must carry token |
| `save_checkpoint` | 577 | 2054 | 379 | Checkpoint and status |
| `save_checkpoint_with_events` | 602 | 2083 | 457 | Atomic checkpoint/status/event prefix |
| `append_event` | 630 | 2124 | 404 | Event and sequence allocation |
| `mark_completed` | 661 | 2288 | 572 | Completed checkpoint/status |
| `mark_failed` | 678 | 2307 | 590 | Atomic failure metadata/checkpoint/terminal events |
| `append_events` (optional) | 713 | 2148 | 506 | Atomic batch and sequence allocation |
| snapshot `save` (optional) | 736 | 2199 | 816 | Snapshot insert/deduplication |
| snapshot `delete` (optional) | 777 | 2246 | 946 | Delete only within the fenced run |
| snapshot `prune` (optional) | 794 | 2263 | 960 | Retention, including a no-op prune |
| `create_pending_run` | backend-only | absent | 268 | Unleased API dispatch, using caller transaction |
| `claim` | 335 | 1939 | 671 | Conditional ownership/epoch transition |
| `renew` | 354 | 1972 | 734 | Matching live owner/epoch; retain epoch |
| `release` | 369 | 2002 | 776 | Matching owner/epoch; clear owner and advance epoch |
| `close` (lifecycle) | optional | 2354 | 981 | Bound view must not close the root backend |

Internal write delegation is also part of the token audit; it must not silently
re-enter the unleased root API:

- InMemory `mark_completed` delegates to `save_checkpoint` at
  `src/linch/run_store.py:2305`; its batch override path calls `append_event`
  at `:2172` after validating the entire batch.
- Postgres delegates `save_checkpoint` to `save_checkpoint_with_events` at
  `src/linch/run_stores/postgres.py:401`, `append_event` to `append_events` at
  `:419`, and `mark_completed` to `save_checkpoint` at `:588`.
- Postgres `_write_event_rows` (`:421`) owns the atomic run/event CTE. Its
  callers are `:486,528,641`. Phase 1a `mark_failed` additionally locks and
  reads TEXT metadata in the same transaction to preserve NUL characters.
- Postgres `_delete_snapshots` (`:910`) owns snapshot deletion; its callers
  are `delete` at `:958` and `prune` at `:975`.
- InMemory's ten mutation entry points call `_check_lease` at
  `src/linch/run_store.py:1904,2076,2108,2140,2167,2223,2257,2277,2303,2332`.
  Postgres pending adoption uses `_assert_fence` at `:238`.

Thus there are **5 public-to-public delegates** and **5 calls to the 2 private
Postgres mutation helpers**, in addition to the external callers below.

### Call sites outside the store implementations

Count each physical invocation expression once, including `getattr` aliases
and branch alternatives. Do not multiply it by the number of callers of its
containing helper or by the number of loop iterations. This yields **19
regions / 26 invocations**: **21 potentially fenced writes**, **1 unleased
pending creation**, **3 lease commands**, and **1 lifecycle close**.
Model-call accounting flows through the same event buffer; it adds no separate
RunStore mutation API. Generic `save/delete/prune` below mean model-input
snapshots, not session-store snapshots.

| Source region | Invocation line(s) and operation | Count |
|---|---|---:|
| `src/linch/loop/runner.py:165-199` | `165 create_run` | 1 |
| `src/linch/loop/checkpoint.py:220-228` | `222 mark_failed`, `224 save_checkpoint_with_events` | 2 |
| `src/linch/loop/checkpoint.py:242-269` | `246 append_events` via `appender`, `258 append_event` fallback | 2 |
| `src/linch/loop/checkpoint.py:272-283` | `282 append_event` without a buffer | 1 |
| `src/linch/loop/runtime.py:932-955` | `949 mark_failed` fallback | 1 |
| `src/linch/loop/runtime.py:1008-1040` | `1021 delete`, `1033 prune` | 2 |
| `src/linch/loop/runtime.py:1042-1164` | `1159 save_checkpoint` | 1 |
| `src/linch/loop/runtime.py:1166-1194` | `1182 save` | 1 |
| `src/linch/loop/terminals.py:101-112` | `111 mark_completed` fallback | 1 |
| `src/linch/scheduler.py:390-405` | `397 append_event`, detached tool completion | 1 |
| `src/linch/tools/subagent.py:317-327` | `325 append_event`, detached subagent completion | 1 |
| `src/linch/worker/dispatch.py:108-147` | `117 create_pending_run`, unleased dispatch | 1 |
| `src/linch/worker/runtime.py:341-371` | `357 renew` | 1 |
| `src/linch/worker/runtime.py:424-522` | `439 claim`, `485 mark_failed`, `521 release` | 3 |
| `src/linch/ext/workflow/context.py:113-129` | `124 append_event` | 1 |
| `src/linch/ext/workflow/context.py:141-162` | `162 save_checkpoint` | 1 |
| `src/linch/ext/workflow/engine.py:57-85` | `81 create_run` | 1 |
| `src/linch/ext/workflow/engine.py:109-128` | `119 save_checkpoint`, `123 mark_failed`, `127 mark_completed` | 3 |
| `src/linch/agent.py:1861-1867` | `1867 close` via `closer` | 1 |

Today the lease remains inside `Worker._run_job`/`_heartbeat`
(`src/linch/worker/runtime.py:341-396,424-522`). `WorkerJob` passes the root
store to the factory; Agent, Session, RunRuntime and RunEventBuffer retain
store/run IDs, not a lease (`src/linch/agent.py:673-680`,
`src/linch/session.py:336-404,588-600`, `src/linch/loop/runtime.py:548-600`).
These are the propagation boundaries each option must address.

There is also an important **child-run authority boundary**, although it
reuses the same physical `loop/runner.py:165` call expression and therefore
does not increase the 26-invocation count. `src/linch/subagents/runner.py:258`
constructs a child Session with the parent Agent; `_drive_child` invokes
`child_session.run` at `:155`, including continuation through `:416`.
`src/linch/tools/subagent.py:242` supplies fresh child RunOptions, and workflow
`wf.agent()` invokes the same child runner at
`src/linch/ext/workflow/context.py:251-269`. A store bound to only the parent's
run cannot simply replace this shared Agent's store: it would reject every
fresh child run. Parent completion events and child-run events require
different authority even though their backend handle is shared.

### Holder-state access inventory

All occurrences in `src/` are in the two backend implementations. Count map
initialization and subscript assignment as writes; `.pop` as a deletion,
separate from ordinary reads. Counts refer to expressions, not runtime calls.

| Backend / map | Read lines | Initialization / assignment lines | Delete lines | Counts: reads / writes / deletes |
|---|---|---|---|---|
| InMemory `_held_leases` | `1985,2013,2025,2033` | `1868,1967` | `2028` | 4 / 2 / 1 |
| InMemory `_lease_owners` | `2026` | `1870,1969` | `2030` | 1 / 2 / 1 |
| InMemory `_lease_ttls` | `1995` | `1869,1968` | `2029` | 1 / 2 / 1 |
| Postgres `_held_leases` | `345,356,371,748,787,809` | `152,729` | `812` | 6 / 2 / 1 |
| Postgres `_lease_owners` | `346,359,810` | `153,730` | `813` | 3 / 2 / 1 |
| Postgres `_lease_ttls` | `751` | `154,731` | `814` | 1 / 2 / 1 |

InMemory lines refer to `src/linch/run_store.py`; Postgres lines refer to
`src/linch/run_stores/postgres.py`. The requested two maps total **14 reads,
8 writes and 4 deletions**. TTL state adds **2 reads, 4 writes and 2 deletions**.

Postgres `_fence_values` has **1 definition** at `:355` and **5 call sites**:
`:485` (checkpoint/events), `:527` (event batch), `:626` (failure), `:842`
(snapshot save), `:917` (snapshot deletion). It reads maps; it never receives
a holder token. `_assert_fence` (`:344`) and `_classify_write_failure` (`:364`)
also read the maps. InMemory `_check_lease` (`:2032`) returns early when no
held epoch exists, creating the release-to-unfenced transition.

### Separate session-store boundary

The lease protects **RunStore** mutations. SessionStore has no run-token
parameter. Both designs must retain this distinction rather than imply that
session history, task tools or external tool effects become fenced:

- Session history is appended from `loop/runner.py:888`,
  `loop/tool_batch.py:205,264,288`, `loop/feedback.py:44`,
  `loop/startup.py:225`, `loop/provider_turn.py:434`, and
  `loop/runtime.py:237,251,300` (all under `src/linch/`).
- Inbox/meta work is in `src/linch/session.py:703-724,741-775` and
  `src/linch/loop/runtime.py:216-228,345-362`; provider-view snapshots are in
  `src/linch/compaction.py:648-659`; skill/task persistence is in
  `src/linch/tools/skill.py:145-155` and `src/linch/tools/tasks.py:47-57,204`.
  Child/fork session creation also uses SessionStore.
- Worker cancellation and session quiescence must finish before release
  (`src/linch/worker/runtime.py:506-521`). Neither option promises atomicity
  between session history and run state, or exactly-once external effects.
  Extending fencing to those concerns would require a separate owner decision.

## Options compared against the inventory

| Concern | (a) Lease on every write | (b) Per-holder bound view |
|---|---|---|
| Holder authority | Every leased mutation passes `lease=RunLease`; backend validates run ID, owner and epoch | `store.bind(lease)` returns a private immutable-token view; every mutation delegates with that token |
| Ten shared write APIs | Add a keyword token to each protocol/backend method; propagate through internal delegates | Keep caller write signatures; explicitly implement the ten methods on the view and pass token to private backend primitives |
| 21 potentially fenced call expressions | Thread token to every call and optional fallback | Resolve the correct run-scoped view for every consumer; a single Agent-level replacement is insufficient |
| Claim/renew/release | Root methods consume/return tokens; no holder maps | Same root lifecycle; view never mutates or clears its token |
| Dispatch and fresh creation | `create_pending_run` and fresh `create_run` remain unleased | Dispatch remains on root; bound `create_run` only adopts its own existing pending run |
| Release / reconnect | Old tokens cannot become omitted or replaced; no lookup of a newer token | Old views retain old token after release, takeover and reconnect |
| Empty append / no-op prune | A supplied token must still be checked | Bound methods still check; unleased root empty-batch behavior may remain unchanged |
| Optional batch / snapshots | Update optional protocols and every caller's kwargs | View must expose supported optional capabilities explicitly and preserve behavior |
| Child subagent / workflow runs | Fresh child options have independent run identity and no inherited parent lease; parent completion retains parent token | Requires separate run/session-scoped store resolution and explicit child authority; wrong-run writes must never unwrap/fall back to root |
| Main risk | One missed optional argument silently creates an unleased write; more plumbing across loops/extensions | An incomplete wrapper loses capabilities, delegates a root write, or closes a shared backend |
| Estimated source change | 700-1,000 changed lines, roughly 10-13 files | 700-1,100 changed lines, roughly 8-12 source files, including child/run scoping |
| Estimated tests | 300-500 changed lines across store, loop, worker and extension tests | 350-550 changed lines across conformance, worker, child execution and direct-consumer tests |

Estimates include docstrings and TTL transport, exclude unrelated refactors,
and are planning ranges, not measurements. The same two backends and durable
owner/epoch predicates implement both options. Neither retains a fallback to
the instance's current maps.

## Recommendation: explicit lease per write

Prefer **(a)** for this repository. A bound view initially appears cheaper,
but the child-run inventory invalidates the assumption that replacing
`Agent.run_store` once is sufficient. Making that safe requires another
run/session store-resolution layer, plus explicit parent/child authority and
optional-capability forwarding. Passing the immutable token through existing
run state is more direct and easier to audit against the 21 call expressions.
This is a recommendation for review, not a change to D1.

Proposed interfaces for Phase 2:

```python
# Existing exported immutable RunLease gains its renewal TTL.
# Existing fields run_id, owner, epoch, expires_at remain.
ttl_s: float

# Existing RunOptions gains an ephemeral execution option.
lease: RunLease | None = None

# Representative built-in write; all ten mutation APIs receive this keyword.
async def append_event(
    self, run_id: str, event: Event, *, lease: RunLease | None = None
) -> int: ...
```

An explicitly supplied token must match the target run before mutation and
must be checked against durable owner/epoch, including empty/no-op operations.
The token can never be replaced by whatever token the backend most recently
claimed. All five public-to-public delegates and five private mutation-helper
calls must forward it. Preserve the subclass batch override path: a leased
override cannot silently drop its keyword token; an override lacking fenced
write support must fail closed.

Postgres passes the token to its existing atomic mutation primitives and
write-failure classification; InMemory compares it with `_lease_state`.
Remove `_held_leases`, `_lease_owners` and `_lease_ttls` rather than retain
two authorities. `renew(lease)` keeps D1's signature and reads `lease.ttl_s`;
both `claim` and `renew` populate it. An older same-owner, same-epoch refresh
token renewing with an older TTL is a liveness tradeoff, not an ownership
bypass. Independent holders still require distinct owners.

### Worker, loop and child propagation

1. `_run_job` keeps the immutable result of `claim`. The factory continues to
   receive the shared root backend; `_invoke` places the token in
   `RunOptions.lease` for that execution. The existing factory store identity
   check remains valid. Setup-failure `mark_failed`, heartbeat and release use
   that same holder token directly, before or after an Agent exists.
2. Validate `lease.run_id` against the selected `run_id` before initial
   creation/adoption or resume writes. Capture it in RunRuntime and
   RunEventBuffer for that execution. Those objects pass it to checkpoint,
   terminal, event and snapshot mutations and to batch/single fallbacks.
   Transport this through the storage seam; do not add reliability-policy
   branches to turn choreography.
3. Detached scheduler/subagent completion writers capture the **originating**
   run ID and token before awaiting/spawning work. Do not read a Session's
   later active token when a completion arrives. An old completion cannot
   borrow a resumed execution's epoch. LeaseLostError never triggers an
   unleased retry.
4. Child creation and continuation use their own RunOptions and new run ID.
   They do not inherit the parent's token. Current ordinary children remain
   unleased, as allowed by D1, while their completion event written to the
   parent carries the parent's captured token. If a host explicitly supplies
   a child lease, it must match that child's run; reject a parent/mismatching
   token rather than strip it. Preserve existing parent-cancellation and
   quiescence behavior. Leasing every child is a separate policy decision.
5. `run_workflow` and WorkflowContext need an explicit optional lease for
   their own durable run; all journal/status/checkpoint calls forward it.
   `wf.agent()` child options remain independent. Keep this ephemeral token
   out of workflow call fingerprints (`src/linch/ext/workflow/context.py:732`),
   RunContract, checkpoints and queue payloads: takeover supplies a newly
   claimed token, not a persisted old one.
6. After successful or failed release, no local authority is cleared or
   replaced. Every old coroutine still carries its original immutable token;
   a new holder receives a separate token. All later writes with the old
   token reach the durable fence and raise LeaseLostError after loss.

Unleased execution must keep the old call shape for custom stores without
leasing. A storage-seam helper should omit the lease keyword when it is None
and pass it only for a leased execution; it must never catch a rejected
keyword/LeaseLostError and retry without the token. Document the protocol
typing change for custom stores. Capability checks reject unsupported leased
execution before side effects, while unleased custom stores keep working.

### What a correct bound-view alternative would require

Option (b) remains viable if Claude prefers its caller signatures. Its private
view would explicitly implement the supported mutations/optional capabilities,
retain a token forever, reject wrong-run/fresh-creation calls, and have no-op
`close`. Claim/renew/release and dispatch remain on the root. Every mutation
would delegate with its token to the same backend primitives as option (a).

It additionally needs a run-scoped store separate from the shared Agent
configuration. The trusted runtime must give a new child its **independent**
authority at child-run creation and retain the parent's view for parent
completion writes. An ordinary child's authority can remain explicitly
unleased under D1; this must never be implemented by automatically unwrapping
or falling back to the root on a wrong-run/stale-view write. Scheduler,
subagent, workflow and snapshot consumers must resolve/capture the correct
run's view, including child continuation. `bind` must preserve optional batch
and snapshot capabilities and fail closed when used with an unsupported
leasing backend. These additional scope and identity changes explain the
revised estimate; do not implement an Agent-only binding shortcut.

### Public API impact

No new `linch.__all__` names are needed: RunStore, RunLeaseStore, RunLease,
RunEventBatchStore, ModelInputSnapshotStore, RunOptions and LeaseLostError
already exist in `src/linch/__init__.py`. Option (a) changes the exported write
protocols' keyword signatures, adds `RunOptions.lease` and frozen
`RunLease.ttl_s`, and adds an optional lease parameter to the explicit workflow
extension. Document these changes under the 3.0 breaking-change policy;
Session.run/resume keep their current options argument. Option (b) instead
adds the optional bind capability and private view/run scoping, plus the same
TTL transport. Neither option requires a persisted wire/schema change.

## Phase 2 verification and merge acceptance

TDD first, with backend parity assertions in the existing shared conformance
suite (`tests/storage/test_run_store_conformance.py`,
`tests/storage/test_run_store_leases.py`, `tests/storage/test_model_input_snapshots.py`).

- On **one shared backend object**, obtain tokens A and B around expiry/takeover.
  Parameterize all ten mutation APIs: every write with the old token raises
  LeaseLostError; records, metadata, checkpoints, event seq and snapshots are
  unchanged. Cover pending adoption and aggregate checkpoint/terminal writes.
- Test successful release then write, stale release then write, and repeated
  writes after loss. A newer token must remain usable after an old release.
  Include empty append, no-op prune, repeated snapshot save and missing
  snapshot deletion so early returns cannot bypass a supplied token.
- Test wrong owner/epoch/run, live same-owner refresh, expiry/renew rejection,
  token TTL, two independent stores, and retained old tokens after reconnect.
  Preserve unleased behavior, custom no-lease stores, missing-run errors,
  all-or-nothing encoding failures and detached returned values.
- Exercise factory/setup-error failure writes, batch/subclass fallback, snapshot
  capability detection, worker agent teardown and detached scheduler/subagent
  completion paths with the captured token. A lost token must never cause an
  unleased retry. Assert the shared root pool stays open. Workflow writes must
  forward their token and replay fingerprints must omit lease state.
- Run foreground/background child execution, continuation and `wf.agent()`
  under a leased parent: child identities stay independent, their own unleased
  durable writes work, and their parent completion writes retain the parent's
  token. Prove that a lost parent token cannot fence or authorize a new child's
  run and cannot borrow authority from a newer parent execution. For option
  (b), run the same matrix against views and verify run-scoped resolution.
- Keep session-store writes explicitly outside the run fence in tests; verify
  quiescence ordering rather than claim cross-store fencing.
- Run the required full pytest/Ruff/Pyright gates and `tests/test_public_api.py`.

**Full M4 SIGKILL and three-second DB-blip acceptance must pass before merge.**
Phase 1 does not claim these Phase 2 measurements. Use the dedicated load-test
Postgres/Toxiproxy topology, not the shared unit-test socket; hold the shared
CPU lock. The checked-in baseline commands and setup are in
[load-test measurements](../measurements/2026-10-31-load-test.md#reproduction-commands);
the final configuration and acceptance baselines are in
[scale gate](../measurements/2026-10-31-scale-gate.md#chaos-acceptance-at-m4--196-runss--rtt1).

Run the full M4 chaos group (kill, 3-second blip, drain), with the final gate's
19.6 runs/s, 15/60-second warmup/steady windows and up to 120-second drain:

```bash
FENCING_PYTHON=/home/duy/Downloads/duy_dev/agent_kit/.venv/bin/python
flock --timeout 900 /tmp/wp8-cpu.lock timeout -s INT -k 30s 12m \
  "$FENCING_PYTHON" scripts/load_test.py chaos \
  --workers 4 --rate 19.6 --rtt 1 --pool-size 16 --readers 1 \
  --warmup 15 --duration 60 --drain-timeout 120 \
  --lease-ttl 4 --stall-timeout 6 --poll-interval 1 \
  --output /tmp/fencing-m4-chaos.json
```

Also run the focused reproduction at M4; this script supplies its own bounded
lock and timeout. Its shorter window supplements, and does not replace, the
full chaos group above:

```bash
"$FENCING_PYTHON" scripts/repro_worker_db_blip.py \
  --workers 4 --pool-size 16 --readers 1 --poll-interval 1 \
  --output /tmp/fencing-m4-db-blip-repro.json
```

The original `9.97` runs/s baseline exposed failures (kill 766/767 completed,
reclaim 6.418 s; blip 292/717 terminal, four worker exits). Use the later final
scale gate as the acceptance reference: **kill 1,489/1,489 completed**, all
34 orphans reclaimed within **5.945 s <= 7 s**; **blip 1,395/1,395 completed**,
zero worker exits; drain 1,479/1,479, zero post-signal claims. The architecture
status records still later r8-r11 passes of 1,903/1,903 kill and 1,835/1,835
blip; record the current branch/configuration with every new measurement.

Require every durable run to finish with exactly one final result, contiguous
event sequences, zero writer mismatches, zero failed-without-error runs, zero
duplicate note attempts/rows and zero unexpected worker exits. Kill must reclaim
**every** orphan within 7 seconds. Enforce the final gate's zero-failed target
for the blip explicitly as well: the harness's blip terminal-status check can
accept failed runs. Keep raw artifacts outside git. A quick run or isolated
reproduction alone cannot approve Phase 2 for merge.
