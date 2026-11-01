# Worker initialization and database recovery

Historical fleet measurements below were taken at commit `b703246`.
They were not rerun for the r8 blocked-error correction on baseline `954c4ef`.
Arrival counts are unseeded Poisson samples, not a forced target count.
Both used RTT 1 ms, `/tmp/wp8-cpu.lock` and Toxiproxy; M is workers, R is offered runs/s.

## Final reproduction aggregates

| Reproduction | Configuration | Completed/total | Reclaim |
| --- | --- | ---: | ---: |
| Initialization SIGKILL at durable `started` | M4/R10, RTT1 | 146/146 | 5.986 s |
| Three-second database blip | M2/R2, RTT1 | 23/23 | 4.997 s |

Both points had zero failed runs, unexpected worker exits and invariant errors:
sequence gaps, terminal-count errors, failed-without-error, orphans,
writer mismatches and duplicate notes were all zero. Every durable job drained.
The initialization victim advanced lease epoch 1 to 2; its old writer was fenced.
The initialization selector targets the durable started checkpoint; the obsolete
running/NULL-checkpoint state cannot occur after atomic SDK adoption.

## Crash coverage and correction

The r6 Memory/Postgres matrix exercised 162 ordinary/context crash injections,
plus eight complete-receipt injections for prompt and EventEmit accounting.
It checks full normalized durable event payload/order, histories, sequence
continuity, prompt/result once, lease epochs and stale-owner fencing.
Only process-local PromptCacheAdvisoryEvent is excluded from trace comparison.
The r8 regression adds four complete blocked-Error callback receipt injections:
committed-before-return and returned boundaries, each in Memory and Postgres.
The duplicate Error replay was reproduced RED and is fixed by checkpoint-owned
emission progress set before yield. Result and AgentStop totals refresh after
its optional paid callback; receipt usage, cost and completeness stay truthful.
The scoped SDK correction is +9/-5 source lines (+4 net), below the ~30-line limit.
No fleet measurements or new full-gate totals are claimed for that correction.

## Decisions and costs

- Atomic initial checkpoint removes the SDK running/NULL gap; the private
  history baseline distinguishes an existing identical tail from this run's prompt.
- Actual startup handlers add a preparation checkpoint; hook-free startup adds
  no checkpoint. Frozen message/event plans preserve hook-context provenance.
- Startup journal writes preserve pre-hook state until preparation completes;
  individual prefix flags and the event cursor retain accounting interleaving.
- Error emission progress shares existing atomic writes; no new I/O, public
  protocol, event taxonomy or steady-state success behavior is introduced.
- Worker-owned schema/store/pool reuse avoids per-job setup and preserves leases.
- Fresh-manager reconnect cycles use the maintained direct driver and drain
  callbacks before closing the old connection. The supervisor is +69 net lines
  versus its approximate 60-line target; narrow retries and safe drain justify it.

## Residual risks

Startup hooks must be repeatable until their preparation checkpoint. Arbitrary
external effects do not gain exactly-once delivery. Observe-only lifecycle and
EventEmit callbacks that mutate model-visible history remain outside the contract.
Interrupted billable dispatch retains incomplete/unknown usage audit facts;
complete-receipt equality tests neither erase those facts nor fabricate receipts.
Ordinary in-process ambiguous commit/retry and transient terminal fallback are
not generalized exactly-once guarantees. Reclaim timing is an observation, not an SLA.
