Measured fix `b3616b0` on `scale/hot-path`, parent `4aa90bd` — store outage recovery, 2026-10-31.

Transient durable-store connection failures interrupt an active resumable run instead of failing it. The classifier sits beside `LeaseLostError`; the store-call boundary supplies provenance, so provider/tool errors retain their ordinary behavior.
Classification covers `OSError` (including connection/timeout errors), asyncpg connection/interface errors, shutdown/not-ready/connection-limit errors, and explicit wrapped causes. MRO matching preserves asyncpg subclasses without requiring the optional driver import; cause traversal is cycle-safe.
The private interruption marker subclasses `LeaseLostError`. Existing ledger passthrough, background cancellation, no-write state, buffered-flush suppression, and snapshot-cleanup suppression apply unchanged. No transport Error/Result terminal is emitted or persisted; the worker's existing nonterminal check triggers dispatch retry or lease takeover.
Non-transient validation/serialization/constraint failures keep their error terminals. Creation errors and operations outside an active resumable run preserve their existing behavior. No worker, runner, journal format, checkpoint field, public name, or wire-version changes.

Regression first: the checkpoint-before-tool test was RED because the old loop emitted Error/Result. It is now GREEN across five error kinds (connection loss, interface, OS, connection, wrapped OS), each before and after the underlying checkpoint commits: ten cases.
Each case checks no post-failure store writes or terminal, a running durable record, successful fresh-session resume, one tool execution/result block, one final ResultEvent, and contiguous event seqs. A separate actual model-call-journal write failure also resumes successfully.
The SQL-only outage regression uses a throwaway database, `ALLOW_CONNECTIONS false` and backend termination, then recovery after three seconds; it now asserts zero failed runs. Both recovery and stop-during-outage variants pass. Ordinary failure fixtures use non-transient errors, retaining provider/session-only OS failures and Postgres fencing coverage.
Normalized text-only and multi-tool success traces match an independently imported `4aa90bd` SDK exactly.

Acceptance uses the unchanged load harness under the shared CPU lock: M4/R25.2, RTT 1 ms, pool 16/process, one witness reader, unlimited concurrency, poll 1 s, lease 4 s/stall 6 s, warmup 15 s, steady 60 s, drain up to 120 s.

| Case | Durable runs | Completed | Failed | Unexpected worker exits | Reclaimed / slowest first reclaim |
|---|---:|---:|---:|---:|---:|
| r9 recorded 3 s blip | 1,820 | 1,816 | 4 | 0 | 29 / 6.113 s |
| Fixed 3 s blip | 1,835 | 1,835 | 0 | 0 | 32 / 5.994 s |
| Fixed worker kill | 1,903 | 1,903 | 0 | 0 | 36 / 5.910 s |

Reclaim counts and delays use the harness's selected pre-fault lease owner; the delay is the latest first takeover, not completion time. The kill cohort covers the killed worker's orphans; terminal/invariant checks cover the whole fleet.
Both fixed cases PASS every invariant: one terminal result, contiguous seqs, no post-result writes, missing errors, orphans, writer mismatches, duplicate notes, reader errors, or failed runs. Kill excludes the intentionally killed process; its slowest first reclaim is below the 7 s limit.
The quick matrix also PASSes: normal 37/37, kill 35/35, blip 19/19, drain 39/39 completed, zero failures and all invariant counters zero. Quick kill reclaimed six runs within 5.712 s; the drained owner made zero post-signal claims. Quick settings are M4/R2, 100 readers, warmup 2 s, steady 15 s, drain 45 s.

Net versus `4aa90bd`: **src +46 lines** (five in loop runtime), **tests +204**, **harness 0**. The core changes are the private classifier/marker, store-boundary translation, and terminal-emission guard. Existing failure fixtures preserve their non-transient purpose; the coordination test import follows the merged `linch.ext.evals` move.
Gates: **2,521 passed, 18 skipped** with the Postgres DSN in 125.00 s; Ruff check and format clean (436 files); Pyright with the explicit interpreter **0 errors / 0 warnings**. Diff check clean; no generated JSON committed.
Capacity is unchanged. Per the owner's record, r9 knees shared the host with four other workers running full test suites and are not comparable with the earlier gate; no capacity tuning or remeasurement was performed here.
