Candidate `6b1d8b2` (runtime/harness `70c7a68`) versus measured baseline `ac2033c` — fleet coordination, 2026-10-31.

**Revised low-rate CPU acceptance PASS:** M8/M1 worker CPU/TPS **1.55 ≤ 1.6**, Postgres CPU/TPS **1.47 ≤ 1.5**. Commits/turn ratio is **1.294**; the accepted residual slope is consistent with the shared broadcast/LISTEN internal-transaction floor.
**Full outage acceptance FAIL:** four runs durably failed during the 3 s blip; workers survived. M1 claim-wait p99 is 50.1 ms versus baseline 52.3 ms, above the earlier 45 ms reference. CPU acceptance does not establish improved capacity.

Shared six-core / **12-logical-CPU** host; PostgreSQL 16.2/PgQueuer 1.6.0; RTT 1 ms, pool 16 per process, one reader, unlimited run concurrency, poll 1 s; warmup 15 s, steady 60 s, drain up to 120 s. Runs contain four simulated turns.
CPU/TPS is summed process CPU percentage-points divided by completed steady runs/s; Postgres CPU sums its processes. Run latency is scheduled API arrival → terminal timestamp; claim wait ends at actual lease claim.
The R16 before/after points below reuse the `70c7a68` campaign because runtime/harness are unchanged by `6b1d8b2`; new knees use fresh unloaded R2 references and the same gate: start R16, double, then refine by midpoints to a bracket ≤25% of its upper rate.
Stable means clean correctness and run p99 ≤ twice that fleet's R2 p99: M1 **4.457 s**, M4 **4.573 s**, M8 **4.719 s**. All capacity points have zero correctness violations. R16 cells show baseline → candidate.

| Workers | TPS | Worker CPU/TPS | PG CPU/TPS | Commits/turn | Claim wait p99 ms |
|---:|---:|---:|---:|---:|---:|
| 1 | 15.02 → 16.02 | 3.760 → 3.356 | 1.958 → 1.350 | 36.858 → 18.590 | 52.3 → 50.1 |
| 2 | 16.53 → 16.10 | 4.404 → 3.759 | 2.280 → 1.457 | 39.201 → 19.469 | 32.5 → 27.6 |
| 4 | 15.88 → 15.93 | 5.467 → 4.302 | 3.135 → 1.596 | 43.538 → 21.062 | 30.5 → 26.4 |
| 8 | 16.03 → 16.97 | 7.537 → 5.187 | 5.297 → 1.984 | 52.355 → 24.058 | 30.0 → 27.8 |

M8 mechanism counts use weighted own-job normalization Σworker counts/Σworker factory starts; mean-worker/fleet-job normalization divides that value by eight.

| Physical receipt / dequeue metric | Per own job, before → after | Mean worker per fleet job, before → after |
|---|---:|---:|
| INSERT receipts | 8.000 → 8.000 | 1.000 → 1.000 |
| DELETE receipts | 8.017 → 7.968 | 1.002 → 0.996 |
| Health-check receipts | 0.400 → 0.376 | 0.050 → 0.047 |
| Cancellation / UPDATE / TRUNCATE receipts | 0 → 0 | 0 → 0 |
| Dequeue calls | 16.800 → 8.874 | 2.100 → 1.109 |
| Empty dequeues | 15.800 → 7.874 | 1.975 → 0.984 |

Counters use steady entry time; factory starts are execution attempts, distinct from terminal completions or turns. Physical receipts are counted before client filtering. All eight workers executed real jobs (95–165 starts each), with no execution leader.
INSERT broadcasts still wake all managers. DELETE/TRUNCATE filtering suppresses unlimited completion probes while positive queue limits retain completion wakes; health resolves matching UUIDs without waking dequeue. The original limited-queue DELETE-to-start observation was 38.2 ms, not claim-p99.
Schedule concurrency 0 relies on the per-definition row lock: `test_concurrent_schedule_fires_serialize_per_definition` issues eight duplicate fires through independently acquired DB connections for two definitions and asserts exactly two runs.
The private hook removes three eligibility probes; eligible work whose notification was already drained may wait up to `poll_interval_s` before the next dequeue, plus scheduling/DB delays. This TOCTOU fallback uses configured polling; PgQueuer is pinned to **1.6.x** (`>=1.6,<1.7`) and tests guard `_effective_dequeue_timeout`/`fetch_jobs`.
Baseline M1 worker/API resets were 16.83 queries/turn against 36.86 commits/turn (**45.7% lower-bound share**); parent audit resets were excluded. Dedicated stateless harness pools now issue zero default reset queries, retaining asyncpg internal cleanup and Worker LISTEN-connection closure; application-shared pools with session state/listeners need their normal reset.

Separate baseline M8/R16 profile: w-0 only, steady 30 s, 8,694,805 calls, 29.924 s elapsed; top 15 by cumulative seconds, in order:
`select` 23.041; `epoll.poll` 22.851; `Context.run` 6.294.
`_invoke` 4.119; `_iterate` 4.029; `run_loop` 4.018.
`_run_loop_impl` 3.661; telemetry `store_call` 2.255; telemetry `store_call` 2.234.
`run_provider_turn` 1.920; `save_checkpoint` 1.509; `save_checkpoint_with_events` 1.470.
`_json_safe` 1.261; `_json_safe_inner` 1.253; `_save_checkpoint` 1.222.
Cumulative times overlap and epoll is waiting, so these are not CPU attribution percentages; profiling is separate from the unprofiled points.

[PostgreSQL 16 notification source](https://raw.githubusercontent.com/postgres/postgres/REL_16_STABLE/src/backend/commands/async.c) wraps `ProcessIncomingNotify` queue scans in internal transactions; client DELETE filtering runs after this work, leaving a broadcast/LISTEN floor.
Isolated 40-notification controls: no-LISTEN commits 43/45; matching LISTEN 87 with 40 frontend messages; unrelated private LISTEN 87 with zero frontend messages. Control drift ±2 supports receive transactions without proving exactly one transaction per notification.
M8−M1 candidate increments were 3.588 physical receipts/turn + 1.761 dequeues/turn = 5.349 versus 5.469 extra commits/turn: 97.8% observational reconciliation, not an exact identity. Scans may batch messages or process unrelated channels.
SQL diagnostics confirmed the override manager, 134 prepares versus 4,092 dequeues, cache 100/max 15,360 chars, longest query 6,813 chars. Explicit BEGIN/COMMIT groups mean SQL requests are not transaction counts.

| Workers | Stable / first-bad offered runs/s | Stable achieved runs/s | Run p99 s | Efficiency |
|---:|---:|---:|---:|---:|
| 1 | 20 / 24 | 19.983 | 5.301 | 100.0% |
| 4 | 32 / 40 | 31.000 | 7.392 | 38.8% |
| 8 | 24 / 32 | 25.083 | 8.066 | 15.7% |
Efficiency = achieved TPS(M)/(M × achieved TPS(1)). These are observed single-sample brackets on a shared host, without confidence intervals or indefinite-load guarantees. Saturated samples were nonmonotonic (M4/R40 versus R48); the earlier scale gate had a higher M8 result, so capacity improvement is not established.
Realized arrivals, counter cohorts, CPU sampling and database-stat endpoint/flush timing also limit precision. Size database/pool capacity with measurements from the deployment host; process count does not imply linear scaling.

Change scope versus `96ee8f3`: worker source **+40** net lines, tests **+215**, harness **+56**; no loop/scheduler changes. Completion regression remains baseline M1/M2 **3/6 → 0/0** empty dequeues; trimmed worker tests **84 passed**. Independently imported normalized success traces match `96ee8f3`.
Final gates on `6b1d8b2`: **2,456 passed / 18 skipped** in 131.62 s with Postgres enabled; Ruff check/format **435 files** clean; Pyright explicit interpreter **0 errors / 0 warnings**; offline lock check passed. These code gates do not override the full-blip failure.
Quick matrix **PASS**: none **43/43**, kill **30/30**, blip **24/24**, drain **40/40** completed; all invariant counts zero, no failed runs. Kill reclaimed all four orphans within **5.998 s**; drain owner claims after signal **0**.
Full chaos M4/R25.2 (70% of knee-bracket midpoint): kill **PASS 1,910/1,910**, 33 orphans reclaimed within **6.035 s** (limit 7 s); 3 s blip **FAIL**, all **1,820** terminal but **1,816 completed / 4 failed** (`ConnectionDoesNotExistError`), zero worker exits and all other invariants zero; 29 reclaims within **6.113 s**. The unchanged loop converts checkpoint/model-call transport errors into failed terminals when persistence recovers; this needs a separate recovery-policy fix.
Raw experiment JSON/profiles remain outside the repository; no per-point JSON is committed. Earlier [scale-gate measurements](2026-10-31-scale-gate.md) describe the separate network/PgBouncer campaign.
