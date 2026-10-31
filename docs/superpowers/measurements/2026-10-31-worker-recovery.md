# Worker initialization and database recovery

Baseline: `1208d1f`, after the requested fast-forward merge of integration into
`scale/hot-path`. The merge added the load harness, reproductions, report/spec,
psutil metadata and harness tests; it changed no SDK sources beyond `2ebbc1f`.

## Initial adoption

Commit `e8d9875` adds `initial_checkpoint` to RunStore.create_run. SDK creation
and pending-row adoption store `started` in the same INSERT/UPDATE as running
status, with existing contract/session checks and lease fencing. A committed
SDK running row is therefore resumable even if its creator dies before startup.
Direct manual store calls may omit the checkpoint and remain non-resumable.

The initial checkpoint captures pre-AgentStart hook state and a private history
count/fingerprint. Started recovery distinguishes an already committed prompt
from an identical pre-existing tail using the baseline and tail equality.
Startup-affecting hooks require a post-hook preparation checkpoint that freezes
the exact message, post-hook baseline and remaining hook-event plan/cursor. This
also distinguishes hook context identical to the prompt. No-hook runs retain
their write count. Preparation is replayable until that boundary; afterward its
frozen output resumes without regenerating already committed events.

Startup journal writes commit pending events with their current checkpoint and
prefix progress, preserving the pre-hook state until preparation completes.
Hook events retain their ordering around accounted EventEmit callbacks. The
temporary marker is removed with the prompt checkpoint. Ordinary startup
prefixes commit with their owning checkpoint; existing observe-only
AgentStart-per-resume behavior remains. The error-callback limit is below.

Worker factory/setup failures fold the existing model-call journal and commit
ErrorEvent, final ResultEvent, checkpoint totals and failed status atomically.
Connection loss, cancellation and lost leases remain resumable rather than
being synthesized into a terminal configuration failure.

TDD: missing initial-checkpoint keyword, committed-adoption takeover, empty
factory-failure events, stale receipt totals, unsafe startup prefixes, hook
state/dependency context, premature skills flags and initial-byte accounting
were observed RED before their fixes. The final dynamic matrix covers 162
ordinary/context injections across Memory/Postgres, basic/exact-input,
identical-tail and distinct/identical hook-context variants: before call,
committed reply loss and after return, through first provider_pending. It checks
full event payload/order/count except process-local PromptCacheAdvisoryEvent,
contiguous seqs, prompt and terminal once, histories, provider once, increasing
epoch and stale-owner fencing. Eight additional completed-receipt injections
check accounted prompt hooks and HookEvent/telemetry receipt interleaving.
Interrupted billable dispatch retains its normal incomplete-usage audit; those
records are never filtered to claim baseline equality. Final startup coverage:
27 passed; the final focused correction group passed 160 tests.

## Reconnection

Installed PgQueuer 1.6.0 PoolDriver.__aexit__ resets an already released LISTEN
proxy before releasing/clearing it. [PyPI](https://pypi.org/project/pgqueuer/)
and the [latest release](https://github.com/janbjorge/pgqueuer/releases/latest)
still identify 1.6.0; unreleased main is not a clean dependency upgrade.
The existing CLI supervisor uses fixed delay and broad exception retry, so a
small Worker supervisor retains the required narrow retry and drain behavior.

Each cycle uses the maintained Queries.from_asyncpg_connection / AsyncpgDriver
with one borrowed queue-control connection, a fresh manager and fresh LISTEN.
The driver serializes queue SQL and notification operations. SDK job writes
continue through the retained pool/store pair. A dead cycle cannot flush held
FAILED queue status on a newly acquired pool connection after recovery.

Worker-owned stop state survives replacement; local jitter ceilings grow from
0.1 to a capped 2 seconds. Startup/acquisition attempts have two-second bounds.
Configuration errors escape; connection failures retry, including connection
initialization FATAL 55000 but not ordinary statement-level 55000. Manager and
callback drain finish before connection close/release. Repeated cancellation
preserves host cancellation, and the minimum two-connection pool guard remains.

TDD reproduced the detached proxy and failed old-cycle retry writes before the
fix. Real SQL tests create their own throwaway database, disable connections,
terminate only that database's backends for at least three seconds, restore it
and clean up. They prove live worker recovery, new manager/backend PID, actual
UUID-specific NOTIFY health checks before/after, terminal/result invariants,
post-recovery dispatch, stop during/after outage and host-pool ownership.
Worker tests: 68 passed; 19 outage-file tests passed after the typing correction.

## End-to-end reproductions

Both supplied scripts ran from this branch against the dedicated Postgres at
127.0.0.1:55432, using their internal bounded shared CPU lock and Toxiproxy.
Arrival counts are unseeded Poisson samples; no count was forced to match the
earlier 170-run sample. A separate ordinary kill point used the same
M4/R10/RTT1 settings as the initialization reproduction.

| Point | Completed/total | Failed | Unexpected worker exits | Reclaim seconds |
| --- | ---: | ---: | ---: | ---: |
| Original initialization selector | 163/163 | 0 | 0 | not injected |
| Initialization at durable started (first run) | 168/168 | 0 | 0 | 5.985 |
| Initialization at durable started (final tree) | 146/146 | 0 | 0 | 5.986 |
| Three-second DB blip (final tree) | 23/23 | 0 | 0 | 4.997 |
| Supplementary actual worker kill | 156/156 | 0 | 0 | 5.172 |

The original cold selector requires running plus checkpoint NULL. Atomic
adoption deliberately makes that SDK state unreachable. Its CLI exits 1 with
chaos/recovery flags because it finds no victim; completion alone is **not** a
cold-chaos pass. After a focused RED/GREEN regression, only its victim selector
was changed to the new durable started boundary (plus its docstring); no phases,
timings or acceptance checks were broadened. That rerun injected SIGKILL and
recovered epoch 1 to 2 with no later claims by the killed owner. Its full oracle
passes. The expanded startup crash matrix adds boundary recovery evidence.

The started, DB-blip and ordinary-kill points have zero sequence, terminal-count,
failed-without-error, orphan, writer-mismatch, duplicate-note or other invariant
errors. The final blip has nine producer ConnectionRefused errors before enqueue
became durable, zero worker errors and zero renewal failures; every durable run
completes. Its worker tasks remain alive and all jobs drain.

Six seconds is stale eligibility from the last queue heartbeat, not a strict
kill-to-claim upper bound. Heartbeats occur every three seconds, the stale
predicate is strict, and polling (0.2 s), SQL/network, dispatch and lease claim
follow. Injection precedes the OS kill; recovery is timed by the later DB claim.
This explains possible overshoot of the historical 6.4 s observation without
claiming to apportion its exact overhead or fix an SLA.

## Size and gates

| Item | SDK net | Test net |
| --- | ---: | ---: |
| Initial checkpoint, startup recovery and atomic failure | +118 | +900 |
| Reconnect/drain supervisor | +69 | +530 |
| Initialization probe contract regression | 0 | +47 |

SDK total: +187 versus `1208d1f`, +463 versus `47c17d3`. The initial implementation
was committed as `e8d9875`, the supervisor as `889df09`; the final startup
correction adds 52 SDK lines to the first item's original 66.

The second item is nine net lines above the approximate 60-line target; narrow
error handling, cancellation-safe drain and restored capacity validation are
included rather than compressed away. No module-global mutable state, durable
write cache, vendor driver patch or top-level export was added.

Final-tree gates: **2,319 passed, 18 skipped** with the Postgres DSN; Ruff
check and format pass (427 files); Pyright with the required Python interpreter
reports **0 errors, 0 warnings**. The normalized success trace matches the prior
worker-store baseline. Task and final scoped reviews found no remaining blocker
within the documented hook contract.

## Decisions and limits

- Keep the initial history count/fingerprint: tail equality alone loses a prompt
  when a prior message is identical. The temporary marker adds startup state.
- The preparation boundary and remaining event cursor add I/O only for actual
  startup handlers; they preserve context/prompt provenance and event ordering.
  Mid-hook accounting preserves the initial hook snapshot until preparation.
- Retarget the obsolete NULL-checkpoint probe, retaining its original kill,
  reclaim and event assertions. Reverting that selector removes real cold-target
  coverage under the new invariant; the earlier non-injected run is explicit.
- Keep cancellation-safe drain and narrow error handling despite nine lines over
  the approximate supervisor budget. The new correctness work increases the old
  optimization baseline; the total above is not presented as still under 300.
- Observe-only lifecycle/EventEmit callbacks are telemetry seams. Model-visible
  mutation there, arbitrary non-repeatable external effects and interrupted
  billable completion do not acquire an exactly-once guarantee from these tests.
- Deferred, source-derived and not reproduced: an accounted EventEmit callback
  on a blocked ErrorEvent can flush that error before the failed terminal and
  permit duplicate error replay. This predates this correction and is separate
  from the worker factory/setup terminal fixed here; it needs an owner follow-up.
