# WP7 scale reliability implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Port default response/tool reliability onto the 3.0 kernel within +700 net source lines.

**Architecture:** One run-local typed, serial turn-stopping dispatcher and disposable listeners. Reliability owns only a correction counter and the referenced successful-tool names; existing append, accounting, checkpoint/event batching and replay proofs own durability.

**Tech Stack:** Python 3.11+, existing kernel effects, pytest/asyncio, InMemory/Postgres stores.

**Spec:** `docs/superpowers/specs/2026-10-30-scale-ready-architecture.md` D4/WP7 and the user's final WP7 requirements.

## Global Constraints

- Stay on `scale/reliability`; base `8024c425ed10aa75d16474666a7ff249a2114683`; never push or edit another worktree.
- Source budget <= +700 net lines; new tests aim <1,200 lines.
- Durable before side effects; session-owned model-visible state is logged through `session.append`.
- Run-local listeners must not register unfiltered on the shared agent Context bus.
- No participants, AppendPlans, response middleware, pending-action journal, text rescue, legacy readers, or new dependencies.
- Only four public additions: ToolPrerequisite, CompletionGate, ReliabilityPolicy, ReliabilityEvent.
- Main checkout venv only. Every commit ends with the required Codex coauthor trailer.

## Review Focus

- Crash after feedback durability but before checkpoint: dedup the same turn's structurally equal tail and count once.
- Terminal-tool denial: paired ordinary error results before any ACK/finalizer, including replay.
- Tool evidence: after final normalized hooks; background ACK, denial, interruption and errors excluded.
- Invalid native response: one receipt/usage settlement, zero sibling tools, no raw rejected arguments.
- Default clean path: no namespace/events/feedback/provider calls; per-run state cannot leak across agents or sessions.

### Task 1: Typed seam and reliability behavior

**Files:** `src/linch/reliability.py`, `src/linch/loop/turn_stopping.py`, `src/linch/loop/reliability.py`; minimal integration in Agent, events/codec/exports, contracts/runtime, provider_turn/resume/finalize, scheduler/ToolPipeline; focused `tests/test_reliability.py` and `tests/loop/test_reliability*.py`.

**Interfaces:** Frozen/slotted API exactly as specified. `TurnStopping` candidates carry reason, turn, assembly/final-tool information; ordered registration returns Disposable; outcomes accept/continue(messages)/fail(error). Reliability is a listener and pre-execute effect. `linch.reliability` contains exactly v/corrections/succeeded when nonempty.

- [x] Write and run failing policy/default/None, validation, codec and contract tests.
- [x] Implement minimal public types, validation and run-local state/contract binding; run targeted tests.
- [x] Write and run failing seam route/equivalence and response correction tests, including malformed sibling batch, empty/envelope, unaffected prose/refusal/truncation.
- [x] Implement seam and bounded correction listener, reuse existing structural replay-tail proof; run targeted tests.
- [x] Write and run failing prerequisite/snapshot/final-hook success/background/completion/force-final/isolation tests.
- [x] Implement pre-permission pre-execute and normalized foreground-result observation with no reliability branches in scheduler; run targeted tests.
- [x] Write and run memory/Postgres before/after feedback-append/checkpoint crash matrix; fix until event traces and correction counts agree.
- [x] Commit logical passing increments with the required trailer. Stop and report if a spec decision proves incorrect.

### Task 2: Measure, document, and verify

**Files:** `docs/usage/agent.md`, `docs/migration-3.0.md`, `CHANGELOG.md`, WP7 measurement report.

- [x] Measure memory/Postgres 200x4 base/default/None, one interleaved pair each under `flock /tmp/wp8-cpu.lock`; report all metrics and <=~2% clean-path comparison.
- [x] Document new default, None opt-out, prerequisites/completion, bounded retries and dropped rescue.
- [x] Review full branch against final requirements and budget; resolve concrete findings.
- [x] Run full pytest with supplied Postgres DSN, ruff check/format, pyright with specified interpreter.
- [x] Commit docs and measurements after gates, verify clean worktree; final report <250 words with decisions, sizes, benchmark pair, crash matrix, gates and deviations.

## Initial evidence

Baseline pytest: 2,039 passed, 223 skipped, one failure in `tests/tools/test_execution_backend.py::test_local_backend_nonzero_returncode` (timeout, before WP7 changes). Read-only loop and independent durability analyses found no required architecture change. Replay should append a canonical correction message and commit counter/events at the existing turn-complete checkpoint; never persist an increment independently before its feedback.

The baseline subprocess test passes outside the sandbox (1 passed). Local
Postgres and subprocess gates need the escalated execution context.

Ruling: invalid text may use a fixed assistant diagnostic plus canonical user
feedback in one atomic `session.append`. The existing `primary_append`
projection records their digests and feedback; resume reconstructs the fixed
diagnostic and proves the ordered suffix. This preserves strict envelope
privacy without new pending state or codecs. The rejected-native receipt
already provides its sanitized rejection fact. A concern based on feedback-only
projections was investigated and retracted; no architecture change is needed.
The requested correction crash matrix covers feedback append and checkpoint,
not the pre-existing accepted-receipt-before-projection boundary.

Ruling: the seam covers the enumerated model-response final routes. Explicit
host `StopPredicateHook` early-success decisions precede provider execution and
retain their documented authority; no synthetic model-final candidate is added
for them. The usage section records this configuration interaction.
