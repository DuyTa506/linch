# Worker-owned stores and schema verification reuse

Baseline: `33cb316` on `scale/hot-path`. Worker factories now receive the
worker-owned run/session stores. A retained borrowed-pool wrapper owns a shared
initialization lock and successful schema-contract cache; it preserves adapters
across agent teardown. Verification is trusted for a fixed database, role and
search path. Raw-pool lifecycle behavior is unchanged. Lease maps are removed
after quiescence and release, only for the matching owner and epoch.

## Statement proof

Real Postgres, using the WP8 protocol counter with SQL exposed on cached binds.
Executed-SQL counts exclude prepare/statement-close cycles. Driver codecs are
warmed on eight physical connections; the producer uses a separate pool.
Worker.run performs startup, then a controlled QueueManager driver invokes two
sequential and six concurrent real `_handle` callbacks and terminal checks.
This test measures callback/store work, not dequeue/ACK traffic.

| Scope | Protocol cycles | Schema/catalog/metadata SQL | Advisory lock calls |
| --- | ---: | ---: | ---: |
| Setup and Worker startup | 117 | 30 | 4 |
| Eight jobs after startup | 325 | 0 | 0 |
| Fresh verified adapters, close and retry | 0 | 0 | 0 |

All eight runs complete. Factories receive identical stores and pool; all lease
maps and active-run state are empty afterwards. Separate tests cover shared
initialization, failure/cancellation retry, raw-pool close behavior and stale
release overlapping a newer local claim. Focused suite: 117 passed.

## Bounded fleet points

WP18 harness copied into scratch; no harness is committed here. Pinned harness
content SHA-256: `e85c1d4a242432b49ba921baa87e6758c1911f56013ad1df28df76e46eef0a36`
(identical at load-test revisions c95d6f5 and c0a3bd1). Before imports archived
`33cb316` sources and uses the original per-job session factory; after imports
the frozen candidate and uses `job.session_store`. Other harness settings match.

Each point: R=16 Poisson arrivals/s, RTT=1 ms through Toxiproxy, four provider
turns/run (0.3 s TTFT + 0.7 s deltas/turn), pool size 8, unlimited queue
concurrency, no readers/chaos, 15 s warmup, 60 s steady window, drain bounded
at 45 s. Host pool reset settings are unchanged. One exclusive
`/tmp/wp8-cpu.lock` spans before M4, after M4, after M8, before M8.

Completed runs/s counts finishes in the steady window. P99 includes queue time
for steady arrivals that finish during the bounded run/drain. Advisory share
is `Lock:advisory / all sampled backend observations`, including idle clients;
it is a sample share, not elapsed wait time. Sampling is database-wide.

| Workers | Completed runs/s before → after | p99 seconds before → after | Advisory sample share before → after |
| --- | ---: | ---: | ---: |
| 4 | 15.417 → 16.133 | 4.764 → 4.812 | 0.315% → 0.000% |
| 8 | 16.533 → 16.200 | 4.854 → 4.806 | 0.309% → 0.000% |

| Point | Completed/total | Advisory/all observations | Advisory/active observations |
| --- | ---: | ---: | ---: |
| before M4 | 1155/1155 | 28/8893 | 28/479 |
| after M4 | 1232/1232 | 0/8627 | 0/665 |
| before M8 | 1242/1242 | 48/15511 | 48/1295 |
| after M8 | 1234/1234 | 0/15396 | 0/1198 |

Active observations exclude `Client:*`. All points have zero run failures,
client errors, renewal failures, sequence gaps, duplicate notes, ownership
mismatches, orphaned runs and other invariant errors. The harness drains and
removes its own processes and schemas.

The requested R16 baseline points already reach the offered rate: the prior
12–14 runs/s plateau was not reproduced. Poisson arrival counts differ, so these
points establish removal of observed advisory contention, not a capacity gain.

## Size and gates

Source net: +25 for this item, +276 versus `47c17d3` (limit +300). Required
blocks: borrowed verification/lifecycle +27, moved dispatch wrapper -11,
worker session/store ownership +5, matching-release fence cleanup +4.
No module-global mutable state, durable write cache or new top-level export.

Final gates: **2258 passed, 18 skipped** with Postgres (78.41 s); Ruff check
and format check pass (420 files); Pyright reports **0 errors / 0 warnings**.
Normalized success traces match `33cb316` byte for byte.
Independent code review found no actionable correctness defect.
