# Tool Discovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add opt-in, host-scoped tool metadata search and policy-approved durable activation without leaking schemas, bypassing permissions, or granting authority before an ordinary tool-result append.

**Architecture:** `tool_surface.py` owns the small host API, catalog, activation policy boundary, immutable offered-surface snapshots, and full-history fact fold. `types.py` and `SessionLog` own strict durable control-field codecs/projection; the runner constructs one snapshot before each provider request and consumes a private approval only at the result append boundary. Existing compaction remains a provider-view optimization, never an authority source.

**Tech Stack:** Python >=3.10, existing async agent loop, `ToolRegistry`, SessionStore/RunStore, configured MCP/skills, pytest, Ruff, Pyright, uv.

**Spec:** ../specs/2026-10-30-tool-discovery.md

## Global Constraints

- Python >=3.10; keep provider/runtime paths async and use duck-typed tool protocols.
- `tool_discovery=None` preserves existing behavior; do not add a FeatureFlags switch.
- Public API is only the nine names in the spec through lazy `linch.__all__`; private control/codec helpers stay private.
- Pre-register only host-supplied native/MCP tools; never accept model-selected code, package, server, URL, command, or tool object.
- `tool_search` is the sole discovery/activation control tool; skills remain metadata plus the existing `Skill` loader.
- Full durable history is authority.  Checkpoints, run events, provider views, snapshots, raw/offloaded artifacts, and compaction summaries never grant activation.
- Preserve the existing copy-on-write `full_history`/`provider_view` model.  Do not wait for raw-artifact or token-budget-compaction work; implement only the shared control-field projection/codec and pairing checks this feature consumes.
- Plans 03–06 are bounded gates: strict store codecs, isolated exact-turn checkpoints, assembled append/reload trace, and read-only pre-provider resume validation.
- Every task must compile, pass its focused tests, and receive a fresh review before the next task. Root owns integration, branch management, release, and final review; workers do not publish.
- Do not touch the source checkout’s `docs/ROADMAP.md`, `plans/`, or `openhuman/` paths.

## Review Focus

- A legacy/third-party session store that silently drops a control block must reject discovery configuration rather than load a usable but unprotected session; Task 1 pins this.
- A context builder returning a replacement registry must not introduce a schema outside the immutable host surface; Task 3 pins this.
- A policy decision containing an unreturned or stale target must produce no durable facts; Task 4 pins this.
- A ToolCallEnd event or checkpoint written before the user tool-result append must never activate a tool after resume; Task 4 pins all crash cuts.
- Default/Detailed/micro compaction, snapshot reload, and fork must derive authority only from full history; Task 5 pins each current path.

---

### Task 1: Strict durable control-field codec and provider projection (Plan 03 gate)

**Files:**
- Create: `src/linch/tool_surface.py` (durable-codec helpers only; no public Agent wiring yet), `tests/storage/test_tool_surface_codec.py`
- Modify: `src/linch/types.py`, `src/linch/session_log.py`, `src/linch/sessions/store.py`, `src/linch/sessions/memory.py`, `src/linch/sessions/sqlite.py`, `src/linch/sessions/postgres.py`
- Modify tests: `tests/test_session_log.py`, `tests/storage/test_sessions.py`, `tests/storage/test_serialization_versioning.py`

**Interfaces:**
- Consumes: existing `Message`, `ToolResultBlock`, `message_to_dict/from_dict`, `SessionLog`, `ProviderViewSnapshot` codecs.
- Produces: immutable `ToolActivation`, private `ToolSurfaceReaderGuard`, strict activation/guard encode-decode helpers, and `ToolResultBlock.tool_activations: tuple[ToolActivation, ...] = ()`.
- Produces: one shared provider-projection function used by `SessionLog.append`, `SessionLog.seed`, and provider-snapshot restoration; it strips guard blocks, clears tool-result activations, and drops empty projected messages while retaining complete full history.
- Produces: private `runtime_control_block_codec_version = 1` capability on every built-in session store; discovery callers can reject an unadvertised store.

- [ ] **Step 1: Write failing codec/projection/store tests**

Create a fixed `ToolActivation` fixture with every spec field and a guard
fixture.  Assert exact message and snapshot round trips preserve facts in full
history, provider projection has neither guard nor fact, an activation field
with malformed/unknown version fails, and a pre-feature decoder fails on the
guard type.  Add InMemory and SQLite reopen/copy-isolation cases; use the
Postgres codec/unit seam without requiring live credentials.  Pin that a store
without capability version 1 is rejected by the future configuration helper.

- [ ] **Step 2: Verify RED**

Run: `pytest tests/storage/test_tool_surface_codec.py tests/test_session_log.py -v`

Expected: FAIL because durable control types, strict codecs, and projection
helper do not exist.

- [ ] **Step 3: Implement all consumed control types/codecs and projection in this slice**

Add the types and strict `tool_activations` wire handling in `types.py`; retain
the old empty-field wire shape for ordinary results.  Add the unknown-to-old-
reader guard block.  Make SessionLog own separate full/projected graphs through
one helper; do not let its normal deep copy make control fields provider-visible.
Set and test the built-in store capability constant.  Do not expose discovery
or alter compaction behavior yet.

- [ ] **Step 4: Verify GREEN and compatibility**

Run: `pytest tests/storage/test_tool_surface_codec.py tests/test_session_log.py tests/storage/test_sessions.py tests/storage/test_serialization_versioning.py -v`

Then run: `ruff check src/linch/types.py src/linch/session_log.py src/linch/sessions && ruff format --check src/linch/types.py src/linch/session_log.py src/linch/sessions && pyright src/linch/types.py src/linch/session_log.py src/linch/sessions`

Expected: PASS; ordinary session/snapshot codecs remain compatible and only
control-bearing records take the strict path.

- [ ] **Step 5: Request fresh review**

Report the full-history/provider-view invariants, store-capability behavior,
RED/GREEN evidence, and owned files.  Do not begin catalog work until approved.

### Task 2: Public host catalog and search-only control tool

**Files:**
- Modify: `src/linch/tool_surface.py`, `src/linch/agent.py`, `src/linch/tools/function.py`, `src/linch/tools/registry.py`, `src/linch/tools/skill.py`, `src/linch/mcp/config.py`, `src/linch/mcp/tool.py`, `src/linch/mcp/client.py`, `src/linch/__init__.py`
- Create: `tests/tools/test_tool_discovery_catalog.py`, `tests/mcp/test_tool_discovery_mcp.py`, `tests/skills/test_tool_discovery_skills.py`
- Modify tests: `tests/tools/test_function_tools.py`, `tests/tools/test_registry_ext.py`, `tests/test_public_api.py`, `tests/mcp/test_mcp_api_contract.py`, `tests/skills/test_builtins.py`

**Interfaces:**
- Consumes: Task 1 durable `ToolActivation` re-export and store capability check.
- Produces: exact public dataclasses/protocols `ToolDiscoveryConfig`, `ToolCatalogScope`, `ToolCatalogRequest`, `ToolCatalogPolicy`, `ToolActivationCandidate`, `ToolSearchActivationRequest`, `ToolSearchActivationDecision`, and `ToolSearchActivationPolicy`.
- Produces: registration metadata `activation_required`, `activation_id`, `activation_version`; canonical descriptor fingerprint; host-only MCP catalog metadata.
- Produces: private `_ToolSearchTool` with fixed schema, deterministic metadata ranker, and search-only behavior. It is not offered in an Agent request until Task 3 wires a surface.

- [ ] **Step 1: Write failing public/catalog tests**

Pin all nine root exports and lazy import behavior.  Unit-test catalog scope
validation, query normalization/ranking/tie-break, 1..10 and 1..8 limits, and
the exact metadata-only response shape.  Test native decorator metadata,
identity-less deferred-tool rejection, configured MCP raw-name validation and
stable wrapper identity, no `add_mcp_servers`/connection side effect from
search, and skill metadata/scope callback while preserving existing substitution
and overlay behavior.  Assert discovery suppresses the static all-skills
listing but disabled discovery does not.

- [ ] **Step 2: Verify RED**

Run: `pytest tests/tools/test_tool_discovery_catalog.py tests/mcp/test_tool_discovery_mcp.py tests/skills/test_tool_discovery_skills.py tests/test_public_api.py -v`

Expected: FAIL because the public configuration, catalog descriptors, and
private control tool are absent.

- [ ] **Step 3: Implement the minimal host-facing catalog layer**

Define only the spec API in `tool_surface.py`, add `tool_discovery` to both
Agent construction forms, and validate built-in SessionStore capability when
configured.  Teach FunctionTool/decorator/registry and configured MCP wrappers
the exact metadata contract.  Leave MCP connection ownership in host code.
Retain `SkillTool`; give it a discovery scope predicate and suppress only its
static listing when discovery is enabled.  Add the lazy exports; no activation
fact is written in this task.

- [ ] **Step 4: Verify GREEN and default compatibility**

Run: `pytest tests/tools/test_tool_discovery_catalog.py tests/mcp/test_tool_discovery_mcp.py tests/skills/test_tool_discovery_skills.py tests/tools/test_function_tools.py tests/tools/test_registry_ext.py tests/mcp/test_mcp_api_contract.py tests/skills/test_builtins.py tests/test_public_api.py -v`

Then run Ruff/format/Pyright for the modified modules.  Verify a bare Agent
still has no discovery control tool and retains its old skill listing.

- [ ] **Step 5: Request fresh review**

Review the public surface, metadata secrecy, MCP host-only rule, and skill
compatibility before any request-loop integration.

### Task 3: Immutable offered surface shared by request and scheduler (Plan 04 gate)

**Files:**
- Modify: `src/linch/tool_surface.py`, `src/linch/session.py`, `src/linch/loop/request.py`, `src/linch/loop/streaming.py`, `src/linch/loop/runner.py`, `src/linch/scheduler.py`, `src/linch/run_store.py`
- Create: `tests/loop/test_tool_discovery_surface.py`
- Modify tests: `tests/loop/test_hardening.py`, `tests/tools/test_scheduler_security_v2.py`, `tests/storage/test_run_contract.py`, `tests/storage/test_run_store.py`

**Interfaces:**
- Consumes: Task 2 catalog policy and registry metadata.
- Produces: private immutable `ToolSurfaceSnapshot` plus strict checkpoint dictionary codec in `linch.tool_surface` under `extension_state["linch.tool_surface"]`.
- Produces: async `prepare_tool_surface(session, run_id, turn_index, context) -> ToolSurfaceSnapshot`, invoked before every fresh/rebuilt provider request.
- Produces: request selection and scheduler validation that consume the same exact snapshot; `ContextBuildResult.selected_tools` only narrows names in discovery mode.
- Produces: bumped RunContract schema with `policies.tool_surface` configuration identity, preserving legacy behavior when discovery is disabled.
- Produces: a guard-only durable full-history message before the first request that can offer `tool_search`; Task 1 removes it from the provider projection.

- [ ] **Step 1: Write failing surface/checkpoint tests**

With a fake async catalog policy, assert initial requests expose only active
scoped schemas plus `tool_search`; deferred schemas are absent; an out-of-scope
active tool is absent; a returned replacement registry cannot add one; and a
provider-emitted unoffered name is rejected before permission/hooks.  Pin
snapshot immutability, detached checkpoint copies, extension namespace merge,
registry generation/descriptor mismatch failure, and RunContract differences.
Assert the guard is durable before the first `tool_search` schema and absent
from the provider wire. Exercise normal turns and the existing
compaction-retry request rebuild.

- [ ] **Step 2: Verify RED**

Run: `pytest tests/loop/test_tool_discovery_surface.py tests/loop/test_hardening.py tests/tools/test_scheduler_security_v2.py tests/storage/test_run_contract.py -v`

Expected: FAIL because request assembly and scheduler use separate legacy
allowlists rather than an immutable common surface.

- [ ] **Step 3: Implement async preparation and one offered-set authority**

Append the Task 1 guard before the first discovery-enabled request, then
resolve scope after context build, fold only currently valid default-active
state (no durable activation yet), apply name-only context/skill narrowing, and
store an ordered descriptor snapshot before provider dispatch. Keep
`_build_turn_request` synchronous by making it consume the prepared snapshot;
thread async preparation through normal, fallback, and compaction-retry paths.
Scheduler must compare its calls against that snapshot before canonical input,
permission, pipeline, hook, or execution.  Preserve legacy
`current_turn_allowed_tools` behavior outside this feature.

- [ ] **Step 4: Verify GREEN and checkpoint isolation**

Run the RED command plus `pytest tests/storage/test_run_store.py tests/loop/test_compaction_ladder.py -v`, followed by targeted Ruff/format/Pyright.

Expected: PASS; exact-turn snapshots cannot be mutated through a loaded
checkpoint and a search-only feature run never exposes a deferred schema.

- [ ] **Step 5: Request fresh review**

Review the async sequencing, scope authority, request/scheduler equality, and
RunContract change before durable activation is added.

### Task 4: Policy-approved durable tool_search activation (Plan 05 gate)

**Files:**
- Modify: `src/linch/tool_surface.py`, `src/linch/tools/base.py`, `src/linch/scheduler.py`, `src/linch/events.py`, `src/linch/loop/checkpoint.py`, `src/linch/loop/runner.py`, `src/linch/session.py`
- Create: `tests/loop/test_tool_discovery_activation.py`
- Modify tests: `tests/tools/test_scheduler_v2.py`, `tests/tools/test_scheduler_security_v2.py`, `tests/storage/test_serialization_versioning.py`

**Interfaces:**
- Consumes: Task 1 strict fact/guard codecs and Task 3 prepared snapshot.
- Produces: a guard in every activation result; Task 3 already guarantees the first-message guard before `tool_search` is offered.
- Produces: a private `_PendingToolSurfaceApproval`, `ToolExecutionOutcome` private field, and non-serialized `ToolCallEndEvent` private carrier; neither is a public or durable authority channel.
- Produces: `ToolContext.tool_use_id` populated by the scheduler, enabling `ToolSearchActivationRequest.tool_use_id` without parsing the idempotency key.
- Produces: `attach_committed_tool_activations(result_message, ...)` immediately before ordinary `session.append`; it revalidates scope/registry and atomically copies facts or none.

- [ ] **Step 1: Write failing activation/append-boundary tests**

Use a fake provider that calls `tool_search`, then inspect the next request.
Assert a valid policy subset becomes available only on that next request; an
unreturned/duplicate/over-limit/exception/denied/stale decision grants none;
and activation stays subject to normal permission and hooks.  Add direct
forgery cases through result text, metadata, custom tool, event, and checkpoint.
Add an assembled offline trace for ToolCallEnd persistence, crash before result
append, append failure, crash after append/reload, and cache deletion; expected
folded grants are respectively zero, zero, zero, and one.

- [ ] **Step 2: Verify RED**

Run: `pytest tests/loop/test_tool_discovery_activation.py tests/tools/test_scheduler_v2.py tests/tools/test_scheduler_security_v2.py -v`

Expected: FAIL because no policy token can survive post-tool hooks without
becoming an unsafe event/checkpoint grant.

- [ ] **Step 3: Implement private approval-to-append flow and all codecs it consumes**

Have the runtime-owned control object create an opaque approval only after its
policy subset and post-policy scope/descriptor recheck.  Preserve it through a
successful post-hook outcome, clear it for every failed/blocked/aborted path,
and explicitly omit it from event/checkpoint serialization in this same task.
At the runner’s result-message append boundary, consume it once, recheck live
scope/registry, attach facts and guard, then append.  Event replay and
`_tool_result_block_from_end` must reconstruct ordinary results with no facts.
Do not change generic ToolResult metadata into an authority format.

- [ ] **Step 4: Verify GREEN and Plan 05 trace**

Run the RED command plus `pytest tests/storage/test_tool_surface_codec.py tests/storage/test_sessions.py -v`; then Ruff/format/Pyright for every modified module.

Expected: PASS; no pre-append checkpoint/event can grant, and all facts are
attached only to the ordinary durable user tool-result message.

- [ ] **Step 5: Request fresh review**

Review token non-forgeability, hook/permission behavior, atomic subset commit,
and each crash boundary before recovery/fork work.

### Task 5: Read-only resume validation, fork, and current compaction proof (Plan 06 gate)

**Files:**
- Modify: `src/linch/tool_surface.py`, `src/linch/loop/runner.py`, `src/linch/sessions/fork.py`, `src/linch/compaction.py` only if a current transform bypasses Task 1’s common projection helper
- Create: `tests/loop/test_tool_discovery_resume.py`
- Modify tests: `tests/storage/test_session_fork.py`, `tests/loop/test_compaction_provider_agnostic.py`, `tests/loop/test_compaction_response_chaining.py`, `tests/loop/test_compaction_ladder.py`, `tests/test_session_log.py`

**Interfaces:**
- Consumes: Task 4 full-history facts, reader guards, snapshot codec, and append boundary.
- Produces: `validate_tool_surface_resume(session, run_id, checkpoint, opts) -> None`, read-only and fail-closed.
- Produces: full-history pairing/fact validator reused by fork; no copied approval/policy/registry/provider snapshot/exact-turn snapshot.
- Produces: current-compaction compatibility proof: all provider-view transforms use Task 1 projection rules or operate on already-projected blocks.

- [ ] **Step 1: Write failing resume/fork/compaction tests**

Pin that `resume_loop` validates contract, guard, full-history pairing, facts,
live scope/descriptor, and exact in-flight snapshot before provider prepare,
hooks, permissions, or tools.  Test missing/replaced descriptor, changed output
contract/scope/policy, unsupported guard/fact, and stale snapshot with counters
proving zero side effects.  Test fork prefixes before/after an activation,
tail-fork copy, malformed facts, and scope mismatch.  Run Default,
Detailed, and micro compaction; reload a provider snapshot; then prove the same
full-history activation fingerprint and no fact in provider view.

- [ ] **Step 2: Verify RED**

Run: `pytest tests/loop/test_tool_discovery_resume.py tests/storage/test_session_fork.py tests/loop/test_compaction_provider_agnostic.py tests/loop/test_compaction_response_chaining.py tests/loop/test_compaction_ladder.py -v`

Expected: FAIL because current resume prepares the provider too early and fork/
compaction do not yet validate the feature-specific durable invariants.

- [ ] **Step 3: Implement bounded pre-provider validation**

Move provider preparation in `resume_loop` below run-contract and read-only
surface validation.  Validate immediate assistant/use-result pairing, guard
presence/version, fact/source identity, descriptors, scope, and checkpoint
snapshot before running normal resume machinery.  Extend fork validation with
the same full-history fold.  Do not add an artifact store, token-budget
compactor, or new summary format; modify current compaction only if needed to
route retained blocks through the common projection helper.

- [ ] **Step 4: Verify GREEN and full current-path proof**

Run the RED command plus `pytest tests/test_session_log.py tests/storage/test_tool_surface_codec.py -v`, then full `pytest`, Ruff check/format check, and Pyright.

Expected: PASS; compaction/reload/fork can alter provider context but cannot
create, erase, or source authority.

- [ ] **Step 5: Request whole-feature review**

Review persistence, scheduler, MCP, skills, resume, and compaction ownership
together.  Root resolves integration conflicts with accounting/reliability.

### Task 6: Documentation, migration, and prerelease preparation

**Files:**
- Modify: `docs/usage/agent.md`, `docs/usage/tools.md`, `docs/usage/skills.md`, `docs/architecture/tool-protocol.md`, `docs/architecture/compaction.md`, `docs/architecture/skills-subagents.md`, `docs/versioning.md`, `CHANGELOG.md`, `pyproject.toml`, `uv.lock`
- Create: `docs/migration-3.0.md`
- Modify/add examples and docs tests only where required to demonstrate the supported public API

**Interfaces:**
- Consumes: approved Tasks 1–5 behavior and their exact limits.
- Produces: user documentation for scope policy/search-only versus activation mode, schema timing, skill/MCP limits, durable-reader/store requirements, resume/fork/compaction guarantees, and explicit non-guarantees.
- Produces: root-selected next 3.0 prerelease metadata and a validation evidence package; no publishing capability.

- [ ] **Step 1: Document actual supported behavior and limits**

Show one search-only configuration and one activation-policy configuration.
State that target schemas appear next request, policy is host authority,
activation is not permission approval, MCP remains configured by the host, and
skills use the existing loader.  State strict reader/store behavior, scope
mismatch failure, full-history authority, and the narrow current-compaction
invariant.  Do not describe future artifact/token-compaction work as shipped.

- [ ] **Step 2: Prepare root-approved prerelease metadata**

Apply the version selected by root’s release ledger, update lockfile and
changelog consistently, and add migration notes.  Do not select a version by
guessing, publish, tag, push, or create a release.

- [ ] **Step 3: Run release validation**

Run: `pytest`

Run: `ruff check . && ruff format --check .`

Run: `pyright`

Run: `uv lock --check` and the repository’s isolated wheel/sdist build and
installed-wheel public-API smoke checks.  Record failures rather than waiving
them; no live provider calls.

- [ ] **Step 4: Hand off to root for final review/release**

Report docs/version evidence, exact checks, migration caveats, and the
whole-feature review result.  Root alone performs branch integration and any
publication.
