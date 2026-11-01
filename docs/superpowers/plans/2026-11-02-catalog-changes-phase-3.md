# Catalog Changes Phase 3 Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development. The user has authorized implementation and local commits in the existing worktree.

**Status:** Implementation and documentation complete. Independent final whole-phase review is pending. Full-suite gate has two execution timeout failures; focused regressions, Ruff, and Pyright pass.

**Goal:** Add opt-in native catalog changes while preserving phase-2 payload bytes when disabled.

**Architecture:** Keep prefix snapshots frozen for positional providers and put new definitions in durable catalog blocks. Render those blocks through the existing Anthropic and Responses adapters, using only section 6's wire shapes. Keep append-only fallbacks and narrowing on the phase-2 path.

**Tech Stack:** Existing async Python SDK, pytest with fake transports, Ruff, Pyright; no dependency additions.

**Spec:** `docs/superpowers/specs/2026-11-02-catalog-changes.md`, sections 2, 4.5, 4.6, and 6. Section 6 is binding.

## Global Constraints

- Target release 3.0.1; retain branch `feature/3.1-catalog-changes`.
- About 200 net lines of source growth; no new `linch.__all__` name or dependency.
- Write each section 6.5 test first and observe its failure before implementation.
- Option off means byte-identical provider payloads to phase 2.
- Never call a live provider; fake transports capture actual adapter payloads.
- Do not start phase 4 or change `ToolSearchPlugin`.
- If a contract item cannot hold as written, stop that item and report it; invent no wire shape.
- Serialize mutations and implementation workers. Read-only audits may overlap.
- All test/check commands use `/home/duy/Downloads/duy_dev/agent_kit/.venv/bin` first on PATH and `PYTHONPATH=src`.
- Do not push. Every commit includes the supplied Co-Authored-By and Claude-Session trailers.

## Review Focus

- A model fallback must include definitions previously sent only in the log.
- Context narrowing is known after context construction and must use phase 2.
- Retained compaction blocks must render deterministically without duplicate additions.
- Schema edits must never invent an existing-name redefinition wire format.
- Resume contracts and exact pending-request replay must retain their existing invariants.

### Task 1: Section 6.5 payload regressions

**Files:** New focused files under `tests/providers/` and `tests/loop/`; reuse existing test support.
**Interface:** Fake SDK transports capture actual `messages.create` / `responses.create` kwargs. Real session/catalog/request code drives lifecycle tests.

- [x] Write tests for option-off byte identity with and without definitions; exact model/endpoint gating; addition placement and unchanged prefix; parallel results; abort/new prompt; prefix-tool restore/removal; duplicate addition after compaction; schema edit; fallback; run and context narrowing; 4 MiB bound; every-request beta; pause-turn termination; Responses round-trip/previous-response position and allowed/forced choices; durable resume; MCP name-only notification text.
- [x] Assert literal wire shapes and stable order; compare phase-2 payload bytes against hand-derived fixtures or frozen baseline captures, never the same builder against itself.
- [x] Run every new test before any implementation. Record per-test failures and explain any existing-behavior test that is already green.
- [x] Use only the binding section 6 wire shapes.
- [x] Commit test coverage with the RED evidence in this plan.

#### Task 1 RED evidence (updated section 6.3 contract)

- Added **75 cases** in `tests/providers/test_native_catalog_wire.py` (57) and
  `tests/loop/test_native_catalog_changes.py` (18). Fake SDK transports capture
  actual adapter kwargs; lifecycle cases run real sessions, synchronization,
  request assembly, tool execution, compaction, fallback, and durable resume.
- Ran every case before any source implementation with
  `PATH=/home/duy/Downloads/duy_dev/agent_kit/.venv/bin:$PATH PYTHONPATH=src pytest -q tests/providers/test_native_catalog_wire.py tests/loop/test_native_catalog_changes.py --tb=short --junitxml=/tmp/catalog-phase3-red.xml`.
  Final result: **72 failed, 3 passed in 1.33s**, exit 1. All failures are expected
  missing phase-3 constructor fields/options or the default capability field.
  No tests were skipped, and no source files were edited.
- The three already-green cases independently pin frozen phase-2 Anthropic and
  Responses payload bytes and existing `pause_turn` termination. They protect
  existing behavior; they are not presented as proof of new native behavior.
- Temporary `/tmp` runtime fixture probes ignored only absent constructor kwargs
  to exercise existing transport/lifecycle setup before handoff. They validated
  all four option-off fixture variants and caught a copied-registry fixture
  issue, which was corrected. No probe or shim is part of the tracked suite.
- Focused `ruff check` and `ruff format --check` pass. The full gates belong to
  Tasks 2/3; this deliberately red test commit does not claim a passing suite.
- By-value removal/restoration follows section 6.3. A tool restored with a
  different schema (including after compaction drops its original notice) uses
  the phase-2 prefix path.
- Full per-case RED/green reasons, coverage mapping, fixture-probe evidence,
  commands, and concerns: `.superpowers/sdd/2026-11-02-catalog-changes-phase-3/task-1-report.md`.

### Task 2: Core and native adapters

**Files:** `src/linch/types.py`, `catalog.py`, `providers/base.py`, `providers/anthropic.py`, `providers/openai_responses.py`, `openai_responses.py`, and necessary private request/resume/fallback seams in `loop/`.
**Interface:** Capability `catalog_changes` defaults to `append_only`; provider option `native_catalog_changes=False`; catalog block `tool_definitions=()` round-trips. No unlisted provider wire fields.

- [x] Implement only behaviors with observed RED evidence from Task 1.
- [x] Preserve phase-2 sync/request behavior when disabled; positional requests use frozen tools, append-only fallback uses `snapshot_tools(snapshot, live_schemas)`.
- [x] Keep schema edits, empty prefixes, run/context narrowing, and bypasses on the phase-2 path.
- [x] Render exact section 6.3 and 6.4 wire shapes; keep text user-role; preserve active-name order, beta merging, and definition limits.
- [x] Run focused tests, full pytest, lint/format, and Pyright; review and commit.

#### Task 2 implementation evidence

- Implemented the corrected section 6 contract with explicit durable request
  eligibility, native-only context-aware catalog timing, effective per-turn model
  gating, safe same-call fallback, and full-history schema/contract comparisons.
- The original 75 RED-first cases now pass with additional UTF-8 byte overflow,
  case-insensitive beta merge, environment endpoint gating, model-overlay sync,
  and exact crash/reload/compaction regressions. The durable fixture now explicitly
  enables exact model-input replay on both agents.
- Focused final regression command: **159 passed, 3 skipped**. Source review and
  scoped model-overlay/contract re-review: **PASS**, original Important issue
  addressed; no outstanding Important/Critical findings.
- Source growth: **290 net lines**. This exceeds the about-200 estimate because
  durable eligibility/codec, deferred context selection, frozen same-call fallback,
  and model-overlay/history/compaction resume integration are necessary for the
  stated contract. No new dependency, public export, or phase-4 work.
- Native existing-snapshot context hooks run before the new notice is appended;
  option-off timing stays unchanged. Anthropic can relocate an unfinished trailing
  native tool message after abort/new prompt, as mandated by buffering; completed
  historical messages keep their placement. By-value removal remains text-only
  while historical by-value provenance is present, including after promotion.
- Final `pytest -q`: **2 failed, 3228 passed, 408 skipped** in 159.25s. The two
  remaining failures are unchanged execution timeouts in
  `tests/execution/test_execution_backend.py::test_bash_tool_backend_selection[False]`
  and `tests/tools/test_execution_backend.py::test_bash_tool_negative_timeout_runs_command`.
  No phase-3 failure remains. Ruff check/format pass (**500 files**); Pyright:
  **0 errors, 5 existing warnings**. Details and earlier RED evidence are in
  `.superpowers/sdd/2026-11-02-catalog-changes-phase-3/task-2-report.md`.

### Task 3: Documentation, final review, and gates

**Files:** `docs/usage/providers.md`, relevant `docs/architecture/` pages, `CHANGELOG.md`, this plan.
**Interface:** Describe both provider options as off by default and unverified against live endpoints; accurately report gate results and remaining review.

- [x] Document capability gating, prefix lifetime, positional rendering, narrowing/fallback, and limitations.
- [x] Add an Unreleased changelog entry for target 3.0.1.
- [ ] Independently review the complete change against section 6 and address actionable findings with RED-first regressions.
- [x] Run `pytest -q`, `ruff check . && ruff format --check .`, and `pyright`; record counts and failures plainly.
- [x] Report commits, gates, changed tests, source growth, and unfinished items. Do not push.

## Final implementation evidence

- Tests-first commit: `17b5c01`; native implementation commit: `51f6937`.
  Documentation commit is recorded in the task-3 report after commit creation.
- The two new native test files contain **87 cases** (75 initial cases plus
  RED-first safety regressions). Tests changed:
  `tests/providers/test_native_catalog_wire.py`,
  `tests/loop/test_native_catalog_changes.py`, and
  `tests/storage/test_model_input_snapshots.py` (two internal golden hashes).
- Source growth is **290 net lines**, versus the about-200 estimate, for necessary
  durability/codec, context narrowing, safe fallback, effective-model admission,
  and historical provenance integration. No new dependency or public export.
- Usage and architecture docs cover off-by-default options, exact gating,
  prefix lifetime, native shapes and user-role text authority, by-value removal,
  restoration/deduplication, phase-2 boundaries, fallback, context timing,
  Responses suffix/active-name semantics, and Anthropic beta/size checks.
- Independent source review and scoped re-review passed after RED-first fixes.
  The final independent whole-phase review remains pending; its checklist above
  is deliberately unchecked.

### Gates on the implemented source and test tree

All gates used `/home/duy/Downloads/duy_dev/agent_kit/.venv/bin` first on PATH and
`PYTHONPATH=src`. Source was unchanged for the documentation task.

- Final recorded `pytest -q`: **2 failed, 3228 passed, 408 skipped in 159.25s**,
  exit 1. Failures:
  `tests/execution/test_execution_backend.py::test_bash_tool_backend_selection[False]`
  and `tests/tools/test_execution_backend.py::test_bash_tool_negative_timeout_runs_command`.
  Both report command timeouts in unchanged local execution code. Similar timeout
  failures were observed before source implementation. A parent-run targeted
  rerun of these two cases passed: **2 passed in 0.14s**. This does not replace
  the failed full-suite result.
- Focused final native/catalog/fallback/history/snapshot regression set:
  **159 passed, 3 skipped in 4.75s**, exit 0.
- `ruff check .`: passed; `ruff format --check .`: **500 files already formatted**.
- `pyright`: **0 errors, 5 existing warnings**, exit 0; missing-source imports
  for `jsonschema` and `croniter`.

Full implementation evidence and RED/GREEN details are recorded in
`.superpowers/sdd/2026-11-02-catalog-changes-phase-3/task-2-report.md`.
Documentation verification is recorded in the adjacent `task-3-report.md`.

### Remaining items

- Final independent whole-phase review and any resulting gate refresh are pending.
- Native Anthropic and OpenAI wire behavior is **UNVERIFIED against live endpoints**;
  no live provider was called, as required.
- Phase 4 was not started. Release target remains 3.0.1, branch remains
  `feature/3.1-catalog-changes`, and nothing was pushed.
