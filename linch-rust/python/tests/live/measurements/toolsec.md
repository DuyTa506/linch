# Area toolsec: results (live run 2026-11-18, stack tip f69d97b2, Postgres database py_fix_c)

Source: `e2e/results/toolsec/` (`expectations.md`, `bash.jsonl`, `sandbox.jsonl`, `remote.jsonl`, `mcp.jsonl`,
`agents.jsonl`, `otel.jsonl`, `provider.jsonl`, `tenants.jsonl`, `tenants_pg.jsonl`, `bench.jsonl`,
`final-debug-run.txt`). Correctness checks ran on the debug binary, 1 run each. The last full debug run recorded
35 passed, 1 failed (the failure is row `3.3 bg`). Measurements ran on the release binary (the 256 MiB flood is timed on both).
Load average 2.3-3.7 on a 64-core shared host (recorded per line). `_iter`, `_iter2`, `_iter3` hold earlier iterations
of the same files; they were not compared line by line and the final top-level files are quoted. The files hold no
verdicts apart from the failing test; other verdicts are derived by comparing `expectations.md` with the data.
Docker (2.8) and a root-run sandbox (2.7) were not run; server-side OTLP (8.3) does not exist by design.

## Checks

| id | feature | expected | observed | verdict |
|---|---|---|---|---|
| 1.1, 1.4, 1.5 | local Bash basics | output plus `[exit code 0]`; background process_id and pid; BashKill kills the group | `hi`, workspace path, `[exit code 0]`; process_id `bash_<12 hex>` with pid in metadata; BashOutput `bg-finished-77` exit 0; `[killed; process group killed]` | MATCH |
| 1.2, 1.3, 1.10 | timeout, truncation, flood on local Bash | timeout under 6 s; head 4000 and tail 26000 bytes; flood truncated, not output_limit | timeout in 1503 ms; result 30,039 bytes (4000 head, 20,000 bytes omitted, 26,000 tail); 300 MB flood: 30,042 bytes, code none, 0.34 s | MATCH |
| 1.6-1.9 | process cap, close_session, client drop, server kill | 5th refused too_many_processes; close kills; client drop leaves no orphan within 5 s; server kill kills background | 4 started, next refused (text says 4 already run); 4 pids gone 0.3 ms after close_session; client drop: background and foreground reaped in 0.062 s and 0.061 s, 0 left; server SIGKILL: background dead in 0.3 ms, foreground survived | MATCH (the cap in the text is 4 running, expectation said 5th refused: consistent) |
| 1.11 | env allowlist | only PATH, HOME, TERM, LANG, LC_ALL, LC_CTYPE, USER, SHELL, TMPDIR (+PWD); no DSN or canary; server environ unreadable | names exactly HOME, LANG, LC_ALL, LC_CTYPE, PATH, PWD, SHELL, TERM, TMPDIR, USER; `unexpected` list empty; own environ same; `/proc/$PPID/environ` Permission denied | MATCH |
| 2.1, 2.6 | sandbox filesystem and env | write inside works; outside and home writes denied; home read denied; server environ unreadable; env has private HOME, TMPDIR | inside write ok; outside and home writes `Permission denied` (local control rc=0); home read denied (local control read it); server environ denied (also denied locally); test-process environ denied (local readable); HOME and TMPDIR under a private `/tmp/linch-sandbox-*` dir | MATCH |
| 2.2-2.5 | sandbox network and syscalls | inet and unix EACCES, socketpair works; clone3 ENOSYS; io_uring refused; unshare, mount EPERM; unnamed syscalls allowed | inet and unix `refused EACCES` (local opened); socketpair works; clone3 ENOSYS, io_uring_setup EACCES, unshare and mount EPERM, fork works; getcpu, sched_getaffinity, getrandom, membarrier succeed. network:true probe: 2 opened, 3 refused (probe labels not in the file) | MATCH (2.3 derived) |
| 2.7, 2.8 | sandbox as root; docker backend | refused as root; docker behaviour | not run: not root, docker socket permission denied | NOT RUN |
| 2.9 | tenant execution policy | default list cannot pick sandbox, docker, remote; altered config refused | sandbox network true, bigger memory, local, docker with allow_root, image or network host: all NotAllowed kind execution; exact entries open; docker exact entry opened and Bash then failed with `permission denied ... docker.sock` | MATCH |
| 3.1, 3.2, 3.4, 3.6, 3.7 | remote host basics | Bash tools only; timeout kills; principal passed; tenant list; schedule refused -32040 | tools Bash, BashOutput, BashKill; timeout 1508 ms, 0 group members left; principals acme/alice/admin, acme/bob, none; tenant not listed: NotAllowed execution, exact entry works, altered config refused; schedule and enqueue refused -32040 reason remote_execution | MATCH |
| 3.3 fg | flood past 256 MiB | code output_limit and the limit text | code output_limit, text ends `[output limit reached (256 MiB); the command was killed]`, text 30,085 bytes; 29.12 s debug, 5.16 s release | MATCH |
| 3.3 bg | BashOutput metadata `status: output_limit` | status reachable in the event | error_code output_limit and text present; metadata None; status in none of metadata, canonical_output, result block | MISMATCH (doc claim vs event stream; RED test `test_a_background_flood_reads_output_limit_status`; the files do not say whether a fix PR changed it) |
| 3.5 | remote command vs connection | clean close kills; client SIGKILL survives; server SIGKILL kills | clean close: gone in 0.055 s; client SIGKILL: `sleep 302` survived (gap, as expected); server SIGKILL with client alive: gone in 0.043 s | MATCH |
| 4.1, 4.2, 4.4 | MCP tools | tools listed; exec scope asks approval; isError continues; child gone on close | 5 tools `mcp__ts__*`; approval asked for add, fail, pid (not echo); fail is_error with text "deliberate failure"; child alive between runs, dead 0.03 ms after agent close | MATCH |
| 4.3 | MCP polls | identical calls end by loop guard at 3 (amended); polls: 12 ok, 40 end near 26 | no polls: guard at 3 calls; polls 12 calls: success; polls 40 calls: guard at call 26 | MATCH (after the amendment A1; the original threshold wording was a misreading) |
| 4.5, 4.6 | MCP lifeline and tenant | child dead within 5 s of server kill; command and env refusals; PATH entry exits 2 | child dead in 0.051 s; NotAllowed mcp_command and mcp_env; config listing PATH: exit code 2 with message | MATCH |
| 5.1, 5.2 | skill and subagent with a real model | answer holds the skill code word; read-only child | skill launch-it: answer TSKILL-7731; subagent child tools [Read], result "the beacon number is 3190" | MATCH |
| 6 admission | refusal kinds, before anything starts | each kind NotAllowed -32004 | 18 kinds observed: provider, provider_request, execution, memory, task_ledger, provider_base_url, run_bound, namespace, permission_mode, auto_allow, tool_cache, workspace, schedule_tools, mcp_command, postgres, workflow, schedules, background_runs; kind `principal` is not in the data; 20 refusal messages scanned, no leaks | MATCH (principal kind NOT RUN or not recorded) |
| 6 limits, isolation, cache | LimitReached kinds; cross-tenant ids; cache scope | limits trip; ids not found; cache miss across principals | connections max 2 (HTTP 429), agents max 2, schedules 1, workflows 1, schedule_interval refused; events and resume RunNotFound, job JobNotFound, schedules and jobs empty; cache s1 miss, s2 hit, other principal s3 miss, s4 hit, write tool then miss; Postgres two tenants: false, false, true, true with 2 tool runs | MATCH |
| 7, 8.2 | scrubbing, header admission, log levels | 6-byte non-credential visible, credential and 8+ bytes scrubbed; credential headers refused; extra_body cannot hijack; log levels | `x-trace-id` (6 bytes) in error text, `x-gateway-token`, `x-secret-key`, 8+ byte `x-long-trace` absent; key absent from text and log; 10 header names refused provider_request, host and 4 others OpenError; extra_body hijack false; debug log (37,363 bytes; 125,798 bytes with worker) holds no value; default stderr 305 bytes WARN only, info 534 bytes, bogus LINCH_LOG falls back to warn with a message | MATCH |
| 8.1 | OTLP from the client Telemetry | spans and usage sums | 1 POST to /v1/traces; spans invoke_agent, step, chat, execute_tool (plus the harness `POST /ask`); tokens 22 in / 10 out equal 11+11 and 5+5 from the usage events | MATCH |
| M1, M5 | tool call overhead | no-op host tool < 5 ms p50; local Bash < 10 ms p50 | host no-op 0.75 ms, local Bash 1.465 ms, sandbox Bash 21.17 ms | MATCH |
| M2 | tool cache | miss about 200 ms, hit < 20 ms | miss 202.7 ms, hit 0.117 ms | MATCH |
| M3 | admission cost | within 5 ms | tenant 0.748 ms vs stdio 0.762 ms | MATCH |
| M4 | RSS after 100 runs | plateau between run 50 and 100 | 26,948 KB at 50 runs, 32,380 KB at 100: +5,432 KB, linear | MISMATCH (derived; the later M4b rounds flatten near 38.1 MB) |

## Measurements

| measurement | value | notes |
|---|---|---|
| host no-op tool, gap start to end of next call | p50 0.75 ms, p95 0.902 ms (medians of 3 repeats) | 3 x 200 calls, release, load 2.93; per-call wall 0.66-0.82 ms |
| local Bash `true`, gap per call | p50 1.465 ms, p95 1.925 ms | 3 x 200 calls; event duration p50 1 ms |
| sandbox Bash `true`, gap per call | p50 21.17 ms, p95 22.55 ms | 3 x 50 calls; event duration p50 20 ms |
| tool cache miss, 200 ms read tool | p50 202.66 ms (201.99-203.33) | n=30, load 3.18 |
| tool cache hit | p50 0.117 ms (0.065-0.157); speedup 1730x at p50 | n=30 |
| agent.open, tenant vs local stdio | p50 0.748 ms vs 0.762 ms (p95 1.222 vs 1.091) | n=60 each (20 agents x 3), memory stores |
| server RSS, 100 runs (1 Bash call per run) | start 10,880; open 19,284; 25 / 50 / 75 / 100 runs 24,208 / 26,948 / 29,680 / 32,380; after close 32,516 KB | 1 run, load 3.18 |
| server RSS, 12 open/close rounds | 32,244 KB after round 0, 35,164 after round 1, 38,124 from round 8 to 10, 38,168 after round 11 | 1 run, load 3.67; what a round contains is not described in the file |
| remote 256 MiB flood, foreground | 5.16 s release, 29.12 s debug | 1 run each, text 30,085 bytes, load 3.53 release |
| remote 256 MiB flood, background | 4.37 s release, 26.23 s debug | 1 run each |
| local Bash 300 MB flood | 0.34 s, 30,042 bytes | debug, 1 run |
| local Bash timeout | 1503 ms for timeout_ms 1500; remote 1508 ms | 1 run each, debug |
| kill and close reaping | client drop 0.062 s / 0.061 s; close_session 0.3 ms; server SIGKILL 0.3 ms (local), 43 ms (remote), 51 ms (MCP child); clean remote close 55 ms | 1 run each, debug, load 2.4-3.0 |
| MCP polls | no polls ended at 3 calls; polls 12 calls ok; polls ended at 26 | 3 runs |
| provider error run | HTTP 500 upstream: run result error after 15,681 ms, retryable true | 1 run, debug, load 2.57 |
| debug log volume | default 305 bytes, info 534, debug 37,363 | 1 run each |
| OTLP | 1 POST, 5 span names, 2 metrics (operation.duration, token.usage) | 1 run |
