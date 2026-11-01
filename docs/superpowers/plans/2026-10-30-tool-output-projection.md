# Recoverable Tool-Output Projection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add opt-in per-tool output projection with scoped retained originals, bounded recovery pages, and durable provenance without changing legacy tool output behavior.

**Architecture:** A policy on a tool opts it into deterministic preview or V2 JSON-focus projection. A separate scoped artifact store retains the final source output; scheduler constructs a bounded ToolResultBlock with typed provenance, while a permission-checked recovery tool reads pages. Event/checkpoint codecs carry the exact projected block through a crash; full history remains append-only.

**Tech Stack:** Python >=3.10, asyncio, existing ToolRegistry/ToolContext/Scheduler, FileBackend adapters, pytest, Ruff, Pyright, uv.

**Spec:** ../specs/2026-10-30-tool-output-projection.md

## Global Constraints

- Keep the feature opt-in: no policy means identical legacy ToolResult, V2, and result_offload behavior.
- Python >=3.10; public dataclasses are frozen=True, slots=True; runtime paths remain async and dependency-free.
- Preserve neutral persona, provider wire formats, full_history immutability, tool pairing, permission ordering, and existing public RunBudget.charge semantics.
- Artifact scope comes only from trusted host configuration plus runtime session ID, never tool/model/ref input; ref alone is not authorization.
- Do not add model-generated summaries, ModelCallEvent/UsageEvent charges, provider slots, or token/chunk compaction in this feature.
- “No recompression” applies to the immediate projection/offload pipeline only; ordinary historical provider-view compaction may summarize old recovery pages while immutable full_history and micro-copy provenance retain the typed ref.
- Preserve linch.model_calls.v1 and linch.reliability checkpoint ownership; this feature introduces no extension-state namespace.
- Do not mutate the source checkout's docs/ROADMAP.md, plans/, or openhuman/; work only in the managed integration worktree.
- Each task stages only its owned files, passes focused tests before review, and receives fresh independent review before its successor. Workers do not publish.

## Review Focus

- A store write succeeds but the process stops before ToolCallEndEvent; Task 4 must prove the orphan is unreachable and never becomes a ref or authority.
- A policy changes while a durable run is resumed; Task 3 must fingerprint it in the run contract and Task 4 must reject stale recovery before a provider call.
- A ref from another tenant/session is valid-looking; Task 2 must return the same unavailable response as a missing ref without backend-path leakage.
- A post-tool hook/precomputed result bypasses the normal execution branch; Task 4 must project it exactly once after mutation rather than reintroducing full raw content.
- A recovery page is oversized or old enough for existing offload/compaction; Task 4 must prove scheduler bypasses projection/offload and preserves its provenance/ref.

---

### Task 1: Projection policy, provenance, and strict block codecs

**Files:**
- Create: `src/linch/tools/projection.py`
- Create: `tests/tools/test_tool_output_projection_contract.py`
- Modify: `src/linch/types.py`, `src/linch/tools/base.py`, `src/linch/tools/function.py`, `src/linch/tools/registry.py`, `src/linch/tools/__init__.py`, `src/linch/__init__.py`, `tests/test_public_api.py`

**Interfaces:**
- Consumes: `ToolResultBlock`, `ToolOutput`, `ToolOutputError`, `FunctionTool`, `tool`, and registry V2 validation.
- Produces: public `ToolOutputProjectionPolicy`, `ResultArtifactRef`, and `ToolResultProvenance`; private policy fingerprint/JSON-pointer selection/projection rendering helpers.
- Produces: optional `ToolResultBlock.provenance` and strict `block_to_dict`/`block_from_dict` handling with legacy omission.
- Produces: optional duck-typed `output_projection` accepted by FunctionTool/@tool and validated at ToolRegistry registration.

- [ ] **Step 1: Write failing policy/codec tests**

Create direct tests for a legacy preview policy, a V2 focus policy with two RFC 6901 pointers, invalid bounds, duplicate/root/malformed pointers, focus on a non-V2 tool, and ToolOutputError. Assert that old blocks round-trip unchanged, a block with provenance round-trips as a detached copy, and `dataclasses.replace(block, content=...)` preserves provenance.

- [ ] **Step 2: Run the focused test file to verify RED**

Run: `pytest tests/tools/test_tool_output_projection_contract.py -v`

Expected: FAIL because the policy/provenance types and codec fields do not yet exist.

- [ ] **Step 3: Implement contracts and validation**

Define the exact public records from the spec in `tools/projection.py`. Add `output_projection: ToolOutputProjectionPolicy | None` to FunctionTool and `@tool`, preserve it on construction, and make registry validation enforce preview/focus rules. Implement deterministic canonical pointer-map rendering and a stable policy fingerprint, but do not add storage or scheduler behavior yet. Update ToolResultBlock serialization in the same task so no consumer sees an unencoded new type.

- [ ] **Step 4: Verify GREEN and compatibility**

Run: `pytest tests/tools/test_tool_output_projection_contract.py tests/tools/test_tool_output_v2_foundation.py tests/test_public_api.py -v`

Expected: PASS; no-policy FunctionTool and existing serialized blocks retain their prior behavior.

- [ ] **Step 5: Commit and request focused review**

Commit owned files with `feat: add tool output projection contracts`. Report exact test/lint/type outputs and obtain independent review before Task 2.

### Task 2: Scoped artifact stores and the recovery reader

**Files:**
- Create: `src/linch/tools/result_store.py`, `src/linch/tools/recovery.py`
- Create: `tests/tools/test_tool_result_store.py`, `tests/tools/test_read_tool_result.py`
- Modify: `src/linch/tools/projection.py`, `src/linch/tools/__init__.py`, `src/linch/__init__.py`

**Interfaces:**
- Consumes: Task 1 ref/provenance records and existing `FileBackend`, `ToolContext`, `ToolResult`, and `AbortContext` contracts.
- Produces: public `ResultArtifactScope`, `ResultArtifactPage`, `ToolResultArtifactStore`, `ToolOutputProjectionConfig`, `InMemoryToolResultArtifactStore`, and `FileBackendToolResultArtifactStore`.
- Produces: private normalized `ToolResultArtifact` and `ReadToolResult` with `projection_passthrough=True`.
- Produces: `store.put(scope, artifact, signal)` idempotent by `(scope, run_id, tool_use_id, digest)` and `store.read_page(scope, ref, cursor, max_chars, signal)` with opaque bounded cursors.

- [ ] **Step 1: Write failing store and reader tests**

Use a fixed trusted scope resolver and a direct `ToolContext`. Assert idempotent same-digest put, conflicting same-call put failure, fixed page bounds/next cursor, wrong-scope/session/missing/corrupt-ref indistinguishability, cancellation propagation, and no scope/backend path in a ref. Test `ReadToolResult` success footer/provenance, invalid page bound, and one generic unavailable error for all denied store cases. Add a test that a public session FileBackend identity is rejected by configuration helper while a private SQLite FileBackend adapter advertises durability.

- [ ] **Step 2: Run focused tests to verify RED**

Run: `pytest tests/tools/test_tool_result_store.py tests/tools/test_read_tool_result.py -v`

Expected: FAIL because no store or recovery tool exists.

- [ ] **Step 3: Implement stores and recovery tool**

Implement a bounded in-memory store plus a FileBackend adapter whose backend paths are private implementation detail, scope/session namespaced, and never returned. Enforce config maxima at both store and reader boundaries. Make ReadToolResult a normal read-scope tool that obtains scope only from its runtime ToolContext/config, calls the store with the AbortContext signal, and returns a page marked `mode="recovery"`; do not call a model or use the filesystem tools.

- [ ] **Step 4: Verify GREEN**

Run: `pytest tests/tools/test_tool_result_store.py tests/tools/test_read_tool_result.py tests/tools/test_tool_output_projection_contract.py -v`

Expected: PASS; all unauthorized cases are observationally equivalent to not found.

- [ ] **Step 5: Commit and request focused review**

Commit `feat: add scoped tool result recovery store`; report tests and request independent review before Task 3.

### Task 3: Agent configuration, registration, and durable run contract

**Files:**
- Modify: `src/linch/agent.py`, `src/linch/run_store.py`, `src/linch/tools/registry.py`, `src/linch/tools/__init__.py`, `src/linch/__init__.py`
- Create: `tests/tools/test_tool_output_projection_config.py`
- Modify: `tests/loop/test_run_contract_integration_v2.py`, `tests/test_public_api.py`

**Interfaces:**
- Consumes: Task 1 policy fields and Task 2 projection config/store/recovery tool.
- Produces: `Agent(..., tool_output_projection: ToolOutputProjectionConfig | None = None)` and matching AgentOptions state, with the recovery tool registered only when config is set.
- Produces: construction validation for policy/config pairing, recovery name collision, durable-store requirement, bounds, and unsafe session/execution filesystem identity.
- Produces: a normalized `policies["tool_output_projection"]` run-contract entry containing codec version, effective per-tool policy fingerprints, recovery name, bounds, and durable-store requirement.

- [ ] **Step 1: Write failing configuration and contract tests**

Create agents with no policy/config, policy without config, config with no policy, recovery-name collision, non-durable store under the default durable requirement, and an explicitly allowed ephemeral test config. Assert the no-policy agent has no recovery schema/tool and keeps existing result_offload setup. Persist a run, alter a policy/recovery bound, and assert resume contract mismatch before provider dispatch.

- [ ] **Step 2: Run focused tests to verify RED**

Run: `pytest tests/tools/test_tool_output_projection_config.py tests/loop/test_run_contract_integration_v2.py -v`

Expected: FAIL because Agent setup neither validates nor fingerprints projection configuration.

- [ ] **Step 3: Implement configuration and contract wiring**

Add the one Agent option, validate all configured registry policies after tools/filesystem are resolved, and register exactly one private recovery tool through the ordinary registry. Do not register it globally or mutate a caller-owned registry unexpectedly. Include a deterministic projection contract in existing RunContract policy data; do not create a checkpoint namespace or alter model-input snapshots.

- [ ] **Step 4: Verify GREEN and legacy isolation**

Run: `pytest tests/tools/test_tool_output_projection_config.py tests/loop/test_run_contract_integration_v2.py tests/test_public_api.py -v`

Expected: PASS; unconfigured agents have byte-compatible tool surfaces and resume behavior.

- [ ] **Step 5: Commit and request focused review**

Commit `feat: configure scoped tool output projection`; report evidence and obtain independent review before Task 4.

### Task 4: Scheduler projection, end-event codec, and crash-safe result recovery

**Files:**
- Modify: `src/linch/scheduler.py`, `src/linch/events.py`, `src/linch/loop/checkpoint.py`, `src/linch/loop/runner.py`, `src/linch/errors.py`, `src/linch/filesystem/offload.py`
- Modify: `src/linch/tools/projection.py`, `src/linch/tools/recovery.py`
- Create: `tests/tools/test_tool_output_projection_runtime.py`, `tests/loop/test_tool_output_projection_resume.py`
- Modify: `tests/tools/test_tool_output_v2_foundation.py`, `tests/loop/test_recovery_cursor.py`, relevant existing offload/event tests only for documented invariants

**Interfaces:**
- Consumes: configured Agent projection config, Task 1 policy/provenance, Task 2 store/reader, and existing post-tool hook/final outcome path.
- Produces: one `project_tool_result(...) -> ToolExecutionOutcome` boundary after final post-hook mutation, before ToolCallEndEvent/result-message append.
- Produces: optional `ToolCallEndEvent.result_block: ToolResultBlock`; event codecs serialize it with `block_to_dict`, and checkpoint recovery prefers it to legacy result-string reconstruction.
- Produces: private `ToolProjectionPersistenceError` terminal path whose safe error says the tool already executed and must not be automatically rerun, preserves all settled parallel sibling outcomes and valid pairing blocks, then prevents another provider request.

- [ ] **Step 1: Write failing end-to-end and crash tests**

Use fake providers and an opted-in tool returning a large result. Assert raw storage happens before the end event; history/provider input contains only preview/footer/provenance; ToolCallEndEvent keeps `result_block`; and resume after end-event-before-user-append reuses the ref without source-tool reexecution. Cover V2 focus, precomputed/post-hook-mutated output, policy-free existing offload, recovery page passthrough, normal permission denial before store access, and same-session durable reload.

Add failure injection for artifact put and cancellation during put. Assert no raw/ref/backend detail reaches history or the event result block; the fixed safe error says the tool already executed and must not be automatically rerun; no later provider request occurs; and the run ends terminally. Add a parallel batch case where one sibling settles successfully before another artifact write fails: assert the final paired tool-result message retains the successful block, safely closes all unfinished calls, then terminates. Add a copied block/micro-compaction test proving provenance survives `replace`/provider-view handling without introducing a token-compaction change.

- [ ] **Step 2: Run focused tests to verify RED**

Run: `pytest tests/tools/test_tool_output_projection_runtime.py tests/loop/test_tool_output_projection_resume.py -v`

Expected: FAIL because scheduler currently builds a plain ToolResultBlock and end-event recovery loses projected metadata.

- [ ] **Step 3: Implement one final-output projection boundary**

Refactor only enough scheduler branches to route final successful legacy/V2/precomputed/post-hook outcomes through one projection boundary. Skip existing `_maybe_offload_block` for configured calls; retain it unchanged for all others. Store first, then build a replaced block and durable end event. Mark ReadToolResult passthrough so neither new projection nor existing offload applies. On persistence/cancellation failure, use the specified fixed safe terminal branch, drain/persist already-settled parallel siblings, synthesize safe pairing blocks for unfinished calls, append the valid paired result message, and only then terminate without model continuation.

Update `ToolCallEndEvent` encode/decode and `_tool_result_block_from_end` in the same task. Use the projected block for `completed_tool_results`, checkpoint save, and eventual append; never reconstruct a ref from display text.

- [ ] **Step 4: Verify GREEN and integration seams**

Run: `pytest tests/tools/test_tool_output_projection_runtime.py tests/loop/test_tool_output_projection_resume.py tests/loop/test_recovery_cursor.py tests/tools/test_tool_output_v2_foundation.py -v`

Then run: `pytest && ruff check . && ruff format --check . && pyright`

Expected: PASS. Confirm no ModelCallEvent/UsageEvent/budget delta is emitted by deterministic projection or page reads.

- [ ] **Step 5: Commit and request whole-feature review**

Commit `feat: project tool output through scoped recovery artifacts`. Submit Tasks 1–4 for an independent whole-feature review focused on scope isolation, crash ordering, event/checkpoint codec fidelity, and legacy compatibility.

### Task 5: Documentation, migration, and prerelease preparation

**Files:**
- Modify: `docs/architecture/tool-protocol.md`, `docs/architecture/invariants.md`, `docs/architecture/turn-lifecycle.md`, `docs/usage/tools.md`, `docs/usage/events.md`, `docs/usage/filesystem.md`, `CHANGELOG.md`, `pyproject.toml`, `uv.lock`
- Create: `docs/migration-3.0.md` if it does not already exist
- Test: relevant examples/public API tests and built wheel smoke coverage

**Interfaces:**
- Consumes: reviewed Tasks 1–4 behavior and exact public API names.
- Produces: user-facing configuration/security/migration documentation and the next feature-by-feature 3.0 prerelease metadata (`3.0.0a2` after the accounting prerelease), without publishing.

- [ ] **Step 1: Document operational and security semantics**

Document opt-in policy configuration, private artifact-store requirement, trusted scope resolver, tenant/session isolation, ordinary recovery-tool permission checks, page bounds, durable-vs-ephemeral behavior, no cross-session fork recovery, deterministic focus rules, no-model/no-budget behavior, and terminal persistence failure. State that the safe failure says the tool already executed and must not be automatically rerun without exposing raw/backend details; settled parallel siblings remain paired before terminal stop. State that existing result_offload is unchanged for unconfigured tools, recovery pages are never immediately reprojected/reoffloaded, and ordinary historical provider-view compaction may later summarize old pages while full-history provenance/ref remains.

- [ ] **Step 2: Prepare prerelease metadata and migration notes**

Set the package to `3.0.0a2`, update `uv.lock`, add a changelog section, and add a focused migration note. Do not claim model summaries, token chunk compaction, cross-session sharing, exactly-once external storage, or later feature lanes as shipped.

- [ ] **Step 3: Run release validation**

Run: `pytest && ruff check . && ruff format --check . && pyright && uv lock --check`

Build wheel/sdist in isolation, install the wheel in a clean environment, and run public-import plus scoped-artifact smoke tests without live credentials. Record exact outputs; do not waive a failure.

- [ ] **Step 4: Commit and hand off release evidence**

Commit `release: prepare 3.0.0a2 tool output projection`. Give root the verification log and review verdicts. Root alone performs any tag, remote publication, or artifact verification.

## Self-review coverage

The five Review Focus conditions map to Tasks 4, 3/4, 2, 4, and 4 respectively. Every public type is introduced with its codec/export in the same slice that consumes it; scheduler event metadata is encoded/decoded in Task 4 rather than deferred. The plan deliberately leaves model-summary accounting and token/chunk compaction to later independently reviewed specifications.
