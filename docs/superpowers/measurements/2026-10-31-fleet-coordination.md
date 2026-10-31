Candidate `70c7a68` versus measured baseline `ac2033c` — fleet coordination, 2026-10-31.

**Performance acceptance FAILED.** After-change M8/M1 ratios are worker CPU/TPS **1.55**, Postgres CPU/TPS **1.47**, and commits/turn **1.294**, exceeding limits **1.3 / 1.3 / 1.1**. M1 claim-wait p99 remains above 45 ms.
The pending decision is whether to expand queue integration or stop with this partial improvement; this report establishes neither acceptance nor new capacity.

Each point offered R16 with M=1/2/4/8, RTT 1 ms, pool 16, one reader, unlimited run concurrency, poll 1 s, warmup 15 s, steady 60 s, drain 120 s; PostgreSQL 16.2/PgQueuer 1.6.0.
CPU/TPS is summed process CPU percentage-points divided by completed steady runs/s; Postgres CPU sums its processes. Cells show baseline → candidate.
PgQueuer table INSERT/DELETE broadcasts wake every manager; an empty pass issues dequeue plus up to three eligibility probes. Health messages resolve matching UUIDs without waking dequeue.

| Workers | TPS | Worker CPU/TPS | PG CPU/TPS | Commits/turn | Claim wait p99 ms |
|---:|---:|---:|---:|---:|---:|
| 1 | 15.02 → 16.02 | 3.760 → 3.356 | 1.958 → 1.350 | 36.858 → 18.590 | 52.3 → 50.1 |
| 2 | 16.53 → 16.10 | 4.404 → 3.759 | 2.280 → 1.457 | 39.201 → 19.469 | 32.5 → 27.6 |
| 4 | 15.88 → 15.93 | 5.467 → 4.302 | 3.135 → 1.596 | 43.538 → 21.062 | 30.5 → 26.4 |
| 8 | 16.03 → 16.97 | 7.537 → 5.187 | 5.297 → 1.984 | 52.355 → 24.058 | 30.0 → 27.8 |

M8 mechanism counts below use weighted own-job normalization Σworker counts/Σworker factory starts; mean-worker/fleet-job normalization divides that value by eight.

| Physical receipt / dequeue metric | Per own job, before → after | Mean worker per fleet job, before → after |
|---|---:|---:|
| INSERT receipts | 8.000 → 8.000 | 1.000 → 1.000 |
| DELETE receipts | 8.017 → 7.968 | 1.002 → 0.996 |
| Health-check receipts | 0.400 → 0.376 | 0.050 → 0.047 |
| Cancellation / UPDATE / TRUNCATE receipts | 0 → 0 | 0 → 0 |
| Dequeue calls | 16.800 → 8.874 | 2.100 → 1.109 |
| Empty dequeues | 15.800 → 7.874 | 1.975 → 0.984 |

Worker counters use steady entry time; factory starts count execution attempts, distinct from dequeue results, assistant turns, and terminal completions. Physical notifications are counted before client filtering, including health/cancel types; absent operations mean zero.
All eight candidate workers executed real jobs (95–165 factory starts each); there is no single execution leader. Raw per-worker counts remain retained with the experiment artifacts.
DELETE filtering retains completion wakes for positive queue limits; its 38.2 ms limited-queue unit result is not a claim-p99 measurement.
Schedule concurrency 0 relies on the existing per-definition database lock; the private PgQueuer timeout override polls deferred/retry/stale work at 1 s. This private hook is version-sensitive.
Baseline M1 worker/API default resets were 16.83 queries/turn against 36.86 commits/turn (about 45.7% lower-bound share); parent audit resets were excluded. Dedicated harness pools now skip default reset SQL; healthy worker/API reset counters are 0, retaining asyncpg internal cleanup and Worker LISTEN-connection closure.

Separate baseline M8/R16 profile: w-0 only, steady 30 s, 8,694,805 calls, 29.924 s elapsed; top 15 by cumulative seconds, in order:
`select` 23.041; `epoll.poll` 22.851; `Context.run` 6.294.
`_invoke` 4.119; `_iterate` 4.029; `run_loop` 4.018.
`_run_loop_impl` 3.661; telemetry `store_call` 2.255; telemetry `store_call` 2.234.
`run_provider_turn` 1.920; `save_checkpoint` 1.509; `save_checkpoint_with_events` 1.470.
`_json_safe` 1.261; `_json_safe_inner` 1.253; `_save_checkpoint` 1.222.
Cumulative times overlap; epoll is waiting, so these are not CPU attribution percentages. Profiled points are separate from the unprofiled table.

[PostgreSQL 16 notification source](https://raw.githubusercontent.com/postgres/postgres/REL_16_STABLE/src/backend/commands/async.c) wraps `ProcessIncomingNotify` queue scans in internal transactions; client DELETE filtering runs after this server work.
An isolated 40-notification observation used identical connections/SQL controls: no-LISTEN commits 43/45; matching LISTEN 87 with 40 frontend messages; unrelated private LISTEN 87 with 0 frontend messages. Control drift was ±2 commits; this supports internal receive transactions without proving exactly one transaction per notification.
M8−M1 candidate increments were 3.588 physical receipts/turn + 1.761 dequeues/turn = 5.349 versus 5.469 extra commits/turn: 97.8% observational reconciliation, not an exact transaction identity. Scans can batch messages or process unrelated channels.
SQL diagnostics confirmed the actual timeout-override manager, 134 prepares versus 4,092 worker dequeues, cache 100/max 15,360 chars, longest query 6,813 chars. Explicit BEGIN/COMMIT groups mean raw SQL requests are not transaction counts.
These are single points without confidence intervals; realized arrivals, different counter cohorts, CPU sampling, and database-stat endpoint/flush timing limit precision. They do not erase the failed ratios.

Change scope: worker source +40 net lines, harness +56, tests +341; no loop/scheduler changes. Reset optimization applies to dedicated stateless pools, not application-shared pools with session state/listeners.
Candidate full gates: 2,459 passed, 18 skipped in 123.90 s; Ruff 435 files; Pyright 0 errors/0 warnings. Normalized success traces match baseline `96ee8f3` using separately imported SDKs; correctness is not performance acceptance.
Quick matrix: CLI 0 for none 33/33, kill 38/38, blip 31/31, drain 41/41 terminal/completed; all invariant counts 0 and failed runs 0. Kill recovered all 4 orphans, maximum 6.166 s; blip worker exits were 0.
New knees, efficiency brackets, and full chaos at 70% of a new knee were **NOT RUN** because low-rate acceptance failed; they remain pending the architectural decision.
Production Capacity numbers remain unchanged. Raw experiment JSON/profiles are retained outside the repository; no per-point JSON is committed.
