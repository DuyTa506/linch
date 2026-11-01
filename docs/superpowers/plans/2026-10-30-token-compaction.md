# Implementation Plan: Token-budget closed-chunk compaction

> **Required subskill:** Use `superpowers:executing-plans` to implement this plan
> task by task.

**Goal:** Add an explicitly selected `TokenBudgetCompaction` strategy that safely
summarizes old, closed tool exchanges to a hard input-token target without changing
the append-only durable history or default prompts.

**Architecture:** Keep the parser/selector pure and private in `compaction.py`.
The runtime supplies its established token estimator and invokes summaries through
the common accounted auxiliary caller.  A successful replacement is a normal
provider-view projection with a fingerprinted recipe; full history remains the
recovery source of truth.

**Tech Stack:** Python 3.10+, asyncio, existing `Message`/tool blocks,
`SessionLog` projections, pytest/pytest-asyncio, Ruff, Pyright.

**Spec:** `docs/superpowers/specs/2026-10-30-token-compaction.md`

## Global Constraints

- This is opt-in only.  Do not alter existing default compaction, default prompts,
  persona, `GENERAL_SUMMARY_PROMPT`, or `CompactionLadder` semantics.
- Never mutate `full_history`; write only a new provider-view projection after a
  validated successful result.  Never split a message or a tool exchange.
- Retain recent complete chunks, current input, all activation/control anchors,
  malformed/open tool regions, and recovery references.
- Inside an Agent, all paid summary calls use the common accounted auxiliary
  caller exactly once.  No direct fallback, extra provider slot, or unaccounted
  retry is allowed in that path.  Direct standalone strategy use remains supported.
- Preserve custom `CompactionStrategy` compatibility; new context facilities are
  optional/defaulted.  Keep code Python-native and do not copy GPL material.
- Each task must compile and run its listed focused tests before independent review.
  Do not stage, commit, publish, or modify files owned by another lane.

## Review Focus

Check parser conservatism before summary quality: parallel and multi-message tool
results must remain atomic; control facts and recovery refs must survive; token
math must use the same estimator as the request path; cancellation/budget failure
must not record a projection; replay must never silently spend again.

---

### Task 1: Build the pure closed-chunk parser and deterministic budget selector

**Files:**

- Modify: `src/linch/compaction.py` — private chunk records/parser, estimator
  adapter, selector, and `TokenBudgetCompaction` constructor/config validation.
- Create: `tests/loop/test_token_budget_compaction.py` — pure grammar and budget
  tests; use no live provider.

**Interfaces:**

```python
TokenBudgetCompaction(
    target_input_tokens: int, *, keep_recent_chunks: int = 8,
    max_summary_output_tokens: int = 2_048,
)
# private: _partition_closed_chunks(messages), _select_closed_run(...)
```

- [ ] Write failing tests proving an assistant tool-use turn plus all matching
  results is indivisible, including parallel ids spread over multiple user
  messages.
- [ ] Write failing tests for orphan, duplicate, unknown, missing, and interrupted
  results: each affected range is protected, not selected.
- [ ] Write failing tests for recent chunk retention, current-message retention,
  control-anchor partitioning, exact token-boundary selection, and non-positive
  constructor values.
- [ ] Implement message-level parsing and oldest-contiguous-run selection.  Treat
  unsupported blocks/open exchanges as protected; do not infer intent from order.
- [ ] Use the runtime estimator when supplied and the existing deterministic
  fallback otherwise.  Reserve summary output and safety margin before choosing
  whole chunks; return no candidate when anchors alone exceed the cap.
- [ ] Run `python -m pytest tests/loop/test_token_budget_compaction.py` and
  `ruff check src/linch/compaction.py tests/loop/test_token_budget_compaction.py`.
- [ ] Request independent review focused on exchange closure and off-by-one token
  boundaries before task 2.

### Task 2: Render, dispatch, and accept one accounted token-budget replacement

**Files:**

- Modify: `src/linch/compaction.py` — defaulted `CompactionContext` facilities,
  deterministic closed-context renderer, strategy execution and acceptance gate.
- Consume (pre-landed accounting Task 4): `src/linch/model_calls.py` — common
  auxiliary caller and paid-projection schema; this token slice does not define
  or modify that accounting contract.
- Modify (integration owner): `src/linch/loop/runner.py` — supply the established
  estimator, window metadata, abort context, and common caller to compaction.
- Create: `tests/loop/test_token_budget_compaction_accounting.py` — fake caller
  tests for gateway, cancellation, and acceptance behavior.

**Interfaces:**

```python
# All fields added after the legacy fields are defaulted.
CompactionContext(..., estimate_tokens=None, auxiliary_caller=None, ...)
await context.auxiliary_caller.text(..., max_output_tokens=...)
```

- [ ] First add failing tests for canonical deterministic rendering, blank/
  oversize/non-shrinking summaries, and a result that still exceeds the final
  input cap.  All must retain the old projection.
- [ ] Add fake-gateway tests showing exactly one admission/limiter call, the
  caller-provided abort signal, no nested provider-slot path, and no direct
  provider fallback while running under Agent control.
- [ ] Implement rendering of role order, tool inputs/results/status, and recovery
  refs while excluding response-chain metadata.  Protect unsupported binary,
  image, thinking, or unknown content in v1 instead of serializing it lossy.
- [ ] Dispatch at most one auxiliary call per compaction invocation.  Accept only
  a nonblank, strictly shrinking, within-cap replacement; otherwise return the
  original messages and let the common caller retain its own receipt/accounting.
- [ ] Preserve direct `strategy.compact(context, provider)` fallback only when no
  runtime auxiliary caller exists; document it as outside Agent accounting.
- [ ] Run `python -m pytest tests/loop/test_token_budget_compaction.py tests/loop/test_token_budget_compaction_accounting.py`,
  `ruff check src/linch/compaction.py src/linch/model_calls.py src/linch/loop/runner.py`,
  and `pyright src/linch/compaction.py src/linch/model_calls.py`.
- [ ] Jointly review with the accounting owner before task 3; this lane consumes
  the pre-landed `CompactionContext.auxiliary_caller` contract and does not invent
  paid-projection schema fields.

### Task 3: Preserve recovery manifests and make accepted projections replay-safe

**Files:**

- Modify: `src/linch/compaction.py` — consume the shared recovery manifest helper
  and emit a versioned token-compaction projection recipe/fingerprints.
- Create/modify (recovery-output lane owned): `src/linch/tools/projection.py` —
  typed `ResultArtifactRef`/`ToolResultProvenance` provenance plus private
  recovery-ref extraction and manifest rendering; token compaction has no raw
  artifact access and does not define storage policy.
- Consume (pre-landed accounting Task 4): its paid-projection schema and snapshot
  persistence contract.  This task records through that contract; it does not
  change `session_log.py` or session-store implementation files.
- Create: `tests/loop/test_token_budget_compaction_recovery.py` and
  `tests/storage/test_token_budget_compaction_resume.py`.

**Interfaces:**

```python
# Private API from src/linch/tools/projection.py.
extract_recovery_refs(messages) -> list[ResultArtifactRef]
render_recovery_manifest(refs) -> Message
```

- [ ] Add failing tests that a selected closed exchange retains typed artifact refs
  in both summary source and replacement without raw artifact access.  Pin the
  narrow scheduler guard: immediate recovery output is not recursively
  projected/offloaded, but an old closed recovery page may be summarized/elided
  while its manifest/full-history ref remains.
- [ ] Add failing snapshot/resume tests: exact source fingerprint reuses the
  accepted projection without a provider call; changed source/config fails closed;
  cancellation/crash before acceptance leaves no partial projection.
- [ ] Implement the recipe with strategy/version/config, source and target
  fingerprints, recovery-manifest fingerprint, receipt, and projection id, using
  the pre-landed accounting schema and typed provenance rather than raw artifacts.
- [ ] Coordinate write ordering with the accounting contract: accepted charge and
  projection recipe must survive resume consistently; rejected/unknown/cancelled
  calls cannot look like an accepted compacted view.
- [ ] Run `python -m pytest tests/loop/test_token_budget_compaction_recovery.py tests/storage/test_token_budget_compaction_resume.py`,
  then the focused suites from tasks 1–2.  Run `ruff check` on only changed files
  and `pyright src/linch/compaction.py src/linch/session_log.py`.
- [ ] Request independent cross-lane review of recovery ref retention, crash
  ordering, and source-fingerprint fail-closed behavior.

### Task 4: Expose the opt-in strategy, document it, and prepare (but do not publish) a release

**Files:**

- Modify: `src/linch/__init__.py` and `tests/test_public_api.py` — add the approved
  deliberate `linch.__all__` export for `TokenBudgetCompaction`.
- Modify: `docs/architecture/compaction.md`, `docs/architecture/invariants.md`,
  `docs/usage/README.md`, `docs/usage/production.md`, and `docs/versioning.md`.
- Modify: `CHANGELOG.md`, `pyproject.toml`, `uv.lock`, and `src/linch/_version.py`
  only after the root integration ledger chooses the prerelease/version policy.
- Create/modify: focused examples or tests only if needed to demonstrate explicit
  configuration; no live-provider example is required.

**Interfaces:**

```python
from linch import TokenBudgetCompaction
agent = Agent(..., compaction=TokenBudgetCompaction(target_input_tokens=...))
```

- [ ] Add/adjust public-surface tests and docs that distinguish immutable full
  history from a compacted provider projection, direct standalone fallback from
  accounted Agent execution, and the no-op safety cases.
- [ ] Document configuration, retained anchors/tool exchanges/recovery refs,
  cancellation and replay guarantees, and explicit non-goals.  State that default
  prompts/persona and default compaction are unchanged.
- [ ] Run `python -m pytest tests/test_public_api.py tests/loop/test_compaction_ladder.py tests/loop/test_token_budget_compaction.py tests/loop/test_token_budget_compaction_accounting.py tests/loop/test_token_budget_compaction_recovery.py tests/storage/test_token_budget_compaction_resume.py`,
  followed by `ruff check . && ruff format --check .` and `pyright`.
- [ ] Perform release-preparation only: select version in the root ledger, update
  changelog/lock/version files coherently, and verify package metadata.  A worker
  must not publish, tag, stage, or commit.
- [ ] Request final independent review against the linked spec before integration.
