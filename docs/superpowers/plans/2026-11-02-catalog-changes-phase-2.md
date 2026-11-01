# Catalog Changes Phase 2 Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development to implement the independent tasks below. The user has authorized implementation and commits in the existing worktree.

**Goal:** Preserve a session's provider prefix while reporting live catalog changes durably at the conversation tail.

**Architecture:** Store the frozen prefix in session metadata. One catalog synchronization step prepares each ordinary request, with adapters rendering durable catalog blocks as text. Reload APIs update live registries; compaction promotes accumulated changes into the prefix.

**Tech Stack:** Existing async Python SDK, pytest, Ruff, Pyright; no dependencies.

**Spec:** `docs/superpowers/specs/2026-11-02-catalog-changes.md`, sections 4 and 5 only.

## Global Constraints

- About 500 lines in `src/`; no new dependency.
- Only new public name: `Agent.reload_skills`.
- New code uses Python 3.10-compatible constructs; existing package floor remains 3.11+.
- Durable writes use `await session.append` and `session.update_meta`.
- No in-place mutation of session projections or new provider capabilities/events.
- Each new behavior gets a failing test before implementation.
- No push; logical commits include the requested attribution and session trailers.

## Review Focus

- Reloaded workers must not derive the prefix from the new host environment.
- A failed metadata write after a notification must not lose that notification.
- Removal must retain a wire schema while execution reports an unknown tool.
- Bypasses must preserve existing selection, overlays, and capability handling.
- Compaction and durable contracts must use the same frozen skill context.

### Task 1: Catalog block wire representation

**Files:** `types.py`, serialization, provider adapters, token estimators; focused tests.
**Interface:** Internal `CatalogChangeBlock(text, added_tools=(), removed_tools=())`, included in `ContentBlock` and message codecs, rendered as text by every adapter.

- [x] Write and run failing round-trip, adapter, and estimator tests.
- [x] Implement block handling and audit every relevant `TextBlock` check.
- [x] Run focused tests and commit the coherent wire change (`443ce3e`).

### Task 2: Reload APIs

**Files:** `agent.py`, skill/subagent wiring, reload tests, `tests/package/test_public_api.py`.
**Interface:** Async `Agent.reload_skills()`, per-agent `_catalog_revision`, existing `reload_subagents()` retaining registry slots.

- [x] Write and run failing reload, permission, tool identity, order, and public-surface tests.
- [x] Implement off-loop scan and synchronous installation; preserve feature-off behavior.
- [x] Run focused tests and commit the reload change (`d517904`).

### Task 3: Frozen prefix, synchronization, compaction, resume

**Files:** Private catalog module, `session.py`, `loop/request.py`, `loop/provider_turn.py`, `loop/streaming.py`, `loop/contracts.py`, `compaction.py`, catalog tests.
**Interface:** `sync_catalog(session, config, lease)` and internal helpers using `session.meta["linch.prefix"]`.

- [ ] Write and observe failures for all applicable section 5 cases: stable/restarted bytes, listing once, skill invocation after reload, tool addition/removal/description/schema, persistence, compaction, append/write crash, durable resume, bypasses, and no-diff fast path.
- [ ] Implement snapshot writes, tail notifications, request consumption, frozen contract/compaction listing, and promotion after compaction.
- [x] Keep exact pending-request replay semantics and context selection intact.
- [x] Run focused tests, update tests encoding deliberately changed listing behavior or private helper signatures, and commit the core change (`5c57e04`).

### Task 4: Documentation, review, and gates

**Files:** Relevant architecture pages and `CHANGELOG.md`.

- [x] Document snapshot lifetime, bypasses, reload APIs, durable notifications, and compaction.
- [x] Independently review the complete diff against sections 4 and 5; resolve supported-scope findings.
- [x] Run full pytest, Ruff lint/format, and Pyright using the requested venv and `PYTHONPATH=src`.
- [ ] Report commits, check counts, changed existing tests, conflicts, and unfinished work.

## Spec items awaiting clarification

- The exact prefix shape at spec lines 62–66 contains frozen skill digests but no
  last-notified skill digests. Sections 4.4 and 4.6 need both across restarts. The
  implementation keeps the frozen base; durable skill-notification deduplication
  awaits authorization to extend the private metadata.
- Spec lines 122–123 update tool schemas in the prefix, while lines 156–163 later
  require the original base to recognize pending changes after compaction. The
  original digests are not in the specified shape. Promotion after additions or
  schema edits awaits clarification; description edits and removals retain their
  base and can be promoted.
- The task requests Python 3.10 compatibility, but `pyproject.toml:10` and CLAUDE.md
  require Python 3.11+. New constructs remain 3.10-compatible; downgrading the
  existing SDK runtime is outside this work package.

## Process limitation

The initial core draft implemented more behavior than its first four failing
tests covered. Remaining cases were added and exercised afterward, so the strict
per-test RED-before-implementation sequence was not fully maintained. No existing
assertion was weakened to accommodate that draft.

## Verification record

All checks used `/home/duy/Downloads/duy_dev/agent_kit/.venv/bin` first on PATH and
`PYTHONPATH=src`.

- Full pytest outside the process sandbox: **3122 passed, 408 skipped**.
- The preceding sandboxed full run: **1 failed, 3121 passed, 408 skipped**.
  `tests/execution/test_execution_backend.py::test_local_fs_and_shell_share_workspace`
  intermittently returned empty shell output. The execution suite passed alone
  (**30 passed**); no execution source or assertion was changed.
- Ruff lint and format check: passed; **496 files** already formatted.
- Pyright: **0 errors, 5 warnings**, for missing `jsonschema` and `croniter` sources.
- Source growth: **690 added, 120 removed, 570 net lines**, above the approximate
  500-line target. No dependency was added.

## Existing tests updated

- `tests/tools/test_search.py::test_skill_threshold_and_body_loading[50]` now
  expects the skill listing in the system prefix instead of the first user prompt.
- `tests/model_calls/test_model_call_journal.py::test_changed_skill_listing_contract_blocks_resume`
  became `test_changed_skill_listing_contract_uses_persisted_snapshot`; a live
  listing edit must no longer reject durable resume with a frozen prefix.
- `test_repeated_paid_projection_preserves_later_skill_reminder_order` in the
  model-call journal tests and `test_compaction_reinjects_only_deferred_skill_names`
  in the search tests now await the private reinjection helper.
- `tests/loop/test_provider_turn.py::test_force_final_context_recovery_keeps_tool_choice_disabled`
  uses an async reinjection mock; all behavior assertions remain intact.
- The documentation-reference guard recognizes the exact metadata-key literal
  `["linch.prefix"]` while still rejecting an undeclared API reference.
- `tests/package/test_public_api.py` adds the async reload method/private catalog
  block guard; top-level exports are unchanged.
