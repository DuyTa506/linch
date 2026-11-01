# Catalog Changes Phase 3 Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development. The user has authorized implementation and local commits in the existing worktree.

**Status:** Stopped before source implementation: section 6.3 cannot encode removal of a natively added tool while satisfying its reference restriction. The tasks below remain unimplemented pending a contract correction.

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

- [ ] Write tests for option-off byte identity with and without definitions; exact model/endpoint gating; addition placement and unchanged prefix; parallel results; abort/new prompt; prefix-tool restore/removal; duplicate addition after compaction; schema edit; fallback; run and context narrowing; 4 MiB bound; every-request beta; pause-turn termination; Responses round-trip/previous-response position and allowed/forced choices; durable resume; MCP name-only notification text.
- [ ] Assert literal wire shapes and stable order; compare phase-2 payload bytes against hand-derived fixtures or frozen baseline captures, never the same builder against itself.
- [ ] Run every new test before any implementation. Record per-test failures and explain any existing-behavior test that is already green.
- [ ] Stop and report any test requiring a contradictory or unspecified wire shape.
- [ ] Commit test coverage with the RED evidence in this plan.

### Task 2: Core and native adapters

**Files:** `src/linch/types.py`, `catalog.py`, `providers/base.py`, `providers/anthropic.py`, `providers/openai_responses.py`, `openai_responses.py`, and necessary private request/resume/fallback seams in `loop/`.
**Interface:** Capability `catalog_changes` defaults to `append_only`; provider option `native_catalog_changes=False`; catalog block `tool_definitions=()` round-trips. No unlisted provider wire fields.

- [ ] Implement only behaviors with observed RED evidence from Task 1; resolve contract blockers before dependent implementation.
- [ ] Preserve phase-2 sync/request behavior when disabled; positional requests use frozen tools, append-only fallback uses `snapshot_tools(snapshot, live_schemas)`.
- [ ] Keep schema edits, empty prefixes, run/context narrowing, and bypasses on the phase-2 path.
- [ ] Render exact section 6.3 and 6.4 wire shapes; keep text user-role; preserve active-name order, beta merging, and definition limits.
- [ ] Run focused tests, full pytest, lint/format, and Pyright; review and commit.

### Task 3: Documentation, final review, and gates

**Files:** `docs/usage/providers.md`, relevant `docs/architecture/` pages, `CHANGELOG.md`, this plan.
**Interface:** Describe both provider options as off by default and unverified against live endpoints; accurately report blocked work.

- [ ] Document capability gating, prefix lifetime, positional rendering, narrowing/fallback, and limitations.
- [ ] Add an Unreleased changelog entry for target 3.0.1.
- [ ] Independently review the complete change against section 6 and address actionable findings with RED-first regressions.
- [ ] Run `pytest -q`, `ruff check . && ruff format --check .`, and `pyright`; record counts and failures plainly.
- [ ] Report commits, gates, changed tests, source growth, and unfinished items. Do not push.

## Preflight and contract blockers

- Clean baseline at `c407f7220b5d453e994828cb5a120b797516fdfd`: **3143 passed, 408 skipped** in 33.43 seconds.
- This checkout lacks `.claude/rules/coding_style.md`; read the main-checkout copy at `/home/duy/Downloads/duy_dev/agent_kit/.claude/rules/coding_style.md` and its development-principles reference.
- Two independent read-only audits confirmed a section 6.3 contradiction: a tool added natively is absent from `req.tools`, but its removal requires `tool_reference`, which is restricted to names in `req.tools`. No by-value removal shape is listed. **That item is stopped pending a contract correction.** No wire workaround is authorized.
- User clarification was requested while independent work continues.

## Execution evidence

### Blocking sequence

1. Start with a frozen prefix containing tool `base`.
2. Add tool `x`. Section 6.2 requires its definition in a catalog block and requires `snapshot["tools"]` to remain unchanged.
3. Remove `x`. Section 6.2 requires a removal notification, still without changing the snapshot.
4. The positional request therefore has no `x` in `req.tools` (section 6.2).
5. Section 6.3's listed removal shape requires `tool_reference` for `x`, but section 6.3 permits references only to names declared in `req.tools`.

Sending the listed removal violates the reference restriction. Adding `x` to the prefix violates the frozen-tools requirement. A by-value removal shape is not listed. The sentence about re-sending a by-value definition does not specify a by-value removal. No workaround was implemented and no spec text was changed.

The contract needs an explicit correction for this case before complete Anthropic support can be implemented. In particular, whether removal references may identify names defined earlier by value must be decided by the contract, not inferred by the adapter. No live provider was called to resolve the ambiguity.

### Work preserved and unfinished

- No source changes, public exports, provider options, or tracked tests were added.
- A draft with 25 collected provider cases is preserved in ignored task scratch space. It has not been run for RED evidence, its fixtures still need correction, and it is not part of the test suite. No section 6.5 test is claimed complete.
- Phase 3 implementation, provider-option usage/architecture documentation, and the Unreleased changelog entry remain unfinished. Documenting unimplemented options as available would be misleading.
- Phase 4 was not started. The release target and branch name are unchanged. Nothing was pushed.

### Gates on the unchanged source and test tree

All gates used `/home/duy/Downloads/duy_dev/agent_kit/.venv/bin` first on PATH and `PYTHONPATH=src`.

- `pytest -q`: **3143 passed, 408 skipped**, 33.43 seconds, exit 0.
- `ruff check .`: passed, exit 0.
- `ruff format --check .`: **498 files already formatted**, exit 0.
- `pyright`: **0 errors, 5 warnings**, exit 0. Warnings are the existing missing-source imports for `jsonschema` and `croniter`.

These gates validate the existing phase-2 tree; they do not validate phase 3.
