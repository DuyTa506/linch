# Postgres-only storage implementation plan

> **For agentic workers:** Use superpowers:subagent-driven-development to execute serially.

**Goal:** Implement WP17 / D13 by deleting SQLite and keeping Postgres durability and InMemory development stores.

**Architecture:** Remove SQLite implementations and exports without shims. Preserve shared codecs and the blocking helper used by non-database callers. Hosts explicitly supply durable deep-agent stores and filesystem.

**Tech Stack:** Python 3.11, pytest, asyncpg, Ruff, Pyright.

**Spec:** `docs/superpowers/specs/2026-10-30-scale-ready-architecture.md` (D13).

## Global Constraints

- Do not switch branches, push, or edit another worktree.
- Never edit WP15-owned observability, hooks, loop, events, or observability examples.
- Edit only SQLite lines in shared `agent.py` and `src/linch/__init__.py`.
- Durable state is Postgres; InMemory is for tests and dev.
- Execute gates using `/home/duy/Downloads/duy_dev/agent_kit/.venv/bin/`.
- Set `AGENT_KIT_TEST_PG_DSN=postgresql://postgres:@/postgres?host=/run/user/1000/python_PostgresServer/28a76638b8` for tests; run Postgres coverage rather than skipping it.
- Every commit ends with `Co-Authored-By: Codex <noreply@openai.com>`.
- Only read work runs in parallel; implementation workers run serially.

## Review Focus

- Restart tests must use independent Postgres holders and unique IDs or schemas.
- Generic runtime, scheduling, and mailbox regressions survive removal of their convenient SQLite fixtures.
- Deep-agent durability cannot silently fall back to ephemeral state.
- Shared run-store codecs remain usable by Postgres; non-SQLite blocking callers remain intact.
- Scripts and examples document actual durability and close all resources they create.

### Task 1: Delete runtime backends

**Files:** SQLite modules in sessions, filesystem, mailbox, scheduling; storage executor; `run_store.py`; backend package exports; `deep_agent/factory.py`; SQLite lines in `agent.py` and top-level exports; focused deep-agent validation tests.

**Interfaces:** Preserve `RunStore`, InMemory implementations, Postgres imports/codecs. `create_deep_agent(durable=True)` requires explicit session/run stores and an explicit filesystem when that feature is enabled, accepting duck-typed host adapters.

- [x] Write and run failing tests for missing durable configuration, explicit adapter preservation, and disabled filesystem behavior.
- [x] Delete implementations, SQLite-only helpers, exports and runtime references. Keep `_blocking.py` and remove stale database-specific wording there.
- [x] Verify focused tests, source lint and Pyright; commit runtime deletion.

### Task 2: Migrate tests

**Files:** Affected tests under `tests/`, including storage conformance/schema suites, durable resume, deep-agent restart, report/benchmark tests.

**Interfaces:** Conformance runs memory/Postgres; schema tests retain Postgres checks because memory has no schema. Reopen durability uses fresh Postgres instances; ordinary semantics use InMemory.

- [x] Remove backend-specific SQLite tests and migrate generic tests without weakening assertions.
- [x] Exercise Postgres reopen, concurrent holders, model input corruption and durable inbox behavior.
- [x] Run full pytest with supplied DSN, lint and format on tests; commit test migration.

### Task 3: Document coordination scope

**Files:** `docs/usage/coordination.md`, `docs/migration-3.0.md`.

- [x] Document in-memory mailbox/scheduling only; durable scheduling comes in WP6/PgQueuer and mailbox moves to `linch.ext` in WP10.
- [x] Commit coordination documentation.

### Task 4: Migrate examples, scripts and current guides

**Files:** SQLite callers in `examples/`, `scripts/benchmark_runtime.py`, `scripts/run_report.py`, related tests; current architecture/usage/docs/README references.

- [x] Use InMemory for transient demonstrations and environment Postgres DSNs for restart/report durability.
- [x] Document local Postgres via Docker or pgserver, keep historical spec/measurement records unchanged.
- [x] Run script tests, compile examples, verify current references and commit.

### Task 5: Migration record and final verification

**Files:** `docs/migration-3.0.md`, `CHANGELOG.md`, this plan.

- [x] Add one migration line for each of the five removed public names and the deep-agent durable configuration change.
- [x] Add Unreleased changelog entry.
- [x] Run all four gates, review branch changes, report remaining historical mentions and LOC deltas; commit and leave worktree clean.
