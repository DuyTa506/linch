# WP7 round 2 — reliability measurements

Base: `1208d1f7f2a330c59b12d507b1a27e786e1f35b5`; merge: `d7e9f9e`.
Measured source: `09f51de2375c48e8f7d55c7dcaf0eac249040b50`.
Source **+548** = seam 199 + policy/runtime 188 + loop 107 + Agent/API 54.
`scheduler.py`: **zero diff** against the new base; no per-tool reliability checkpoint.
Test net **+1301**: 797 primary, 498 crash, 6 existing-fixture lines.

## Benchmarks

All sets held `flock /tmp/wp8-cpu.lock`: fake provider/tools, 200 sessions × 4 turns.
Per backend: base/default/None/None/default/base; None changes only Agent construction.
Controlled repeat: `PYTHONHASHSEED=0`, one base warmup per backend; earlier data retained.

| Case | Setup | Default runs/s | None runs/s | Base runs/s | Elapsed Δ/base | Δ/None |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| memory | unseeded | 45.529 | 47.516 | 43.921 | -3.37% | +4.61% |
| Postgres | unseeded | 31.123 | 31.261 | 31.621 | +1.60% | +0.44% |
| memory | controlled | 47.488 | 48.173 | 48.000 | +1.08% | +1.41% |
| Postgres | controlled | 27.572 | 28.023 | 28.339 | +2.76% | +1.64% |

Rates are two-sample means; deltas compare mean elapsed times, not rounded rates.
Every sample completed 200/200 correctly; all variants retained **14.25 writes/turn**
and **8071.25 checkpoint bytes/turn**. No model-quality or speedup claim is made.
Timing is noisy near 2%: controlled Postgres base samples differ by 3.24%; unseeded
memory baselines ranged roughly 43–49 runs/s. Controlled incremental cost: 1.41–1.64%.
Raw sets: `/tmp/wp7-r2-pair-09f51de-unwarmed.json`, `/tmp/wp7-r2-controlled-pair.json`.

## Recovery evidence

**48 correction cases**: text/native/completion/final-tool, memory/Postgres, before/after
feedback append/checkpoint, including second consecutive text/native corrections.
**8 ToolEnd cases**: serial/parallel, live/restarted, both stores, before batch checkpoint.
**2 repeated-recovery cases** retain canonical background proof across two crashes.
Also covered: 8 literal-diagnostic collisions, canonical background live/replay cases,
no-listener trace equivalence, two agents and two simultaneous runs on one agent.
Durable traces match uninterrupted runs: only PromptCacheAdvisoryEvent is filtered,
using WP8 volatile-field normalization. Corrections, usage and receipts count once.

## Decisions and requested answers

First-position PreToolUse hook; Context-owned run map; run-owned idempotent Disposable.
Success folds once from final normalized/recovered results at `tool_results_appended`;
original and canonical background launches are excluded. No snapshot/scheduler seam.
The 74-line dispatcher only type-checks serial vetoes; one helper appends all feedback.
**Item 3:** retain `primary_append(after_action="continue")`. After its durable write
but before the first diagnostic/feedback append, history has no suffix and the accepted
receipt has no response text. Tail matching cannot reconstruct the absent messages.
Existing diagnostic/control proof restores suffix 0/1/2 without retaining raw arguments.
**Item 4:** off paths terminate before assembly-dependent processing. Unread context
receipt/rejected fields were removed; local receipt/usage accounting is unchanged.
`tests/loop/test_model_call_accounting.py::test_rejected_native_arguments_are_charged_but_never_durable`
covers four malformed forms × None/retry-invalid=False: one receipt/usage, zero sibling tools.
Original batch cursor is retained while recovered results await the batch checkpoint.
No new journal, namespace, wire codec or durable action metadata.

## Final gates

**2379 passed, 18 skipped**; Ruff lint/format pass (430 files); Pyright **0 errors**,
5 baseline warnings. Required venv and explicit pythonpath used. Independent review passes.
