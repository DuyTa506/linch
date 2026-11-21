# Benchmarks

[Tiếng Việt](./vi/benchmarks.md)

What linch-rust costs compared with Python Linch, how it behaves when it is killed or
loaded, and what has not been measured. Every number names the run it came from. The
runs used a shared 64-thread host that also ran other work, so read absolute values as
"on a loaded host" and compare cells that ran in the same run.

## Setup

| | |
|---|---|
| Host | 2 x Intel Xeon Silver 4514Y (64 hardware threads), 251 GB RAM, Ubuntu 24.04.4, Linux 7.0.0-34 |
| Postgres | 16.15, loopback, `max_connections` 100, `synchronous_commit on`, `fsync on` |
| Toolchain | rustc 1.99.0, release profile of the repository (`lto = "fat"`, `codegen-units = 1`); Python 3.14.10 for both Python sides |
| Re-measured run ("final") | `origin/master` `617715b3` (includes the run-start durability fix, the shared tool schema, the SGLang fixes), `linch-server` sha256 `3749f7e79789ac69...`, 2026-11-19, 1-minute load average 2.3 to 9.2 across the rounds, 3 rounds interleaved |
| Case 6 re-measured run ("case 6 run") | `origin/master` `1da43ee3` (includes the SSE framing rewrite, #211), release builds of `linch-server` and `bench_driver`, Python sides from the same tree with `linch-native` built from `crates/linch-python`, 2026-11-21, 1-minute load average 2.2 to 2.9 across the rounds, 4 rounds alternating side order, every process pinned to one core (see [Stream decoding](#stream-decoding-case-6-case-6-run-4-rounds-of-20-iterations)) |
| Earlier run ("earlier") | `origin/master` `50eda478`, 2026-11-18, load average 1.9 to 6.9, one pass (3 for the memory cells) |
| Providers | scripted fixtures and loopback servers for every performance and memory number; live providers only in [Scale and live](#scale-and-live) |

Three sides are compared, as in `linch-rust/bench`:

- `py`: Python Linch in one process.
- `rs-client`: the `linch_rust` client over the `linch-server` sidecar on stdio, the path a Python application uses.
- `rs-kernel`: the Rust kernel called directly (the floor, no RPC).

Where a cell is a range, it is `median (minimum to maximum)` over the 3 rounds, and each
round's value is the median of 20 iterations. The commit latency of a regular Postgres table
(50 inserts, p50) was 1.7 to 3.9 ms across the final rounds: it is the floor under every
Postgres cell.

### Reproduce

Cases 3, 6, 7 and 14 are the repository's own harness:
`python bench/run_all.py --out DIR --cases 3,6,7,14 --pg-dsn DSN` with the server binary, the
`bench_driver` example and a Python Linch venv as `linch-rust/bench/README.md` describes. The
memory probe (one `bench_driver` process, case 14 shape, `VmRSS` sampled every 50 ms, with and
without `MALLOC_ARENA_MAX=2`), the kill harness, the fault proxy and the load ramps are not in the
repository. Their methods are described in the sections that use them.

## Performance against Python Linch

### Loop, tools, startup, concurrency (earlier run, `50eda478`, not re-measured)

Scripted provider, no network. Per provider turn or per call, median of 20 iterations, one pass.

| Cell | py | rs-client | rs-kernel |
|---|---|---|---|
| Loop, memory stores, 10 turns x 1 call | 2.76 ms/turn | 0.20 | 0.044 |
| Loop, memory stores, 50 turns x 1 call | 3.49 ms/turn | 0.53 | 0.067 |
| Read / Write / Edit | 4.8 / 4.8 / 5.2 ms/call | 0.39 / 0.20 / 0.26 | 0.20 / 0.17 / 0.17 |
| Grep / Glob | 5.0 / 4.3 ms/call | 0.67 / 0.67 | 0.38 / 0.41 |
| Bash | 17.7 ms/call | 1.19 | 1.03 |
| Grep over 2000 files | 111 ms/call | 12.7 | 12.5 |
| MCP stdio echo | 4.86 ms/call | 0.68 | 0.71 |
| Cold start (import, open, first run, exit) | 425 ms | 184 | 11.7 |
| 16 concurrent sessions of a 10-turn loop | 56 runs/s | 985 | 1986 |

The RPC cost of the sidecar (`rs-client` minus `rs-kernel`) was 0.16 to 0.46 ms per turn.
These rows have short iterations (2 to 27 ms for `rs-client` loops); their spread inside a run
was 39 to 62 percent, so read them as an order of magnitude.

### Postgres loops, case 3 (final run)

Loop with Postgres stores: every turn commits several times, so the cell is dominated by commit
latency and the gap to Python shrinks from about 10x to about 1.2x to 1.6x. Milliseconds per
provider turn, 3 rounds (load 2.3 to 2.7 at the start of each).

| Shape | py | rs-client | rs-kernel |
|---|---|---|---|
| 10 turns x 1 call | 20.6 (19.6 to 24.7) | 15.4 (14.6 to 16.4) | 16.4 (13.8 to 17.8) |
| 10 turns x 3 calls | 31.2 (30.8 to 35.1) | 25.4 (19.2 to 26.6) | 23.1 (19.5 to 27.1) |
| 50 turns x 1 call | 22.2 (21.3 to 23.0) | 16.3 (14.7 to 16.8) | 16.5 (16.3 to 16.8) |
| 1 turn x 8 calls | 48.0 (36.9 to 50.8) | 38.2 (36.1 to 38.7) | 34.5 (34.5 to 36.0) |

Writes per run (tuples, 10x1 / 10x3 / 50x1 / 1x8): py 203 / 283 / 923 / 69, linch-rust 190 /
270 / 870 / 65. Against the earlier run, which had its own repeat at load 2.2 (py 19.3 / 30.3 /
21.6 / 52.4, rs-client 16.2 / 24.9 / 17.0 / 38.4, rs-kernel 16.0 / 23.0 / 18.1 / 34.4), no cell
moved outside the spread.

### Worker queue, case 7 (final run)

100 jobs per iteration on Postgres. The client does not expose the worker, so `rs-client` has no
cell. Jobs per second: py 94 (91 to 115), rs-kernel 271 (232 to 271). The earlier single pass read
117 and 353; both sides read lower now by a similar share (rs-kernel over py: 3.0x earlier, 2.9x now),
which points to the host or Postgres load of the hour (commit p50 1.8 to 3.9 ms across the three rounds), not a
change in either side.

### Concurrent sessions on Postgres, case 14 (final run)

M sessions started at once on one agent, 4 turns each, a 1 s provider delay per turn (ideal run
time 4 s), Postgres stores. Run wall p50 in seconds and turns per second (4 x M over the window).

| M | py | rs-client | rs-kernel | rs-client, 4 sidecars |
|---|---|---|---|---|
| 100 | 4.97 s, 73/s | 4.35 s, 91/s | 4.20 s, 95/s | 4.15 s, 96/s |
| 500 | 13.4 s, 89/s | 5.47 s, 351/s | 4.59 s, 424/s | 4.51 s, 439/s |
| 1000 | 25.0 s, 85/s | 7.46 s, 484/s | 5.34 s, 694/s | 4.83 s, 800/s |
| 2000 | 46.5 s, 89/s | 10.5 s, 686/s | 6.25 s, 1078/s | 5.76 s, 1308/s |

Ranges over the 3 rounds are in `finalbench/RESULTS.md` of the run record; the widest are
`rs-kernel` at M=1000 (5.08 to 13.11 s: one round overlapped with other work on the host, the other two
read 5.1 and 5.3 s) and `rs-client` at M=1000 (6.85 to 8.59 s). Python saturates its event loop at about 85
to 90 turns per second from M=500 up (CPU 20.7 s at M=500, 87.6 s at M=2000); linch-rust keeps scaling.
There were 0 failed runs in every cell of every round.

### Stream decoding, case 6 (case 6 run, 4 rounds of 20 iterations)

A loopback server (`sse_server.py`) replays the same recorded-shape SSE stream per vendor and each side decodes it
through its own provider adapter; one iteration is one text-only run. This section replaces the earlier case 6
tables, which came from builds before the SSE framing rewrite (#211) and are no longer shown.

How it was measured: harness `linch-rust/bench/run_all.py --cases 6` at `1da43ee3`, default 20 timed iterations per
cell (1 warm-up), one process per cell. Each side was run on its own, and the three sides ran back to back in 4 rounds,
alternating the order (py, rs-client, rs-kernel; then rs-kernel, rs-client, py; and so on). The whole harness
(coordinator, replay server, side process and, for `rs-client`, the sidecar) was pinned to one core with `taskset -c 26`,
so the three sides share that core with the replay server. The governor is `powersave` and cannot be changed from user
space, so the core's `scaling_cur_freq` was read every 50 ms during each run. Host load average was 2.2 to 2.9 from
other work, none on the pinned core. Each round's value is the median of 20 iterations; the table gives the median over
the 4 rounds and, in brackets, the lowest and highest round.

| Vendor (events, bytes) | Side | Wall ms | MB/s | events/s |
|---|---|---|---|---|
| DeepSeek (20 004, 4.07 MB) | py | 1260 (1254 to 1270) | 3.2 (3.2 to 3.2) | 15.9 k (15.7 to 16.0) |
| | rs-client | 94.4 (93.8 to 98.8) | 43.1 (41.2 to 43.4) | 211.8 k (202.5 to 213.3) |
| | rs-kernel | 26.1 (25.7 to 26.2) | 156.1 (155.6 to 158.2) | 767.2 k (764.9 to 777.5) |
| OpenAI chat (20 004, 4.03 MB) | py | 1268 (1263 to 1290) | 3.2 (3.1 to 3.2) | 15.8 k (15.5 to 15.8) |
| | rs-client | 94.1 (93.3 to 96.7) | 42.8 (41.7 to 43.2) | 212.6 k (206.9 to 214.3) |
| | rs-kernel | 25.9 (25.8 to 26.5) | 155.4 (152.2 to 156.3) | 771.7 k (755.6 to 776.0) |
| OpenAI responses (20 008, 4.36 MB) | py | 2181 (2172 to 2191) | 2.0 (2.0 to 2.0) | 9.2 k (9.1 to 9.2) |
| | rs-client | 93.9 (93.4 to 95.9) | 46.4 (45.4 to 46.6) | 213.0 k (208.6 to 214.3) |
| | rs-kernel | 24.2 (24.0 to 24.5) | 180.1 (178.0 to 181.2) | 827.6 k (817.8 to 832.5) |
| Anthropic (20 005, 2.49 MB) | py | 287.7 (285.7 to 293.7) | 8.7 (8.5 to 8.7) | 69.5 k (68.1 to 70.0) |
| | rs-client | 90.3 (89.7 to 95.8) | 27.6 (26.0 to 27.8) | 221.5 k (208.7 to 223.1) |
| | rs-kernel | 23.2 (22.8 to 23.9) | 107.1 (104.2 to 109.0) | 860.7 k (837.4 to 876.2) |
| Gemini (20 000, 2.99 MB) | py | 1809 (1796 to 1819) | 1.7 (1.6 to 1.7) | 11.1 k (11.0 to 11.1) |
| | rs-client | 95.3 (94.8 to 100.9) | 31.4 (29.6 to 31.5) | 210.0 k (198.2 to 210.9) |
| | rs-kernel | 27.4 (27.2 to 28.0) | 109.1 (106.6 to 109.9) | 730.2 k (713.0 to 735.3) |

Wall time ratios (median over rounds): `py` over `rs-client` 13.3x (DeepSeek), 13.5x (OpenAI chat), 23.2x (OpenAI
responses), 3.2x (Anthropic) and 19.0x (Gemini); `py` over `rs-kernel` 48.3x, 48.9x, 90.2x, 12.4x and 66.1x;
`rs-client` over `rs-kernel` 3.5x to 3.9x on every vendor. Python's cells match the earlier run's (DeepSeek 1255 to 1266
ms before, 1254 to 1270 ms now; Anthropic 283 ms before, 287.7 ms now).

Frequency and comparability: in every cell of every round the median of the core's samples was 3.40 GHz. The lowest
sample of a cell was 2.52 to 3.38 GHz, and the share of samples below 3.3 GHz was 0 to 9% for `rs-kernel`, 4 to 25% for
`rs-client` and 5 to 16% for `py`, by vendor (`rs-kernel` cells last about 1 s, so they have about 40 samples each;
where in a cell the dips fall was not recorded). Before a side's run the idle core read 0.78 to 2.7 GHz and after it 2.4 to
3.4 GHz. Within one cell, the slowest of its 80 iterations (4 rounds) was at most 1.38x the fastest, so the two-level
split of the earlier runs did not appear. All 15 cells are in the same frequency band and their wall times can be
compared with each other. They cannot be compared with the earlier case 6 numbers: different builds, and the earlier
runs did not pin the harness this way.

What the rows do not show: the replay server shares the pinned core with the side under test, so each wall time
includes the time the server spends producing the stream on that core; how large that share is was not measured.
`rs-client` ran with its client
and sidecar on the same core, not on separate cores.

Kernel cost per event (`rs-kernel` wall over events, median over rounds): DeepSeek 1.30 us, OpenAI chat 1.30 us, OpenAI
responses 1.21 us, Anthropic 1.16 us, Gemini 1.37 us. The earlier run's 1.42x higher per-event time on Anthropic than on
DeepSeek is not present in this run: Anthropic takes 0.89x the DeepSeek time per event. The SSE framing rewrite (#211)
landed between the earlier builds (`d1f3cfe5`, `617715b3`) and this one. No run of one build before and after #211 was
made, so whether and how much of the change comes from it is not isolated here. Python's Anthropic path is faster per event than its OpenAI-SDK paths in this run too (69.5
k against 15.8 to 15.9 k events/s); the earlier profile attributed that to the Anthropic SDK validating one event in one
pydantic-core call against about 10 `construct_type` calls per chunk in the OpenAI SDK, which was not re-profiled. See
[Known gaps](#known-gaps).

## Memory per session

### Peak memory at M concurrent sessions (case 14 shape)

Same cell as above, Postgres stores. Peak resident memory of the side's process tree (client plus
sidecar for `rs-client`), median (range) over 3 rounds, and the same divided by M.

| M | py | rs-client | rs-kernel | earlier run (`50eda478`): py / rs-client / rs-kernel |
|---|---|---|---|---|
| 500 | 159 MB (156 to 160), 0.32 MB/session | 136 MB (135 to 139), 0.27 | 134 MB (128 to 134), 0.27 | 159 / 255 / 277 MB |
| 1000 | 186 MB (182 to 186), 0.19 | 227 MB (224 to 228), 0.23 | 248 MB (239 to 252), 0.25 | 185 / 477 / 526 MB |
| 2000 | 244 MB (239 to 244), 0.12 | 411 MB (406 to 421), 0.21 | 478 MB (463 to 502), 0.24 | 242 / 914 / 1021 MB |

The difference between the two runs on the Rust sides comes from one change: `ToolDefinition.input_schema`
is now an `Arc` shared by the registry, the catalog, the logged prefix and every request, so a session no
longer deep-copies the tool schemas. The heap profile of the earlier code showed about 100 MB of its 178 MB live
heap in those copies, in a process of 574 MB; the rest was allocator retention. Python's cells did not change (185 to 186 MB and
242 to 244 MB), as expected.

At M=2000 the Rust sides now use 1.7x (`rs-client`) and 2.0x (`rs-kernel`) the memory of Python per session
(they used 3.8x and 4.2x). Python's per-session figure falls with M because its baseline (about 42 MB at 0 agents) is spread over
more sessions.

### Allocator and the `MALLOC_ARENA_MAX` knob (`rs-kernel` memory probe, final run)

One `bench_driver` process, 1000 or 2000 sessions in flight on Postgres, 4 turns each, peak `VmRSS`
sampled every 50 ms, 3 rounds interleaved, 0 failed sessions.

| M | default (glibc) | `MALLOC_ARENA_MAX=2` | saving |
|---|---|---|---|
| 1000 | 268 MB (265 to 269) | 207 MB (205 to 207) | 23 % |
| 2000 | 492 MB (491 to 511) | 344 MB (341 to 357) | 30 % |

The earlier probe at the same cell before the shared tool schema read 550 MB (545 to 567) at M=1000 and 463 MB
with the arena cap; the tool-schema change alone took the default to 268 MB. Set `MALLOC_ARENA_MAX=2` in the
server's environment for a process that serves many sessions at once
([Embedding](./usage/embedding.md#sizing-what-was-measured)).

### Idle agents

Memory stores, one agent per `open_agent`, N agents open and then N runs at once (4 turns, 3 tool calls each), total
resident memory of the client plus the sidecar (`rs`) or of the one process (`py`), median of 3 repetitions, in MB.
The earlier run's value is in brackets.

| | py | rs |
|---|---|---|
| Baseline, 0 agents | 42.2 (42.1) | 38.8 (34.8) |
| N=100 open, idle | 43.6 | 66.5 |
| N=1 in flight | 76.7 (76.7) | 50.6 (47.2) |
| N=10 in flight | 80.2 (80.2) | 54.1 (53.9) |
| N=50 in flight | 95.6 (95.5) | 72.6 (87.5) |
| N=100 in flight | 115.6 (115.7) | 92.1 (126.7) |
| Marginal KB per agent, idle (linear fit, N >= 10) | 13 (14) | 174 (261) |
| Marginal KB per agent after one run | 405 (404) | 426 (833) |
| First run of the first agent | +34.5 MB | +11.0 MB |

Memory is not returned to the baseline after the agents close (N=100, 3 s after close: py 115.9, rs 92.4 MB) and a second
wave in the same process grew it by 0.5 to 2 MB: the allocator keeps it for reuse, not a leak. Python's first run costs
34 MB because it imports the provider SDK and tools then. Below N=100 the Rust side was already smaller; at N=100 it is
now smaller (92 against 116 MB) where it was larger before (127 against 116 MB). The baseline and N=1 cells of `rs` are
3 to 4 MB higher than in the earlier run; that was not investigated.

### The remaining gap and what is known about it

Python still costs less per session at M=2000 (0.12 MB against 0.21 to 0.24). The known causes, from the heap
profile of the earlier code, which has not been repeated on the shared-schema build: about 28 KB of boxed run
future per run, about 26 MB of sqlx argument buffers and session-entry encodings of in-flight writes at M=1000,
`LoopGuard` recent results, and allocator retention across worker threads. About 0.04 MB per extra turn was
measured and not re-profiled after the schema change. Stream decoding, memory and the server's thread count
were each profiled only once; none of these is an exact accounting.

## Reliability

Measured on `50eda478` with a scripted OpenAI-chat provider over HTTP and Postgres stores, unless a row says
final (`617715b3`). Each run used a fixed transcript (4 turns, 6 host-tool calls writing a ledger) and checked
the stored events, the ledger and the usage after the kill.

| Check | Result |
|---|---|
| `kill -9` at a random moment, 1 and 4 concurrent runs | 991 killed runs of 1000 launched, 0 invariant violations (seq contiguous, exactly one result, no tool effect applied twice, usage equals the sum of accepted receipts); 95 % upper bound 0.39 % for 0 of 991. 3 runs were inconclusive in the harness (2 `RunExists`, 1 `RunTerminal`) and are not counted as passes |
| `kill -9` in the delivery window (last chunks of a response, up to 6 ms after its last byte) | 100 kills, 0 violations; the paid provider call was repeated in 14 of the 100 (13 of them killed in the last chunk, `[DONE]`) |
| Paid provider calls repeated per kill, all kills | 4 of 991 repeated once, none twice (each a fully delivered response whose receipt was not yet durable) |
| Takeover after `kill -9` | p50 2.67 s at a 3 s lease (max 3.45 s); p50 29.3 s at the 30 s default; resume accepted to first event p50 16 to 18 ms |
| Kills forced right after the `run.start` answer, 160 runs (final run, `617715b3`; delay drawn with a 50 ms mean) | 0 of 160 found no run (`RunNotFound`), 0 invariant violations. Before the fix the same harness found 70 of 160 acknowledged ids not durable. Kill delay after the answer: min 20, median 61, max 112 ms |
| `SIGTERM` with an interactive run and a job in flight | exits at the drain timeout (1.52 s for 1500 ms); 100 of 100 leases released; a second server takes the interactive run in 81 ms; the job finishes 0.39 s after exit with 0 failures counted. Interactive run only: exit in 28 ms. `SIGKILL` of the same shape: leases held, takeover after the lease (2.7 s) or the job visibility timeout (29.9 s) |
| Postgres latency +50 ms and +500 ms each way, 30 runs each | 30 of 30 succeed, 0 violations, no lease lost (+500 ms: runs take 129 s instead of 1.4 s) |
| Postgres connection reset every 10 s | 60 of 60 runs finish first try |
| Reset every 1 s (stress) | 27 of 30 streams end with `RunError` (-32020, -32024); all 30 reach success after resume or re-issue of the same run id; 0 violations |
| Postgres blackhole of 40 s, lease 30 s | 5 of 30 streams end `-32024` (pool acquire timeout), all resume to success; with a second server it takes over and the first server's writes are refused: 0 duplicated events, tool effects or results |
| Provider faults, 12 scenarios x 30 runs (429, 503, 500, stream cut, hang, fallback) | each scenario deterministic; retries bounded (at most 5 on the primary, 1 on a backup); `total_usage` equals the one completed response in all 360 runs; a cut stream or 500 is flagged `usage_complete: false` |
| Resume of a long session (first event) | 1011 entries: p50 33 ms (p99 43); 9981 entries: p50 112 ms (p99 125), 20 runs each |
| Live suites on vLLM | 196 passed, 1 skipped (needs a DeepSeek key), 1 failed then passed on rerun (test harness race), 0 product failures |

The kill and fault rows are counts of runs, not rates: 0 of 991 is a bound, not a promise of zero.

## Scale and live

### 2500 concurrent runs (earlier run, `50eda478` and `4412ad65`)

One `linch-server` (stdio, so no tenant caps), one agent, N runs at once, one tiny turn each
(`max_tokens` 16, retries off, memory stores unless stated), 0 errors at every N in every table below.

| Provider | N | Result |
|---|---|---|
| DeepSeek (live) | 10 to 2500 | 2500 of 2500 ok, 0 errors (0 x 429, 0 x 5xx, 0 timeouts). Throughput plateaus at 140 to 173 runs/s from N=250; run p50 7.5 s at N=2500. Server 180 MB RSS, 0.12 core |
| vLLM (live) | 10 to 2500 | 2500 of 2500 ok. 124 runs/s, run p50 10.8 s; vLLM ran about 32 to 38 sequences at once and queued the rest. Server 206 MB RSS, 0.07 core |
| vLLM, multi-thread listener with one tenant | 100, 1000, 2500 | 2500 of 2500 ok; 65 threads, 218 MB RSS, 0.16 core |
| Postgres stores, 1 agent, 1000 runs | 10 to 1000 | 0 errors on both providers; the pool of 32 connections was the ceiling (32 backends of ours plus 1 watcher connection), 9 to 11 commits per run at N >= 100 |

Read these as "2500 runs accepted and completed", not as 2500 requests in flight at the provider.
DeepSeek capped in-flight requests at 311 for this account's balance (an HTTP 429 above that, from a direct
test of 500 simultaneous connections); through linch about 110 to 170 requests were in flight, consistent with
the 128-stream limit per HTTP/2 connection that DeepSeek advertises (the connection count was not measured).
Linch added about 0.06 to 0.1 s per run at low N and nothing visible at saturation. The scale numbers used a
stdio server without a tenant; a tenant's `max_runs` defaults to 16 and the server refuses run 17 with
`-32005` ([Server](./usage/server.md)). Raise it in the tenant config for load like this.

### vLLM and SGLang ramps (different times and settings)

One tiny turn per run, retries off, memory store, 0 errors up to N=1024 on both. Medians of 3 samples up to N=256
(N=512 and 1024: one sample). Linch against a plain HTTP client (one connection per request, same body), run wall time in seconds.

| N | vLLM linch / direct | SGLang linch / direct |
|---|---|---|
| 1 | 0.11 / 0.11 | 0.05 / 0.04 |
| 32 | 0.38 / 0.32 | 0.11 / 0.11 |
| 128 | 1.16 / 1.12 | 0.30 / 0.29 |
| 256 | 2.03 / 2.12 | 0.52 / 0.58 |
| 1024 | 6.85 / 8.00 | 2.43 / 2.10 |

These are not an engine comparison. SGLang 0.5.23 (NVFP4 snapshot, 71 running requests) ran alone on a free GPU slice at
`d9beaa3a`; vLLM 0.32.0 (64 sequences, speculative decoding) ran on the shared production GPU at `13c7266b`. The
result they support: linch is within the spread of a plain client on both engines and adds no errors.

### Tenant cache isolation on vLLM

With `allow.cache_isolation` on, two tenants sent the same ~7200-token prompt: the first run of a tenant read 0 cached tokens, its
second read 4352, and the other tenant's first run read 0. A tenant with isolation off cached separately from both. At about
3000 tokens vLLM cached nothing on this model (its block is 2176 tokens), so no result is claimed at that size.
The `cache_salt` field itself was not captured on the wire; the evidence is the behaviour.

## Known gaps

### Unexplained

- Case 3 Postgres cells had runs of iterations of 3 to 22 s (against 0.1 to 1 s) in 4 of the 12 cells of the
  earlier run, on `py` and `rs-kernel`, and none on `rs-client`. In the final run: 0 of 720 iterations exceeded 5x their cell's median. Not explained; the host and
  Postgres were not instrumented during the earlier stalls.
- Case 6 in the earlier runs (builds before #211) moves between two levels inside one cell, about 1.7x apart (`rs-kernel` Anthropic 290 against
  510 ms, `rs-client` 380 against 580 ms), on all three sides. The levels are CPU frequency levels: the governor is
  `powersave` with `intel_pstate` and HWP, base 2.0 GHz, turbo 3.4 GHz, ratio 1.70. In a profile of 60 pinned client
  iterations of identical work, 48 used 507 to 626 ms of server CPU with the core read at a median of 1998 MHz,
  and 12 consecutive ones used 304 to 313 ms at a median of 3358 MHz. Pinning does not select the level: a busy
  core stayed at 2.0 GHz for tens of seconds in a decode microbenchmark. The final run did not record the
  frequency, so its 594 against 295 ms mixes the two levels; the Anthropic row was
  re-measured with the level recorded per iteration. In the case 6 run (section above) the core's median was 3.40 GHz in every cell and no cell split in two; why the earlier runs did split is not determined. Why HWP holds a busy core at base for seconds is not
  determined (it needs root tooling: MSRs, `turbostat`).
- Method: a benchmark cell must record the CPU frequency level per iteration and compare at equal level.
- Case 6 client path against the kernel, profiled on a build before #211 and not repeated since (3.4 GHz, 64 KiB pipe, 30 iterations per rung, medians). The
  earlier hypothesis that the cost follows the number of JSON-RPC messages is refuted for this case. A run sends
  about 61 lines for 20 006 envelopes, because the server joins the queued envelopes of a run into one line of up
  to 64 KiB. Client `orjson.loads` is about 12 ms of roughly 365 ms. Most of the remaining gap is the server
  waiting on a full 64 KiB stdout pipe: 43 voluntary context switches per run and 39 to 56 ms blocked, against 2
  switches and 7 to 10 ms with a 1 MiB pipe. A paced stream (100 deltas/s) sends one envelope per line and costs
  client CPU, about 3% of a core per stream, not wall time. The Python client raises the child's stdout pipe to
  256 KiB after the spawn (`F_SETPIPE_SZ`, Linux; a refusal keeps 64 KiB). Measured through the real client,
  interleaved, 4 cores, n=34 and 36 runs at 2.0 GHz on both the server and client cores: median wall 566 ms with 64
  KiB and 535 ms with 256 KiB (5.6% less), server voluntary context switches 44 and 6 per run. At a 3.4 GHz server
  core (client core at 2.0 GHz) only n=3 and n=5 runs were reached: 375 and 325 ms, indicative only. The client to
  server pipe is not changed: a run sends one 119-byte request.
- Message format on stdio: msgpack, protobuf and compression were evaluated and rejected. `python-msgpack` used 5 to
  11% more client CPU than orjson for 19% fewer bytes. Compression loses on a local pipe: zstd level 1 needs
  1.5 ms to compress the 3.7 MB stream, and the pipe moves it in 1.6 ms. Remote WebSocket links were not measured.
- Case 14 (many sessions on Postgres): `rs-client` is slower than `rs-kernel` by about 1.7x at M=2000 (10.5 s
  against 6.25 s), and the case 6 findings above do not explain it. It appears only with Postgres in a single
  sidecar: 4 sidecars take 5.76 s. With memory stores one sidecar did 1272 to 1487 turns/s at M=2000 (window 5.38 to
  6.29 s) and the client used under 20% of one core, so neither JSON-RPC messages nor client decode account for
  it. The 10.5 s row is a one-thread limit: a sidecar spends about 1 ms of CPU per turn at 2.1 GHz on its one
  thread (about 700 to 750 turns/s), more pool connections on that thread gain nothing (64 connections 9.2 s,
  80 connections 10.1 s, against 9.7 s with 32), and more threads do. Measured at M=2000 with 32 connections
  (run wall p50, medians of 3 to 4 rounds at 2.1 to 2.3 GHz): one thread 9.7 s; one sidecar with 2 workers 9.1 s,
  4 workers 7.4 s, 8 workers 6.9 s, 16 workers 6.4 s; 2 sidecars of 16 connections 7.5 s; 4 sidecars of 8
  connections 6.3 s; the in-process kernel 6.3 s. Server CPU was 7.8 s on one thread, 13.2 s with 4 workers and
  17.1 s with 16. The gap to the kernel is 0.8 s at M=500, 1.3 s at 1000 and 3.4 s at 2000. That the wait inside
  durability barriers (about 4.5 s per run, almost all of it waiting for a pool connection) is the one thread
  turning connections around is inferred, not profiled. `LINCH_STDIO_THREADS` and the sidecar split are described
  in [Running the server](usage/server.md#many-runs-on-one-stdio-sidecar).
- Server thread count: the DeepSeek runs reached 110 threads at N=2500 (the stdio server uses one thread otherwise).
  The same loopback stub (no live provider), N=1000 runs, memory stores: addressed by IP literal the server had 1 thread
  in 4 of 4 runs; addressed by the host name `localhost`, 7, 17, 6 and 5 threads. So the extra threads come with host
  name resolution (blocking pool), not with the run count. The 110 threads of the DeepSeek runs were not reproduced
  with a local resolver, so the size of that number (real DNS latency, N=2500) is not explained.

### Not measured or weak

- Case 6 (all five vendors) was re-measured on `1da43ee3` at one frequency band, replacing the earlier cells. Not measured there: one build before and after #211 (so the effect of the framing rewrite is not isolated), the replay server on a separate core from the side under test, `rs-client` with its client and sidecar on separate cores at 3.4 GHz, and the client-path profile above after #211. Where inside a cell the sub-3.3 GHz samples fall was not recorded.
- Loop, tools, startup, 16-session throughput and the other decode providers are one pass from the earlier run.
- The idle-agent table is memory stores only; the Postgres-store memory cells (backends per agent, pool size) were not repeated in the final run. The memory of a run in flight and of an agent that has run once cannot be told apart in these data: the peak equals the after-runs figure.
- Single-sample cells: vLLM and SGLang ramps at N=512 and 1024, every DeepSeek and vLLM load point, the cache-salt check (one run per row), the `SIGTERM` and fault rows (one pass).
- Postgres-bound cells depend on commit latency and ran on a host that also served other work. No quiet-machine run exists.
- No run on a different CPU architecture, no run with a real network between the sidecar and the provider other than the live DeepSeek and vLLM ramps, and no comparison with another agent framework.
