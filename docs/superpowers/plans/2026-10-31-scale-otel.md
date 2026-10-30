# Direct OpenTelemetry Implementation Plan

> Implement WP15 under the approved D11 contract, using TDD and separate logical commits.

**Goal:** Replace the observer layer with direct, isolated OTel instrumentation.
**Architecture:** One private telemetry module resolves API tracer/meter services through Agent Context. Source call sites own spans and store timers; async iterators restore caller context at every yield.
**Tech stack:** Python 3.11+, opentelemetry-api core; SDK for dev/tests only.
**Spec:** docs/superpowers/specs/2026-10-30-scale-ready-architecture.md (D11).

## Constraints

- Current branch/worktree only; no push, branch switch or edits to WP17-owned paths.
- Agent and package root edits limited to observability lines; no new public API.
- All gen_ai/linch attribute names are constants; content gated by the standard environment switch.
- SDK providers injected in tests, never set globally; caller context parents run spans.

## Review focus

- Async iterator close/cancellation must finish spans and restore the caller context.
- Retry/fallback/auxiliary provider deliveries each receive their own chat span.
- Failed tool results and raised errors both set error.type without leaking content.
- Resumption preserves run identity while using the current caller's trace context.
- Session writes and buffered/batched run-store calls are timed at their call sites.

## Tasks

- [x] Record no-SDK scale baseline, move API to core / SDK to dev, refresh lock, verify dependency metadata and commit.
- [x] Write failing private telemetry tests; implement Context resolution, constants, safe iterator/context helpers and metrics; verify and commit.
- [x] Write failing source span-tree, error, resume, nested-run, isolation and capture tests; instrument run/step/provider/tool/compaction/store sources; verify and commit.
- [x] Delete observer package, adapter, exports and obsolete tests/example; migrate lifecycle assertions to hooks/SDK; verify removals and commit.
- [x] Update OTel usage/example, migration and changelog; remeasure no-SDK scale benchmark; verify and commit.
- [x] Review complete diff; resolve material findings with regression tests; run pytest, ruff and pyright, report counts and LOC/public/API/benchmark deltas; leave clean.

Pre-flight: telemetry helpers precede source calls; source calls precede observer deletion. Documentation uses the same Context keys as tests. WP17 stores remain untouched.

Verification: full Postgres suite 2165 passed / 18 skipped; ruff lint/format passed; pyright 0 errors (3 pre-existing environment warnings). Six scale cases correct. Fresh review findings fixed with RED→GREEN coverage; no decisions departed from D11. Report: `docs/architecture/otel-noop-benchmark.md`.
