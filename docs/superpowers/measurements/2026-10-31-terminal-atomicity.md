# WP8 terminal atomicity follow-up

Atomic terminal writes are implemented. The owner-approved comparison exception
excludes `PromptCacheAdvisoryEvent`, a per-process diagnostic; no advisory state
is added to checkpoints.

Baseline: `7811c28`. Same scale harness and deterministic provider/tool workloads,
one interleaved baseline/current pair for all six cases. Both sides use the same
working path and Postgres DSN; a shared CPU lock serializes collection. These are
individual timing observations, not a statistical CPU claim.

Failure terminals commit events, checkpoint, failed status, and error metadata in
one fenced statement. Abort and interrupt terminals use combined writes. The
guard-event replay check is removed; reminder history recovery remains. Reminder
events are buffered before their checkpoint. A failed combined write suppresses
standalone cleanup flushing of its pending prefix. Cleanup also keeps an
uncommitted tool-results tail buffered if cancellation precedes that write.

Final confirmation against `7811c28`: one interleaved pair per requested case,
with the shared CPU lock held across all four samples. These are the final
runtime sources, including the cancellation cleanup guard.

| Case | runs/s before → after | ms/turn before → after | calls/turn | cycles/turn before → after |
| --- | --- | --- | --- | --- |
| postgres_200x4 | 20.332 → 16.883 | 12.296 → 14.808 | 14.25 | 15.463 → 15.488 |
| memory_200x4 | 24.863 → 17.946 | 10.055 → 13.930 | 14.25 | — |

Both final after samples are slower; no timing improvement or unchanged timing
is claimed. Both cases pass correctness checks, with identical API-call counts,
checkpoint bytes (8071.25/turn), and session-append bytes (299/turn).

The earlier six-case pair below predates only the cancellation cleanup guard.
It uses the same baseline and harness; all cases passed correctness checks.

| Case | runs/s before → after | ms/turn before → after | calls/turn | cycles/turn before → after |
| --- | --- | --- | --- | --- |
| memory_1000x4 | 23.690 → 28.804 | 10.553 → 8.679 | 14.25 | — |
| memory_1x50 | 1.358 → 1.232 | 14.725 → 16.228 | 14.02 | — |
| memory_200x4 | 19.741 → 26.684 | 12.664 → 9.369 | 14.25 | — |
| postgres_1000x4 | 15.013 → 17.082 | 16.652 → 14.635 | 14.25 | 15.297 → 15.297 |
| postgres_1x50 | 0.348 → 0.376 | 57.529 → 53.249 | 14.02 | 14.480 → 14.480 |
| postgres_200x4 | 18.588 → 17.862 | 13.450 → 13.996 | 14.25 | 15.475 → 15.488 |

All six cases pass correctness checks. Call counts, checkpoint bytes, and session
append bytes are identical. Raw event-byte differences are below 0.03% (duration
digit lengths); normalized success traces match `7811c28` and `47c17d3` byte for
byte. Postgres 200×4 has 0.0125 more protocol cycles/turn from cold prepares;
1,000×4 and 1×50 cycles are identical.

| Payload bytes/turn | checkpoint | events before → after | session append |
| --- | --- | --- | --- |
| memory_1000x4 | 8071.25 | 4538.68 → 4538.43 | 299.00 |
| memory_1x50 | 11517.42 | 4383.12 → 4383.10 | 268.08 |
| memory_200x4 | 8071.25 | 4538.81 → 4537.52 | 299.00 |
| postgres_1000x4 | 8071.25 | 4536.32 → 4535.97 | 299.00 |
| postgres_1x50 | 11517.42 | 4383.14 → 4383.12 | 268.08 |
| postgres_200x4 | 8071.25 | 4535.42 → 4535.32 | 299.00 |

At 1,000 sessions, memory lag p99/max is 8739 → 6295 ms; Postgres lag p99 is
36.1 → 40.5 ms and max is 202 → 223 ms. Timing varies in both directions. The
memory 1×50 outlier was checked with two additional alternating pairs: runs/s
before = 1.358, 1.194, 1.320; after = 1.232, 1.413, 1.063. Medians are
1.320 → 1.232 (15.153 → 16.228 ms/turn). This does not establish absence of
a timing regression; no CPU improvement is claimed. The six-case collection
precedes the final cancellation-only cleanup guard; success traces remain equal.

| Crash point (before call and after commit) | Memory | Postgres |
| --- | --- | --- |
| Guard stop: combined failed terminal | pass / pass | pass / pass |
| Force-final: reminder session append | pass / pass | pass / pass |
| Force-final: first turn-complete checkpoint | pass / pass | pass / pass |
| Force-final: following checkpoint | pass / pass | pass / pass |

The matrix checks contiguous sequence numbers on every stored row before
excluding `PromptCacheAdvisoryEvent` by type from both sides. Every other event
must match exactly after ID/timing normalization: payload, type, order and count.
All 16 combinations pass and each run commits terminal status exactly once;
postcommit stop rejects resume without another terminal write. The owner ruling
keeps advisories as per-process diagnostics: fresh resume restarts comparison,
so its first request can omit an advisory an uninterrupted run would emit.

Current source net: +251 versus `47c17d3` (-47 versus `7811c28`). Benchmark
contract tests: 98 additions / 2 deletions versus `47c17d3`.
Final gates: **2245 passed, 18 skipped** with Postgres (80.26 s); Ruff check
and format check pass (419 files); Pyright reports **0 errors / 0 warnings**.
Final normalized success traces match both `7811c28` and `47c17d3` byte for byte.
Independent review found no new atomicity, fencing, sequence, or cancellation
defect; the scoped exception preserves all other crash assertions.
