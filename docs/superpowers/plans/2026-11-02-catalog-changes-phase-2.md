# Catalog Changes Phase 2 Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development to implement the independent tasks below. The user has authorized implementation and commits in the existing worktree.

**Goal:** Preserve a session's provider prefix while reporting live catalog changes durably at the conversation tail.

**Architecture:** Store the frozen prefix and separate `base`/`notified` catalog digests in session metadata. One catalog synchronization step prepares each ordinary request, with adapters rendering durable catalog blocks as text. Reload APIs update live registries; compaction resets `notified` to `base` and preserves system/tool bytes, then synchronization consolidates changes at the conversation tail.

**Tech Stack:** Existing async Python SDK, pytest, Ruff, Pyright; no dependencies.

**Spec:** `docs/superpowers/specs/2026-11-02-catalog-changes.md`, sections 4 and 5 only.

## Global Constraints

- About 500 lines in `src/`; no new dependency.
- Only new public name: `Agent.reload_skills`.
- Python 3.11+ per the SDK runtime contract.
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

- [x] Cover every section 5 case and observe review regressions failing before their fixes: stable/restarted bytes, listing once, skill invocation after reload, tool addition/removal/description/schema, persistence, compaction, append/write crash, durable resume, bypasses, and no-diff fast path.
- [x] Implement snapshot writes, tail notifications, request consumption, frozen contract/compaction listing, and notification reset after compaction.
- [x] Keep exact pending-request replay semantics and context selection intact.
- [x] Run focused tests, update tests encoding deliberately changed listing behavior or private helper signatures, and commit the core change (`5c57e04`).

### Task 4: Documentation, review, and gates

**Files:** Relevant architecture pages and `CHANGELOG.md`.

- [x] Document snapshot lifetime, bypasses, reload APIs, durable notifications, and compaction.
- [x] Independently review the complete diff against sections 4 and 5; resolve supported-scope findings.
- [x] Run full pytest, Ruff lint/format, and Pyright using the requested venv and `PYTHONPATH=src`.
- [x] Report commits, check counts, changed existing tests, conflicts, and unfinished work.

## Review corrections under the updated spec

The updated sections 4.1, 4.4–4.6, and 5 resolve the former metadata and compaction
gaps. `base` is immutable; `notified` contains tool, skill, and subagent digests.
The live catalog is compared directly with `notified`. Compaction and its crash
recovery reset notifications without rebuilding the prefix. No spec gap remains
from the earlier implementation notes.

The review also requires provider-view-only skill reinjection, snapshot bypass
awareness during reinjection and compaction, description-bearing notifications
with name-only MCP handling, exact environment-block placement, and durable
resume coverage spanning skill reload, compaction, and a crash.

Tool preview cannot be dropped: a durable run started after an addition or schema
edit must fingerprint the tool rows its first catalog sync will send. The former
`loop/contracts.py::_preview_snapshot_tools` is removed; both synchronization and
contract construction share `catalog.snapshot_tools(snapshot, live_schemas)`.
It ignores `notified` resets and description-only edits. Regression tests
demonstrated that dropping preview breaks both addition and schema-edit contract
identity.

Skill-body changes remain detectable through their digest. The spec's restriction
to skill names and descriptions applies to notification text; bodies stay private.
Paid-compaction recovery also receives the effective run config, so a bypassed
run retains its live listing reminder instead of treating it as frozen system
context.

## Review regression evidence

- The first catalog regression run had **4 failures**: missing `base` and
  `notified.skills`, absent description text/MCP handling, environment placement,
  and missing crash recovery.
- The first two reinjection regressions failed before restoring synchronous,
  provider-view-only reminders. Bypass listing and contract fingerprint tests
  were then added around the same corrected behavior.
- A disposable archive of the original `HEAD` reproduced the strengthened
  in-turn reload → compaction → crash regression: it failed with **0 catalog
  reminders instead of 1**. The corrected runtime passes the test.

## Section 5 coverage

Paths below are relative to `tests/`; focused regression files supplement the
original phase-2 cases rather than replace their assertions.

| Required behavior | Coverage |
|---|---|
| One skill listing across two runs | `loop/test_catalog_requests.py` |
| Stable system/tool bytes | `loop/test_catalog_requests.py` |
| Restart with changed host platform | `loop/test_catalog_requests.py`, `loop/test_catalog_resume.py` |
| Reload skills, notify once, invoke new skill | `loop/test_catalog_changes.py` |
| Append tools in order with one advisory | `loop/test_catalog_requests.py` |
| Retain removed schema and return unknown-tool error | `loop/test_catalog_requests.py` |
| Description edits leave wire schemas unchanged | `loop/test_catalog_requests.py` |
| Reload subagents with an MCP tool registered afterward | `test_catalog_reload.py` |
| Persist notification through store reload | `loop/test_catalog_requests.py` |
| Compaction consolidation and no-change/no-meta behavior | `loop/test_catalog_changes.py`, `loop/test_catalog_phase2_regressions.py` |
| Resume after reload, compaction, and crash | `loop/test_catalog_resume.py` |
| No repeated skill notice after restart or unrelated tool edit | `loop/test_catalog_phase2_regressions.py` |
| New descriptions, including Subagent, with MCP name-only text | `loop/test_catalog_phase2_regressions.py` |
| Append-before-meta crash repeats at most once | `loop/test_catalog_changes.py` |
| Durable resume after skill reload | `loop/test_catalog_resume.py` |
| Every bypass preserves live bytes and exact dedupe | `loop/test_catalog_bypasses.py`, `loop/test_catalog_skill_context.py` |
| No snapshot diff on unchanged fast path | `loop/test_catalog_bypasses.py` |

## Process limitation

The initial core draft implemented more behavior than its first four failing
tests covered. Remaining cases were added and exercised afterward, so the strict
per-test RED-before-implementation sequence was not fully maintained. No existing
assertion was weakened to accommodate that draft.

## Final verification after review corrections

All gates used `/home/duy/Downloads/duy_dev/agent_kit/.venv/bin` first on PATH
and `PYTHONPATH=src`.

- Full pytest: **3143 passed, 408 skipped**, in 158.95 seconds; no failures.
- Ruff lint: passed. Ruff format check: **498 files** already formatted.
- Pyright: **0 errors, 5 warnings**, the existing missing-source warnings for
  `jsonschema` and `croniter`.
- Documentation-reference checks after the plan edits: **2 passed**.
- `git diff --check`: clean.
- Phase-2 source growth relative to `b9a283f`: **622 added, 122 removed,
  500 net lines**. No dependency or top-level export was added.

The review adds **21 test cases** in total. New files
`tests/loop/test_catalog_phase2_regressions.py` and
`tests/loop/test_catalog_skill_context.py` cover the corrected state transitions,
MCP descriptions, reinjection, and paid replay. Five new resume cases cover
pre-sync tool changes, overlay fingerprints, and both persisted-view and in-turn
reload/compaction/crash recovery.

Existing tests changed in this review:

- `tests/loop/test_catalog_changes.py` uses the exact snapshot shape, asserts
  retained prefixes and consolidated reminders, and checks real compaction with
  no catalog changes performs no metadata write.
- `tests/loop/test_catalog_resume.py` adds the five contract/recovery cases above.
- `tests/loop/test_provider_turn.py`,
  `tests/model_calls/test_model_call_journal.py`, and
  `tests/tools/test_search.py` restore synchronous helper calls/mocks.
- `tests/test_catalog_reload.py` registers an actual MCP wrapper after `Subagent`
  for the ordering regression.

Every section 5 case is covered; no review item remains unfinished. The earlier
verification and process limitation below describe the original implementation,
not these review corrections.

## Verification record before review corrections

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

## Existing tests updated before review corrections

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
