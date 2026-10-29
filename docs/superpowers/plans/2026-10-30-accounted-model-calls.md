# Accounted SDK Model Calls Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Account for primary and compaction provider deliveries, enforce shared observed-spend admission, and recover durable receipts without duplicate local charges.
**Architecture:** A private per-run ledger owns intent/dispatch/receipt state and accounting. The runner merges its namespaced checkpoint state, and compaction uses an accounted gateway/provider facade. A versioned journal is authoritative; provider projections and checkpoints are recoverable derived state.
**Tech Stack:** Python>=3.10, async normalized providers, existing RunStore/SessionStore, pytest, Ruff, Pyright, uv.
**Spec:** ../specs/2026-10-30-accounted-model-calls.md

## Global Constraints

- Python >=3.10, async runtime/provider paths, duck-typed protocols.
- Preserve the current neutral identity/persona; no persona expansion.
- Provider wire formats stay inside provider adapters.
- Public additions are deliberate through linch.__all__ and public API tests.
- Implement Python-native contracts independently; do not copy GPL OpenHuman/TinyAgents/TinyJuice implementation into the MIT SDK.
- Preserve full_history/provider_view, permission ordering, existing public RunBudget.charge and primary event relative ordering.
- No global mutable ledger, no unreported spend silently treated as zero, no nested limiter admission.
- Keep linch.model_calls.v1 disjoint from linch.reliability and checkpointable-hook state.
- The exact spec limits and replay branches are binding. No exactly-once external billing or distributed-tree budget-recovery claim.
- Do not mutate the source checkout's docs/ROADMAP.md, plans/ or openhuman/.
- Each task stages only its owned files; root supplies independent review. No worker pushes, tags, merges or publishes.

## Review Focus

- Journal failure after reported usage retains in-memory spend while stopping continuation; add a failure-injection test in Task1 and assembled test in Task4.
- Intent-only cancellation never poisons a cap, while dispatch_started without receipt fences the same live owner and siblings; test in Tasks1/2/4.
- Hooks or accepted session appends cannot erase or mutate received usage; settle first and pin terminal totals in Task3.
- A projection cache and accounting journal may be out of step; cover BOTH crash orderings and fingerprint mismatch in Task4.
- Existing custom compaction using the supplied provider gets accounting, not an unsupported arbitrary-transform replay promise; test facade/limiter in Task2 and docs in Task5.

---

### Task 1: Receipt codecs, journal envelope and accounting state

**Files:**
- Create: src/linch/model_calls.py, tests/test_model_calls.py
- Modify: src/linch/budget.py, src/linch/events.py, src/linch/__init__.py, tests/test_public_api.py
- Test existing event codec/budget suites where applicable

**Interfaces:**
- Consumes: Usage, RunBudget, existing event codec/export conventions.
- Produces: ModelCallIntent/ModelCallReceipt/AuxiliaryTextResult fields exactly as Spec section2; intent_to_dict/from_dict, receipt_to_dict/from_dict.
- Produces: public ModelCallEvent envelope and is_model_call_event as Spec section12; journal record kinds intent/dispatch_started/receipt/abandoned/projection.
- Produces: _ModelCallLedger(run_id, *, budget=None, persist=None); async begin(intent), mark_dispatch_started(intent), settle(receipt)->bool, abandon(intent,reason), restore(state,journal); snapshot(), assert_may_start(), detached total_usage/total_cost_usd/usage_complete.
- Produces: private receipt-aware budget ownership/deduplication and same-owner unknown-use fence, with unchanged public charge().

- [ ] **Step 1: Write behavioral failing tests**

Create fixtures that construct the exact spec records (one compaction receipt: input111/output22, model gpt-5, summary text and its SHA-256, fixed request/source fingerprints).
Pin these assertions:
```python
await ledger.begin(intent)
await ledger.mark_dispatch_started(intent)
assert await ledger.settle(receipt) is True
assert await ledger.settle(receipt) is False
assert ledger.total_usage.input_tokens == 111
assert ledger.total_usage.output_tokens == 22
assert budget.spent_tokens == 133
```
Add named cases for strict codec roundtrip, unknown version, invalid usage/cost/hash, receipt-without-start, conflicting duplicate, caller Usage mutation, detached snapshot, same/fresh/mismatched budget ownership, journal-write failure retaining observed spend, and sibling admission after unknown dispatched use. Fixture IDs are opaque constants; the production gateway later mints UUIDs.

- [ ] **Step 2: Verify RED**

Run the new test file with the locked Python environment. Imports occur inside tests so missing production code fails the requested behavior rather than collection. Capture expected missing-module/type behavior.

- [ ] **Step 3: Implement the records, ledger, event codec and private budget helper**

Follow the exact spec state machine. In-memory runs use an in-memory journal; configured RunStore persistence is required before successful continuation. Keep owned Usage/JSON detached and distinguish observed spend from durable settlement. Export only the two deliberate event names; model-call record implementation types remain private to submodule contracts.

- [ ] **Step 4: Verify GREEN and compatibility**

Run focused codecs/budget/public API tests, then full pytest once, Ruff check/format check and Pyright. Test public event codec roundtrip and that ordinary UsageEvent legacy shape remains unchanged.

- [ ] **Step 5: Commit and report**

Commit owned files with subject `feat: add receipt-based model call accounting`. Report exact RED/GREEN/check commands and results, self-review and concerns. Independent task review must approve before Task2.

### Task 2: Accounted auxiliary gateway and compaction provider facade

**Files:**
- Modify: src/linch/model_calls.py, src/linch/compaction.py
- Create: tests/test_compaction_accounting.py
- Modify relevant existing compaction/limiter tests only when a documented new invariant requires it

**Interfaces:**
- Consumes: Task1 ledger, records, journal envelope and private budget helpers.
- Produces: _AccountedAuxiliaryCaller(provider,ledger,limiter=None,parent_logical_call_id=None,turn_index=None).text with Spec section3 keyword signature.
- Produces: private _AccountedCompactionProvider facade: normal provider attributes delegate; stream(request) follows the same per-delivery gateway/limiter and normalized stream contract.
- Produces: optional CompactionContext auxiliary-caller/logical-operation binding, preserving existing three positional fields and standalone strategy use.
- Default/Detailed compaction prompts remain byte-identical; passed-provider custom strategies retain the old call shape.

- [ ] **Step 1: Write failing gateway tests**

Use a fake normalized provider returning summary Usage111/22 and text. Assert the result text/receipt, ledger133 tokens, intent before dispatch_started before iterator advancement, and exactly one limiter acquisition at capacity1. Add empty/tool-use/oversized summary with valid Usage charged/rejected, strict required message_start.model/Usage/order, abort before dispatch versus during stream, limiter release, persistence failure no dispatch, and a custom strategy using the supplied provider. Assert opaque outside-gateway calls are never advertised as covered.

- [ ] **Step 2: Verify RED**

Run tests/test_compaction_accounting.py; capture the missing gateway/facade behavior.

- [ ] **Step 3: Implement the shared gateway and wire built-in compaction**

Mint a fresh delivery UUID per stream invocation, retain logical identity only for retries of the same operation, and gate immediately before advancing the iterator. No outer strategy-level limiter around accounted dispatch. Bound accepted text at1MiB; capture strict reported model/Usage and settle reported spend before output acceptance. Unknown dispatch marks the live budget owner fence.

- [ ] **Step 4: Verify GREEN**

Run the new file, existing compaction/limiter tests, full suite once and style/types. Test direct standalone strategies still work without an active durable run and do not acquire a nested slot.

- [ ] **Step 5: Commit and report**

Commit `feat: account for compaction provider deliveries`; report the same evidence contract and receive independent review.

### Task 3: Integrate main-run accounting and terminal totals

**Files:**
- Modify: src/linch/loop/runner.py, src/linch/loop/streaming.py, src/linch/loop/request.py, src/linch/loop/finalize.py and src/linch/loop/terminals.py only where accounting chokepoints require
- Modify: src/linch/session.py, src/linch/events.py, src/linch/reports.py and src/linch/__init__.py as needed for the specified additive fields
- Create: tests/loop/test_model_call_accounting.py

**Interfaces:**
- Consumes: Task1/2 ledger/gateway/facade.
- Produces: one session/run-bound ledger; no Agent-global mutable ledger. All main/aux observed usage, actual-model cost, ResultEvent/report totals and budget changes come from it.
- Produces: optional UsageEvent source/call/logical/model metadata, omitted when absent; ResultEvent.usage_complete defaultTrue with legacy omission when true; RunReport propagation.
- Produces: common receipt reference + sanitized primary disposition/reason for the later reliability module. That module never independently charges totals or budget.
- Carries strict normalized message_start.model through AssistantAssembly privately; settle before AfterProviderCall control and accepted session append.

- [ ] **Step 1: Add end-to-end RED assertions**

Port the public-API reproduction into focused tests with long real history and actual DefaultCompaction. Assert uncapped usage444 input/66 output and spent510; with cap100 assert provider call kinds exactly [summary], total133, and budget terminal before main. Also assert main AfterProviderCall stop still reports/charges received Usage, hook mutation cannot rewrite it, mixed-model costs remain per receipt, every physical retry/fallback gets a new delivery ID, and primary-only event relative ordering remains intact.

- [ ] **Step 2: Verify RED**

Run the new tests on the integrated ledger/gateway before runner wiring; capture the existing377-only and summary-then-main failures.

- [ ] **Step 3: Wire all primary delivery paths and terminal tails**

Replace the small primary accounting block, not the whole loop. Track the actual physical stream boundary, including retries/fallback/frozen replay. Inject the compaction gateway, drain auxiliary usage, recheck admission after compaction, and use ledger totals in every stop/error/abort/success path. Preserve signal/observer span ownership and normal permission/tool scheduling.

- [ ] **Step 4: Verify GREEN**

Run targeted run/budget/streaming/hook/report tests and the full suite once; lint/format/types. Do not relax the provider stream contract or fabricate input to hide invalid native calls.

- [ ] **Step 5: Commit and report**

Commit `feat: unify run usage and auxiliary budget admission`; independent review follows.

### Task 4: Durable receipts, projection replay and checkpoint reconciliation

**Files:**
- Modify: src/linch/model_calls.py, src/linch/loop/runner.py, src/linch/loop/checkpoint.py, src/linch/run_store.py
- Modify: src/linch/compaction.py, src/linch/sessions/store.py, src/linch/sessions/memory.py, src/linch/sessions/sqlite.py, src/linch/sessions/postgres.py and snapshot codec only where projection identity requires
- Create: tests/storage/test_model_call_journal.py
- Modify existing resume/model-input snapshot tests with explained version-contract expectations

**Interfaces:**
- Consumes: ledger.restore/snapshot and receipt event codec.
- Produces: merged linch.model_calls.v1 namespace; receipt journal authoritative ahead of checkpoint; independent namespaces preserved.
- Produces: active-primary logical/delivery/turn/snapshot/base/digest linkage and every frozen-replay branch from the Spec.
- Produces: built-in projection_id/source/target fingerprint anchor in journal and optional provider snapshot; deterministic paid-result replay without inference or another charge.
- Accounting codec/semantics/text bound/replay behavior are fingerprinted in RunContract.

- [ ] **Step 1: Write crash-boundary RED tests**

Use real InMemoryRunStore and SQLite reopen tests with injected write failures at intent, dispatch_started, receipt, projection, snapshot and checkpoint boundaries. Assert no provider dispatch before a successful dispatch marker; intent-only not_dispatched; started/no receipt fences; known receipt settles once; accepted main with matching assistant continues; accepted main without assistant uses a NEW charged delivery; rejected-native receipt reconstructs the safe pending prefix without raw arguments. Test prefix0/1/2 and mismatched suffix. Test projection-before-consumed and consumed-before-projection, cache loss, changed context/fork, fresh/same/mismatched budget owner, namespace preservation, and actual-model cost restoration.

- [ ] **Step 2: Verify RED**

Run the crash/reopen tests; capture the missing reconciliation behavior, not an unrelated environment error.

- [ ] **Step 3: Implement deterministic recovery**

Reconcile accounting and pending reliability state before provider_pending replay. Never reuse a physical delivery ID for a new request or derive charges from checkpoint totals alone. Apply built-in paid projections only to matching source, skip matching target, fail mismatched input. Custom facade users have accounting-only replay semantics unless explicitly providing a deterministic commit seam.

- [ ] **Step 4: Verify GREEN and cross-feature seams**

Run durable store/resume/fork/compaction/model-input tests and the full suite once; lint/format/types. Existing malformed-native batch handling remains assigned to reliability, with its common receipt interface pinned by tests/manual review.

- [ ] **Step 5: Commit and report**

Commit `feat: reconcile model call receipts during resume`; receive independent whole-feature review covering Tasks1–4 before release preparation.

### Task 5: Document, validate and prepare the first feature prerelease

**Files:**
- Modify: CHANGELOG.md, pyproject.toml, uv.lock, docs/usage/events.md, docs/usage/agent.md, docs/architecture/compaction.md, docs/versioning.md
- Create: docs/migration-3.0.md (incremental prerelease coverage; later reliability migration will extend it)
- Add relevant API examples/tests only when needed to demonstrate the new supported fields

**Interfaces:**
- Consumes: approved accounting feature; unchanged core dependency floors.
- Produces: documented version3.0.0a1, wheel/sdist and checksums, feature evidence package. Publishing is root's responsibility, never the worker's.

- [ ] **Step 1: Document actual behavior and limits**

Describe cumulative primary+aux usage, post-summary budget stop, strict unknown-use fence, live shared-owner behavior, standalone/custom strategy coverage, built-in paid-result replay, partial pricing, and no distributed-tree/exact-billing/reservation guarantee. Preserve persona and existing compaction prompt/default strategy.

- [ ] **Step 2: Prepare consistent prerelease metadata**

Set package3.0.0a1 and refresh uv.lock consistently. Add one concrete changelog section with the behavior before/after and migration notes; don't describe later features as shipped.

- [ ] **Step 3: Run release validation**

Run full pytest, Ruff lint/format, Pyright, uv lock --check, build wheel/sdist in isolation, validate metadata/import/public surface from installed wheel and run appropriate wheel smoke/full coverage. Record failures rather than silently waive them; no live API calls.

- [ ] **Step 4: Self-review and commit**

Commit `release: prepare 3.0.0a1 accounting feature`, with complete verification report. Root creates the review package, independent final feature review, PR/release actions and remote artifact verification; no publish from this task.
