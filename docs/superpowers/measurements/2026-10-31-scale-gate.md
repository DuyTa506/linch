# Scale-ready acceptance gate — SDK `8d32b51` — 2026-10-31

Measured SDK: `8d32b51755b557a88f4163d60d28724b5b25f5f4` (WP8 + default reliability); final harness: `d440e78`, 679 lines, **net +80** from the merged 599-line harness. SDK source stayed fixed throughout; harness reporting/audit guards evolved during the campaign. No SDK source edits.
Topology: one API/enqueuer → Toxiproxy 2.13.0 → dedicated Postgres 16.2 (`127.0.0.1:55432`, 300 connections), with M independent worker processes. PgBouncer 1.16.1 points insert a private transaction pool between proxy and Postgres; system port 6432 is untouched.

## Method

- Shared six-core / 12-logical-CPU host: workers, proxy and Postgres share CPUs, making these knees lower bounds for separately provisioned hosts. CPU 100% means one logical core; PG sums its processes.
- Per point: 15 s warmup, 60 s steady, up to 120 s drain; pool 16 per process, unlimited queue concurrency, lease/stall 4/6 s, polling 1 s. One witness reader for capacity/chaos/pool comparison; 100 for every network point.
- Four synthetic turns: 300 ms first token, 20 deltas over 700 ms, read tools and a receipt-keyed note write with 30–80 ms fake I/O. Default reliability stays enabled. Open-loop Poisson arrivals are scheduled independently of completion.
- SQL computes throughput, distributions and all durable-run checks, including ambiguous enqueue commits. Latency is scheduled API arrival → terminal timestamp; wait is arrival → actual lease claim. Stored timestamps precede commit. Client lag/pool distributions are also aggregated in SQL; psutil supplies CPU.
- Toxiproxy adds the entire integer RTT upstream and zero downstream: one-way timing shifts while request/response round-trip cost is preserved. No throughput toxic. Every heavy command holds `/tmp/wp8-cpu.lock` and has a timeout; schemas/processes are isolated per point.
- “Stable” is the highest tested rate with p99 ≤ twice that M's unloaded R2 p99 and clean correctness checks. Ramp/refinement reached a first-bad point for every M; bracket width is ≤25% of its upper rate. Single Poisson samples give observed brackets, not confidence intervals or indefinite-load guarantees.

## Capacity at 1 ms RTT

Efficiency = TPS(M) / (M × TPS(1)); offered rates are runs/s. CPU columns are percentages.

| M | Stable / first bad | Stable TPS | p99 s | Worker CPU, each | PG / proxy CPU | Efficiency | Observed pressure |
|---:|---:|---:|---:|---:|---:|---:|---|
| 1 | 20 / 24 | 19.93 | 5.448 | 82 | 42.5 / 26.7 | 100.0% | pool / CPU |
| 2 | 24 / 32 | 24.25 | 7.137 | 82.1–83.1 | 87.2 / 51.9 | 60.8% | CPU / WAL |
| 4 | 24 / 32 | 24.60 | 5.864 | 68.4–70.3 | 158.1 / 77.3 | 30.9% | WAL / pool |
| 8 | 40 / 48 | 38.73 | 8.586 | 67.2–70.8 | 291.0 / 107.3 | 24.3% | WAL / shared host |

First-bad p99/TPS: M1 26.065 s/18.10, M2 16.219 s/27.02, M4 10.172 s/30.78, M8 16.083 s/41.18. All 19 capacity points completed every durable run correctly, including overload drains.

The next sizing targets are connection availability and the commit/WAL path, then worker CPU. At first-bad points every worker pool reaches 16 at p99; WALWrite accounts for 63.8/66.7/68.3/69.1% of non-ClientRead backend observations for M1/2/4/8. Those are snapshot fractions, not elapsed wait time; no steady advisory waits were sampled. At stable M4, pool p99 is 12–13; this is coupled pressure, not proof that one resource alone causes the knee.

A full M1/R24 comparison with pool 32 improved TPS 18.10→22.42 and p99 26.065→9.447 s, while worker CPU reached 98%. Pool 16 is a tested configuration constraint, not ultimate fleet capacity. This single comparison is not a replicated causal estimate. At M8/R48 the measured processes consume about 1,013% of 1,200% available CPU; host sharing contributes pressure without proving global CPU exhaustion. At the selected stable points, database commits/turn (~37–46) include queue, readers, resets and audit traffic, not just loop work.

## Network and 100-reader fan-in

M4, offered 19.6 runs/s (70% of the midpoint of its 24–32 bracket). All 100 readers completed 16–18 sampled subscriptions each with zero sequence errors.

| Added RTT ms | Completed / durable | TPS | Run p50 / p95 / p99 s | Claim wait p99 ms | Read lag p99 / max ms | PG / proxy CPU |
|---:|---:|---:|---|---:|---:|---:|
| 0 | 1473/1473 | 19.40 | 4.578 / 4.961 / 5.109 | 117 | 3225 / 4371 | 116.6 / 59.4 |
| 1 | 1415/1415 | 18.72 | 4.765 / 4.955 / 5.020 | 119 | 1917 / 3358 | 107.5 / 63.4 |
| 2 | 1472/1472 | 19.33 | 4.926 / 5.165 / 5.229 | 141 | 176 / 4227 | 121.2 / 70.5 |

All network correctness checks PASS. Median run latency rises by 188/348 ms at RTT 1/2 versus RTT 0, measuring the whole fleet path rather than isolated loop round trips. Arrival realizations and sampled histories differ; the non-monotonic tails do not show a benefit from added RTT. Reader lag includes history catch-up, polling, pool waits and pre-commit timestamps; it is not a pure delivery SLA.

## Chaos acceptance at M4 / 19.6 runs/s / RTT1

“Common” checks every durable run terminal with exactly one final result, contiguous seq from 1, zero writer mismatches, zero failed-without-error and no duplicate note attempts/rows. All are PASS in every full and quick point.

| Case | Completed / failed / durable | Common | Worker exits | Mode-specific requirement | Result |
|---|---:|---|---|---|---|
| SIGKILL active worker, final audit | 1489 / 0 / 1489 | PASS | Only injected kill | All 34 orphans first reclaimed within 5.945 s ≤7 s; zero failed | PASS |
| Proxy disabled for 3 s | 1395 / 0 / 1395 | PASS | 0; same four PIDs recover | All durable runs terminal; target zero failed met | PASS |
| SIGTERM / drain | 1479 / 0 / 1479 | PASS | Only requested drain | Zero post-signal claims; all complete | PASS |

The outage generated 59 enqueue exceptions (58 connection-refused, one connection-closed); SQL still includes every durable commit. These caller-visible errors do not establish 59 lost jobs. PID observation detects clean early exits too; none occurred unexpectedly. A prior kill sample also passed (1487/1487), then the final audit strengthened recovery from one selected run to the slowest first reclaim across every orphan.

## PgBouncer transaction mode

Application → Toxiproxy RTT1 → private PgBouncer → Postgres; `statement_cache_size=0`, trust auth, backend pool limit 64 per temporary process role. Role defaults preserve the fixture schema after resets, and `current_user` audits writer identity; `application_name` cannot provide that audit through transaction pooling.

| Path, same M4/rate | Completed / durable | Claim p50 / p99 ms | Run p99 s | PG / proxy / PgB CPU | Correctness |
|---|---:|---:|---:|---:|---|
| Direct | 1479/1479 | 23 / 40 | 4.938 | 86.5 / 44.9 / 0.0 | PASS |
| Transaction pool | 1433/1433 | 334 / 4322 | 9.731 | 166.3 / 75.8 / 33.0 | PASS |
| Transaction pool + kill | 1461/1461 | 458 / 4901 | 10.748 | 142.8 / 66.5 / 28.4 | PASS |

Transaction-pool kill: zero failed/exited survivors, zero common violations, all 22 orphans reclaimed within 5.616 s ≤7 s. The loaded application pools reach 16 at p99; backend connection maxima were 56 direct and 72/73 pooled, so this fixture did not demonstrate a connection-count saving. Prepared-cache disabling and pool pressure also contribute overhead; do not assign the entire dispatch penalty to polling.

The known notification gap is reproduced by [repro_pgbouncer_listen.py](../../../scripts/repro_pgbouncer_listen.py): with listener A held, client B uses/releases the same transaction-pooled backend; notifications received are pooled **0** versus direct **1**, without query errors. The isolated proof forces one backend and uses matching controls. Worker needs a separate direct/session-pooled queue connection to preserve LISTEN while ordinary queries use transaction pooling; the current one-pool interface cannot route those separately. Polling preserves correctness in the measured points.

## Final gates and cleanup

`--quick` PASS: normal 47/47, kill 33/33, outage 24/24, drain 35/35; zero failed/common violations, maximum kill reclaim 6.035 s across two orphans, zero post-drain claims. Run under the shared lock with `--pool-size 16 --poll-interval 1 --drain-timeout 45`; its warmup/steady windows are 2/15 s.

**2,450 tests passed, 18 skipped** with the specified socket DSN; Ruff check/format (434 files) and Pyright (explicit venv Python path, zero errors/warnings) pass. Final static checks include the standalone reproduction. Across 34 full/quick/comparison points, **49,840 durable runs completed**, with zero failed runs or note conflicts. No new SDK defect was found; the transaction-pooling notification limitation above remains intentionally unfixed.

Teardown audit: zero experiment schemas on both Postgres servers, temporary roles, app connections or experiment processes; system PgBouncer PID remains untouched. Raw JSON/logs stay in `/tmp/wp18-scale-gate-20261031`; **no generated JSON is committed**. The baseline report receives only a pointer here; production guidance uses these current measurements.
