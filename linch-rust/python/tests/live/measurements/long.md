# Area long: results (live run 2026-11-18, stack tip f69d97b2, vLLM qwen3.6-35b-a3b through a body-logging tee)

Source: `e2e/results/long/` (`expectations.md`, `*.json`, `*.tee.jsonl`, `compaction*.run.log`, `schedule.run.log`,
`release_pass.log`). Correctness checks ran on the debug binary, 1 run each unless a row says otherwise. The release
binary was used only for four steps (see `release_pass.log`): steer latency, compaction time, memory recall latency,
schedule. Load average 2.3-3.3 on a 64-core shared host (per file). The files hold no verdicts; every verdict is derived
by comparing `expectations.md` with the data. A `debug_run/` subdirectory holds a second copy of the same file names (not compared);
only the top-level files are quoted.

## Checks

| id | feature | expected | observed | verdict |
|---|---|---|---|---|
| S1-S3, S11 | steer semantics | queued answer; call waits for the tool; one alignment after tool_call_end; model follows; same id twice taken once | 5 of 5 reps: queued answer, still waiting after 2 s of a 4 s tool, 1 alignment with the right steer_id after tool end, word followed 5 of 5; second send of the id returned the same answer, 1 alignment | MATCH |
| S4, S5 | refusals | finished run -32026, unknown -32021; blank, 16385 bytes, bad id -32038; 16384 accepted | exactly so; 129-byte id refused; 8192 two-byte chars accepted, 8193 refused (bytes counted); each refusal answered in 1 ms | MATCH |
| S6, S7 | 17th steer, cancel with 16 waiting | 17th refused at once LimitReached steers; cancel returns within 2 s, run aborted | 17th refused in 2 ms (-32005, kind steers, max 16); cancel returned in 4 ms; run `aborted`; the 16 waiting calls returned their ids (not RunError) | MATCH |
| S8-S10 | suspended run, restart, undriven run | steer accepted at once; delivered exactly once; undriven run RunLeased then InvalidSteer | suspended: answered in 15 ms, stayed awaiting_approval, 1 alignment after approve, 1 steer_received row. Running run after restart: 1 alignment, 1 row, success. Undriven: RunLeased -32033 (0.529 s left), InvalidSteer after expiry; other server: RunLeased -32033 (4.751 s left) | MATCH |
| M1 | steer return to alignment | < 100 ms | event-minus-return about 0 ms in 5 of 5 (signed, rounds to -0.0 s); event within 0-1 ms of the tool end | MATCH |
| L1, L2 | goal section and task ledger | goal in every request; ledger in each request tail; rows durable | 15 provider requests: goal in system in all; ledger in tail in all 14 non-summary requests; 13 assistant turns; rows 1-3 completed, row 4 pending. One TaskUpdate answered "Task not found." | MATCH |
| L3, L4 | ledger after compaction, in a fresh session | rows unchanged; ledger in the next request; fresh session sees open tasks | 1 summary compaction (2416 to 1257 tokens); ledger in the 2 requests after it; fresh session's first request (826 tokens) lists #4 pending and the answer names Step D | MATCH |
| L5 | 60 s heartbeat, 3 fires | same session, fire n+1 holds fire n | session long-beat each time; requests of 2, 4, 6 messages (58, 106, 154 prompt tokens); replies CHECK-IN 0, 1, 2. The first (debug) run failed its own ordering assertion because heartbeat and cron requests interleave; the release run passed | MATCH |
| L6, L7, M2 | schedule floor, cron boundary, jitter | -32004 schedule_interval; cron at :00 within about 2 s; jitter within about 1-2 s | 30 s refused (min_ms 60000); cron fires 20:29:00, 20:30:00, 20:31:00, start lag 0.45-0.70 s; first cron fire 54 s after creation; heartbeat start lag 0.48-0.76 s | MATCH |
| C1, C2 | summary request shape and size | prefix of the preceding call plus the turns since plus one instruction; tools and system identical; not 2x | prev_is_prefix, tools and system equal in all 56 summaries; prompt ratio to the preceding call 1.03-1.33 at the default setting, 1.06-1.13 at larger histories; cached_tokens 0-34816 | MATCH (derived; exactly-one-trailing-message checked only through the instruction text) |
| C3, C4 | summary accepted; recall | tokens_before > tokens_after; recall 3 of 3 | default: 10 compactions per run, after/before 0.77-0.89, 0 rejected. Threshold 2000: first compaction 2020 to 2020 (equal) and one later at 2045 to 1815. Recall 3 of 3 default reps (5 of 5 other runs) | MATCH, except one non-shrinking compaction at threshold 2000 (MISMATCH) |
| C5 | verbatim section | `[user message, verbatim]` blocks by default; none with keep_user_tokens 0 | default: 2 blocks in the next request (4 in the verbatim test); keep_user_tokens 0: 0 blocks, after/before 0.58-0.74; first summary request 7318 vs 7313 prompt tokens, 15 messages both | MATCH |
| C6 | second compaction | instruction says update; exactly one summary message | 2nd and later summaries use the 887-char update instruction (first 659); summary count in the next request 1 in all | MATCH |
| C7 | window path | compaction fires at 80% of a small window | first attempt (window 6000, 20 fillers x 60 words): no compaction, test failed. A later run passed with 3 compactions; the window of that run is not recorded | MATCH (derived; parameters of the passing run not in the files) |
| C8 | hold after 2 rejections | not run | not run, needs a model that refuses to summarize | NOT RUN |
| M3 | time per compaction | grows with prefix size, seconds | median 3.33 s (tokens_before about 2.0-3.5k), 3.53 s (about 6.6k), 3.10 s (about 14k); flat, not growing | MISMATCH (derived; prefix caching keeps it flat) |
| B1, B2 | context budget drop and pinned | whole blocks dropped, used <= max, kept text present; pinned alone stays with over_by | cap 250: dropped alpha 0, alpha 1, beta 0, used 200. Cap 350: dropped alpha 1, beta 0, used 300. Cap 150: used 100. Pinned alone, cap 50: used 100, over_by 50. Dropped text absent from the request, kept present | MATCH (drop order not checked against the config, which is not in the files) |
| B3 | sections order and tokens | A, B, C, base prompt, append; prompt_tokens follow section length | positions 0, 16, 1827, 3648, 3701; prompt tokens 36 baseline, 651 with sections, 1551 doubled | MATCH |
| K1 | file memory | index in `/memories/MEMORY.md`, injected, answer from it | first run: model wrote the index to `/MEMORY.md`, not injected, answer Teal anyway; 3 reps: index at `/memories/MEMORY.md`, fenced in the tail, answer Teal 3 of 3 | MATCH (3 reps); first run MISMATCH (model wrote the index at the root) |
| K2, K3 | long-term memory and scopes | search finds it; recall in tail; scopes isolated; no principal fails | MemorySearch found it in a fresh session; recall block in the tail with untrusted marker, answer Thursday; bob's search "No memories found."; no-principal save refused with is_error; rows by scope 4:acme:ada 1, 4:acme:bob 1 | MATCH |
| K4 | memory cap | save past the cap fails with the limit text | "the memory limit of 2 for this user is reached; delete a memory first", twice | MATCH |
| K5 | recall latency | well under 300 ms | p50 recall gap 10.5 ms (off 8.5 ms); MemorySearch tool p50 6.0 ms | MATCH |
| P1 | long poll | no early end; reports done after at least 20 s | 20.9 s, 3 BashOutput calls, no loop_guard, `done-20`, exit code 0 | MATCH |
| P2, P3 | idle bound; process reaped | loop_guard after limit identical polls; no sleep left | limit 3: guard repeated_tool_call after 4 polls, subtype error; process alive after the run, gone after close_session; long-poll run left none | MATCH |

## Measurements

| measurement | value | notes |
|---|---|---|
| steer call to answer | 3.003-3.008 s | 5 reps, release, load 2.30; the answer comes at the end of the 4 s tool batch |
| steer answer to alignment event | about 0 ms; event 0-1 ms after the tool end | same 5 reps |
| steer refusal latency | 1 ms | 4 refusals, 1 run |
| 17th steer refusal / cancel with 16 waiting | 2 ms / 4 ms | 1 run |
| compaction duration, threshold 2000 | median 3326 ms (3040-4152) | n=9, release, load 3.07 |
| compaction duration, threshold 6000 | median 3527 ms (3359-3632) | n=4, load 2.63; summary prompt 17.1-17.6k tokens, 10.9-13.1k cached |
| compaction duration, threshold 14000 | median 3098 ms (2264-3281) | n=3, load 2.40; summary prompt 37.6-39.2k tokens, 32.6-34.8k cached |
| compaction duration, default threshold (debug) | medians 3243, 2693, 3283 ms (2512-4602) | 3 reps x 10 compactions, load 3.06-3.18 |
| compaction summary latency vs output | 2.6-3.5 s at 396-570 completion tokens | default rep 1, n=10; summaries that hit the 600-token cap took 3.3-4.6 s |
| compaction shrink ratio | default 0.77-0.89; keep_user_tokens 0: 0.58-0.74; threshold 14000: 0.58 | n=10 / 6 / 3; default keeps 2 verbatim blocks so it re-triggers almost every turn (10 compactions in 26 requests) |
| summary prompt minus preceding call | 227-2095 tokens | default reps; at the default threshold the preceding call is 5.5-7.7k tokens |
| context-window compaction (debug) | 3 compactions, 4871 / 4759 / 4731 tokens estimated before, 3191 / 3163 / 3172 after; durations 3110 / 2463 / 3314 ms | the wire prompt of the request before the first one was 11,902 tokens |
| memory recall latency | p50 gap 10.5 ms (range 5-16), off 8.5 ms (6-11); MemorySearch p50 6.0 ms (4-38) | n=10, release, load 2.40, 201 rows in the scope, 5 hits per recall |
| heartbeat jitter | start lag 0.756 / 0.646 / 0.477 s; queue lag 0.718 / 0.612 / 0.442 s | 3 fires, worker poll interval 1000 ms, release, load 2.89 |
| cron start lag | 0.696 / 0.634 / 0.447 s | 3 fires, same run |
| budget drop prompt size | 146 wire prompt tokens with 2 kept blocks | 1 run (cap 250) |
| long poll | 20.9 s elapsed, 3 BashOutput calls | 1 run |
| steer bytes bound | 16384 bytes accepted, 16385 refused | 1 run; chars vs bytes confirmed with two-byte chars |
| ledger run | 13 assistant turns, prompt 910 to 3901 tokens, 1 compaction at 2416 to 1257 | 1 run |
| section tokens | +615 tokens for +3652 chars; doubled: +900 tokens for +5400 chars | about 6 chars per token; 1 run each |
