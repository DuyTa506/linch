# Durable area: expectations (written before any measurement)

Stack tip f69d97b2 (PRs #160..#167). Release binary for numbers, debug for correctness.
Database `py_fix_b`. Provider: vLLM `qwen3.6-35b-a3b` through my own counting proxy in front of the e2e proxy
(`:18201`), so each test counts its own provider calls (the shared `vllm-proxy.log` has five agents in it).
Sources: `usage/runs.md` ("What is durable, and when", "Resume"), `usage/server.md` (Liveness), `usage/workflows.md`,
`usage/scheduler.md` (Durability), `architecture/stores.md` (Leases, Group-commit write counts), PR bodies #136 #143 #160 #162 #165.

Lease TTL: the documented default is 30 s (`stores.lease_ttl_ms`); the live suites use 2 s. I state the TTL beside every number.
Renewal is every TTL/3 on a timer; so at a kill the remaining lease is between 2/3 TTL and 1 TTL.

## 1. Crash and resume (kill -9 of my own server pid, a new server, `run.resume`)

| id | feature | expected observable |
|---|---|---|
| 1.1 | kill before the first provider call (client `before_provider_call` callback kills it) | the proxy saw 0 calls before the kill; after resume the run ends `success`; total proxy calls for the run = turns needed (no extra); events: seq strictly increasing, exactly one `result`, last |
| 1.2 | kill during a provider call (my proxy holds the request, then the kill) | the killed call is the only one repeated: proxy calls = (turns) + 1; one `assistant` and one `usage` per turn; no `usage` for the killed call (no receipt) |
| 1.3 | kill between a `tool_call_end` and the next provider call (`before_provider_call` callback of turn 2) | calls for turn 1 are NOT repeated: proxy calls = turns exactly; the tool ran once (a `tool_call_start` once per `tool_use_id`) |
| 1.4 | kill after the last turn's response, before the terminal event (`stop` callback) | the final model call is not repeated if its assistant event is durable (runs.md: "After each model response: the assistant message, usage and cost"); proxy calls = turns; one `result` |
| 1.5 | paid receipt | sum of `usage` events' tokens = `result.total_usage` (no double count after resume); for calls that completed, the proxy's `prompt_tokens`/`completion_tokens` equal the event usage |
| 1.6 | no duplicates | `seq` strictly increasing; no two `assistant` events for one turn; `tool_call_start` unique per `tool_use_id` |

At each point I classify where the kill actually landed from the durable events (delivery / receipt / assistant present for that turn).

## 2. Lease and fencing

| id | expected observable |
|---|---|
| 2.1 | while server A drives a run, `run.resume` on server B ends `RunLeased` naming the holder with `expires_in` <= TTL |
| 2.2 | after A is killed, B's resume succeeds once the lease ran out: takeover latency (kill to B's first successful claim) in (0, TTL + 1 s], mean about 5/6 TTL (the lease was renewed within the last TTL/3). Measured with TTL 3 s (5 reps) and the 30 s default (1 rep) |
| 2.3 | A frozen with SIGSTOP past the TTL, B resumes and finishes the run, A is SIGCONT-ed: A's writes are refused (`LeaseLost`, epoch fence); the event list after A wakes equals the list B left (no fork); one `result` |

## 3. Approvals across restarts

| id | expected observable |
|---|---|
| 3.1 | a run suspended on an approval, server killed, another server: `run.approve` completes it; the tool ran once, after the approval |
| 3.2 | `agent.cancel` of a suspended run (also after a server restart): the run ends terminal (`aborted`-style result/cancelled), a later approve is `NotSuspended`/`RunTerminal`, the session is free |
| 3.3 | #165: a scheduled run suspended on an approval, history pruned (worker `history_retention_ms` tiny, prune on its first poll): the stored definition (`linch_worker_agents`) and the history row survive and `approve` completes the run |

## 4. Workflows (#160 #162 #165): 3 stages = fan-out of 2, a step, a typed handoff through `output_schema`

| id | expected observable |
|---|---|
| 4.1 | kill the CLIENT process between stages; rerun with the same `run_id`: finished calls replay (`agent_replayed`/`step_replayed`) with ZERO provider calls for them; only the unfinished stages call the model |
| 4.2 | kill the SERVER mid-way; new server, same `run_id`: replay finished calls with ZERO provider calls; the in-flight call continues its child run (at most that call repeated) |
| 4.3 | an edited prompt re-runs only its own call |
| 4.4 | an interrupt answered after a server restart: the workflow completes with the answer; the answer is journaled and not asked again |
| 4.5 | an interrupt whose payload (draft) changed between question and resume: `interrupt_stale` event, the stale answer is dropped, status stays `awaiting_input` |
| 4.6 | two clients on one `run_id`: the second gets `WorkflowHeld` (-32045), the first untouched |
| 4.7 | a workflow started with a `principal` refuses resume with another principal or none: `NotAllowed` `kind=workflow_principal` |
| 4.8 | `run_or_resume` on a not-stored id starts it; on a finished one returns the stored `ResultEvent` with 0 provider calls; on a stored-and-running (other server) one waits for the lease then continues |
| 4.9 | a budget with `max_cost_usd` on an agent whose model has no price is refused at `workflow.start` (-32011) |

## 5. Scheduler and worker

| id | expected observable |
|---|---|
| 5.1 | an interval schedule on a server with a worker; kill -9 of the server across a fire; each fire runs exactly once: runs started per `schedule:<id>:<fire time>` job key = 1, `fires` counter = number of distinct jobs |
| 5.2 | server stopped for 3 minutes with a 60 s schedule: after the restart ONE fire, `skips` grows by about (missed ticks - 1) (+-1) and `next_fire` is the first tick after now |
| 5.3 | a stored agent with no `max_turns` and no budget for a tenant (stored by a stdio client): its fire ends `failed`/error naming both settings (`max_turns` and a budget), visible in `job.list`; the worker goes on with other jobs |
| 5.4 | job retry and give-up are logged on server stderr at `LINCH_LOG=warn` (tracing subscriber, #165): a warn line per failed attempt; give-up after 5 failures |

## 6. Connection liveness (WebSocket, short `liveness`)

| id | expected observable |
|---|---|
| 6.1 | a raw WebSocket peer that never reads/answers (SIGSTOPed client) is dropped within about `pong_timeout_ms` (+ one ping interval) and the tenant's `max_connections` slot is free (a new login that was 429 before now gets 101) |
| 6.2 | a client that answers pings is not dropped (stays past 3 x `pong_timeout_ms`) |
| 6.3 | a Python client whose loop blocks with `time.sleep` longer than `pong_timeout_ms` is cut (next call `ConnectionClosed`); one using `asyncio.to_thread(time.sleep)` is not |

## 7. Group commit (Postgres `xact_commit` delta)

`architecture/stores.md`: a tool turn with k started tools costs 3 + 2k run writes and 2 session writes; k=1 -> 7 commits;
k=2 -> 9. A text-only final turn: pending flush + write-through + 1 session write + terminal write (about 3 to 4).
Expectation on the marginal cost (difference between runs of N and N+1 scripted tool turns, no worker, long TTL, idle database):
**+7 commits per k=1 tool turn** (+-1 for what else commits: lease renewals every TTL/3 are separate and measured in an idle control).
The absolute total of a run is larger (claim, create, final writes); I report it and what else commits (per-table inserts/updates).

## Measurements (release binary, quiet moment, `uptime` noted, 3+ repeats)

* resume latency: server start to the first resumed event, p50 of 5, with lease already free (clean stop) and with a killed server's lease wait separated. Expected: a few tens of ms without the wait.
* takeover: see 2.2.
* throughput: 20 scripted trivial runs at 1, 4, 16 concurrent: report runs/s. No expectation beyond "scales with concurrency until the pool or the cores limit".
* Postgres write latency per turn: the same scripted run on memory stores vs Postgres; expectation: Postgres adds about (writes per turn) x (one commit latency), a few ms per turn on loopback.
