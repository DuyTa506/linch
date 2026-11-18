# Durable area: results (stack tip f69d97b2, binaries in e2e/bin; database py_fix_b; branch live/e2e-durable)

Expectations: `expectations.md` (committed first as fd916beb). Raw data: `crash_*.json` (3 reps each), `lease_*.json`,
`approve_*.json`, `cancel_suspended.json`, `wf_*.json`, `liveness_*.json`, `sched_*.json`, `perf_*.json`.
Scripts: `linch-rust/python/tests/live/durable_e2e/` (harness `h.py`: my own counting proxy in front of the e2e proxy, so
provider calls are counted per test). Correctness runs: debug binary. Numbers: release binary, load average 2.0-2.8 (64 cores).

## Table

| id | feature | expected | observed | verdict | evidence |
|---|---|---|---|---|---|
| 1.1 | kill before the 1st provider call | 0 calls before the kill; no repeat | 0 calls at the kill, 2 total for 2 turns, tool ran once after resume | MATCH | crash_before_first{,_r2,_r3}.json (durable at kill: init,user) |
| 1.2 | kill during a provider call (held at proxy) | only the in-flight call repeated | 3 calls for 2 turns (1 dropped + 2); 1 assistant + 1 usage per turn | MATCH | crash_during_call*.json |
| 1.2b | kill mid-stream (3 partial events) | same | 2 calls for 1 turn; usage in events = the completed call only | MATCH | crash_mid_stream*.json |
| 1.3 | kill between tool end and the next call | 0 repeated calls, tool not rerun | 2 calls for 2 turns; tool 1x (before kill), 0x after resume | MATCH | crash_after_tool_end*.json |
| 1.4 | kill after the last response, before the terminal event (stop hook) | 0 repeated calls | 2 calls for 2 turns; assistant+usage of turn 2 durable at the kill | MATCH | crash_before_terminal*.json |
| 1.5 | paid receipt (kill inside an after_provider_call hook) | usage of the paid call kept, counted once, model called again | 3 calls (1 paid then killed + 2); 3 usage events; sum(usage events) = result.total_usage = proxy tokens (960/56) | MATCH | crash_in_after_hook*.json |
| 1.6 | no duplicates | seq strictly increasing, one result and last, unique tool starts, sum(usage)=total_usage | true in all 18 runs (6 points x 3 reps); proxy tokens of completed calls = event usage in all | MATCH | `summ_crash.py` output |
| 2.1a | second server refused while the first holds the run | `RunLeased` with expires_in <= TTL | `RunLeased`, expires_in 2.0-2.99 s (TTL 3 s), 29.6 s (TTL 30 s), 6 of 6 | MATCH | lease_takeover_*.json |
| 2.1b | the refusal names the holder | the holder identified | `.owner` is `"linch-rpc"` for both servers (the default `RunOptions::owner` is the same in every process); the holder identity (`lease_holder`, a UUID per store instance) is only in `linch_runs` | MISMATCH, minor (usability/doc: runs.md says `.owner` says "whose lease"; stores.md explains the holder is per store instance, not the owner name). Product behaviour as designed, not pinned by a test | lease_takeover_*.json, lease_fence_3000.json (`lease_holder` before/after) |
| 2.2 | takeover after kill -9 in (0, TTL+1 s] | | TTL 3 s: 2.79 2.40 3.02 2.96 2.34 s (mean 2.70); TTL 30 s: 28.6 s | MATCH | lease_takeover_3000.json, _30000.json |
| 2.3 | SIGSTOP past TTL, SIGCONT: stale writer refused, no fork | | epoch 1 -> 2, B completed; A's run ended `LeaseLost (epoch 1) -32022`; events after A woke == events B left; one result | MATCH | lease_fence_3000.json |
| 3.1 | approve after kill of the server | completes, tool once after approval | suspended, kill, new server: approve 0.54 s, success, tool 1x, 2 provider calls total, second approve `RunTerminal` | MATCH | approve_after_kill.json |
| 3.2 | cancel of a suspended run, also after restart | terminal, approve refused, session free | cancel True (25 ms / 93 ms after restart), result `aborted`, approve `RunTerminal`, 2nd cancel False, new run in the session success | MATCH | cancel_suspended.json |
| 3.3 | #165 pruned approve of a queued run (a `job.enqueue` run, not a schedule fire: no schedule row keeps the definition alive, the stronger case) | definition and history kept while suspended | `linch_worker_agents` 1 and `linch_worker_history` 1 survive the worker's prune (retention 1 ms); approve success, file written; after the run ended the next prune removed both (0/0) | MATCH (approve went through the client; the worker-queue `enqueue_approve` is crate-only and not reachable from RPC) | approve_pruned.json |
| 4.1 | kill the client between stages | finished calls replay with 0 provider calls | killed after stage 1 (2 calls): resume replayed 2 agents, ran only stage 3 (1 call); killed after stage 2: replayed 2 agents + step, 1 call; finished rerun: 0 calls, 3 agent + 1 step replays | MATCH | wf_a.json |
| 4.2 | kill the server mid-way | replay, at most the in-flight call repeated | killed at stage 3's call (3 calls arrived, 1 dropped); new server: 2 agent + 1 step replays, stage 3 continued its child run (1 call, 3.4 s incl. lease wait); repeated calls = 1 (the in-flight one) | MATCH | wf_b.json |
| 4.3 | edited prompt re-runs only its call | | 1 provider call; 2 agent replays + 1 step replay | MATCH | wf_a.json edited_prompt |
| 4.4 | interrupt answered after a server restart | completes, answer journaled | parked, kill, new server, `resume={"approve": True}` -> `signed v1` with 0 calls; again: `interrupt_replayed` | MATCH | wf_d.json |
| 4.5 | changed draft discards the stale answer | `interrupt_stale`, stays awaiting_input | `interrupt_stale` + `interrupt_requested` (payload v2), end `suspended`; answer for v2 completes | MATCH | wf_d.json |
| 4.6 | two clients one run_id | second `WorkflowHeld` -32045, first untouched | second: `WorkflowHeld` after 0.24 s; first finished `rested`; third (after finish) replayed | MATCH | wf_e.json |
| 4.7 | principal | other principal / none refused | bob and none: `NotAllowed kind=workflow_principal`; roles reordered ok; fewer roles refused; start without then with: refused | MATCH | wf_f.json |
| 4.8 | `run_or_resume` | not stored starts, finished 0 calls, running waits then continues | not stored: 1 call; finished: 0 calls, same result; driven by another server with wait=1: `RunLeased` after 1.2 s; after the holder was killed: waited 2.8 s, continued with 1 call, success | MATCH | wf_g.json |
| 4.9 | cost cap on an unpriced agent | refused at `workflow.start` (-32011) | `OpenError -32011`: "needs a price for the model qwen3.6-35b-a3b ... set pricing or cap max_tokens"; max_tokens budget accepted | MATCH (the -32046 `unpriced` mid-call path: NOT RUN, needs a model that is priced for admission and unpriced in a response) | wf_h.json |
| 5.1 | each fire runs once across kill -9 | runs = fires | 3 kills with a job running in a 75 s window of a 4 s schedule: 17 fires = 17 history rows = 17 distinct fire_at = 17 runs (all `completed`) = 17 provider calls; 3 jobs had attempts > 1; 0 duplicate assistants | MATCH | sched_s1.json (the kills landed outside an in-flight stream: 17 calls all `done`, so no call was repeated here; the in-flight case is in 1.2/4.2) |
| 5.2 | missed fires coalesce | one fire, skips counts the rest | 60 s heartbeat, server down 190.2 s (3 ticks): after restart fires 1 -> 2, skips 0 -> 2, 1 run started, next_fire = first tick after now | MATCH | sched_s2.json |
| 5.3 | tenant agent without a bound fails its fire with the text | text in `job.list` | 3 fires failed, error "a tenant agent needs a bound on its runs: set spec.max_turns or a spec.budget cap (max_tokens or max_cost_usd) ..." in `jobs()`; the other job ran (1 call) and finished `done`; the schedule keeps firing (not paused) | MATCH | sched_s3.json |
| 5.4 | retry and give-up logged on stderr | warn lines | 4 `WARN linch_worker::settle: retrying a job ... failures=1..4 delay=200ms,400ms,800ms,1.6s` and 1 `WARN ... failing a job ... gave up after 5 failed attempts`; gave up in 5.2 s | MATCH (cosmetic: the failed job row says attempts 5, failures 4) | sched_s4.json |
| 6.1 | silent WS peer dropped within about pong_timeout (+ one ping interval), slot freed | pong_timeout (+ ping_interval) | short config (ping 500 ms, pong_timeout 2000 ms): raw silent peer freed at 2.06-2.07 s (3 reps); SIGSTOP client at 2.57-2.58 s; default config (one run each): raw peer 60.01 s, SIGSTOP client 89.0 s | MATCH (inside the pre-registered bound timeout + one interval). Doc-precision note: server.md says "60 s of silence" / "about a minute"; the code (`http.rs` `ping()`) checks `silence >= pong_timeout` only at ping ticks, the first tick fires at connect, the client's pong lands just after a tick, so the silence at the tick `timeout` later is a few ms short and the drop comes one tick later: 90 s for a client that answered the first ping then froze, 60 s for a peer that never answered | liveness_*.json |
| 6.2 | answering client not dropped | | held 6.5 s (3.25x the 2 s timeout): slot still taken, next call success | MATCH | liveness_short_blocked.json |
| 6.3 | blocked loop cut, `to_thread` not | | `time.sleep(3.5)` (> 2 s): `ConnectionClosed`; `asyncio.to_thread(sleep 3.5)`: ok; `time.sleep(1.0)`: ok | MATCH | liveness_short_blocked.json |
| 6.4 | kernel path (TCP keepalive, user timeout of a peer that is gone) | | NOT RUN: needs a peer whose kernel stops answering (packet drop, needs root) | NOT RUN | |
| 6.5 | keepalive config units | each field "at least 1" (ms) | sub-second `keepalive_idle_ms` or `keepalive_interval_ms` (500) -> `setsockopt` EINVAL: stderr "cannot set TCP keepalive on a connection: Invalid argument (os error 22)" once per connection, no keepalive on it; whole seconds (1000, 1500): no warning | MISMATCH (product defect, minor) | liveness_keepalive_units.json; RED `test_sub_second_keepalive_times_do_not_leave_connections_without_keepalive` |
| 7 | group commit (the brief's run: 1 tool call, 2 model calls = 23 commits over the 29 of open+close) | 7 commits per k=1 tool turn (3+2k run writes, 2 session writes) | marginal +7 per added tool turn (7,7,7, 3 reps all equal); a turn with 2 started tools: +9 (7 + 2); per added turn: linch_runs +5 updates, linch_sessions +2 updates, session_entries +2, run_events +8 inserts | MATCH | perf_commits.json |

## Measurements (release binary)

| measurement | value | load | notes |
|---|---|---|---|
| resume latency, server start to first resumed event (lease already expired, TTL 1 s) | p50 59 ms (48-64), of which spawn+open_agent 31-36 ms and open_agent to first event 16-29 ms; to result 76 ms | 2.57 | 5 samples, scripted provider, kill in `before_provider_call` |
| takeover after kill -9 | TTL 3 s: 2.34-3.02 s (mean 2.70); TTL 30 s default: 28.6 s | 2.5-2.8 | bounded by TTL (+15 ms polling), as documented |
| run throughput, 20 trivial scripted runs, Postgres | 1 / 4 / 16 concurrent: 56 / 180 / 258 runs/s (medians of 3; ranges 52-64, 168-187, 252-335) | 2.4-2.5 | 64 cores; a 20-run batch takes 60-380 ms, so noisy |
| same on memory stores | 4568 / 6713 / 7516 runs/s | 2.5 | |
| Postgres write cost per turn | one 4-call run (3 tool turns): 60.3 ms median on Postgres vs 0.8 ms memory: +59.5 ms per run = +14.9 ms per turn | 2.71 | 50 runs each; about 37 commits per run (16 + 3 x 7 = 37 from the commit counts), so about 1.6 ms per commit on loopback |
| commits | open agent + close, no run: 29; text-only run: +16; each k=1 tool turn: +7; second tool in a turn: +2 | 2.0 | counted after server exit (stats flush), see below |

Group-commit method: `pg_stat_database.xact_commit` read after the server process exited (Postgres flushes a backend's counters at exit; a live
idle backend only within about 10 s, so a read 2.5 s after a run is wrong: my first version read a constant 29 whatever the run length, and I
discarded it). Whole-lifetime counts of runs of different length are differenced, so the fixed cost (migrations check, claims, close) cancels.
What else commits: `xact_commit` counts read-only transactions too, so part of the text-only run's +16 (run create, claim, session create, the
terminal write: per-table stats show run_events +8, session_entries +3, linch_runs 1 insert and 4 updates, linch_sessions 1 insert and 2 updates) is reads.
The 29 of open+close is the full migration set of a fresh schema plus the pool's connects, not a "check" (a reused schema would cost less).
Lease renewals (every TTL/3 = 20 s at the 60 s TTL used here) did not occur inside the runs. A scripted run repeating the same tool input is ended by the loop guard
(my first 3-turn run ended `error` for that reason; distinct inputs were used afterwards).

## Defects (RED tests on live/e2e-durable)

1. `test_sub_second_keepalive_times_do_not_leave_connections_without_keepalive` (test_live_durable_liveness.py): a sub-second keepalive time is
   accepted by the config ("at least 1") and then fails with EINVAL on every connection:
   `AssertionError: linch-server: cannot set TCP keepalive on a connection: Invalid argument (os error 22)`.
   Its control, `test_whole_second_keepalive_times_are_set_without_a_warning`, passes.

Observations that are not defects: frozen-client drop at timeout + one ping interval (6.1; I wrote a RED test for the tighter "within the
timeout" reading and deleted it, because the pre-registered expectation allows the extra interval), `RunLeased.owner` constant (2.1b), the failed
job row shows `failures` 4 for 5 attempts, a scripted run that repeats one tool input is ended by the loop guard.

Sample sizes: crash points 3 reps each; takeover at TTL 3 s 5 reps, at the 30 s default 1; fencing, each workflow scenario (4.1-4.9), the pruned
approve, 5.2 (the 190 s coalescing), 5.3, 5.4 and the default-liveness (60 s) and cancel runs were each run once; the two 3-rep crash/commit
series and the 5-rep resume are the only multi-rep numbers besides those listed. Model behaviour never decided a verdict (every run ended as the
prompts asked), so no rerun was needed.

## Not run
- 4.9 `-32046 unpriced` mid-call path; 6.4 kernel TCP keepalive and user timeout (no packet-drop tool without root); the worker-queue `enqueue_approve`
  after a prune is crate-only (not reachable over RPC), so 3.3 checks the kept rows plus the client approve.
- Memory-store workflow `WorkflowNoJournal` (not in this brief's list).
