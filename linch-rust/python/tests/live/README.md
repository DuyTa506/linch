# Live end-to-end suites

Tests that drive a real `linch-server` process with a real model and a real Postgres. They carry the
`live` marker and are not part of the unit gate: without the environment below every one of them skips
(`pytest tests/live` on a bare environment reports only skips, nothing fails).

## What the environment is

| variable | meaning |
|---|---|
| `LINCH_SERVER_BIN` | the `linch-server` binary to test. Use a debug build for correctness runs and a release build for every latency, throughput or memory number |
| `LINCH_TEST_PG_DSN` (or `LINCH_LIVE_PG_DSN`) | a Postgres database the tests may create schemas in; each test makes its own schema and drops it |
| `LINCH_TEST_VLLM_URL` (or `LINCH_LIVE_VLLM_URL`) | base URL (ending `/v1`) of an OpenAI-compatible vLLM server |
| `LINCH_TEST_VLLM_MODEL` | the model name that server serves |
| `LINCH_LIVE_KIND` | optional; the provider kind the suites drive the server as (default `vllm`; for example `sglang`). See below |
| `DEEPSEEK_API_KEY` | only for the DeepSeek scenarios; tests that need it skip without it |
| `E2E_RESULTS`, `TOOLSEC_RESULTS`, `E2E_DIR` | where the suites write their raw JSON/JSONL results (default: a `linch-live-e2e` directory under the system temp directory) |

Run one suite:

```bash
cd linch-rust/python
export LINCH_SERVER_BIN=/path/to/linch-server
export LINCH_TEST_PG_DSN=postgres://user:pass@127.0.0.1:5432/scratch
export LINCH_TEST_VLLM_URL=http://127.0.0.1:18201/v1 LINCH_TEST_VLLM_MODEL=qwen3.6-35b-a3b
pytest -m live -q tests/live/test_live_long_steer.py -k "not deepseek"
```

Assertions are on durable facts (events, rows, files, process state), never on wording. A test that
depends on the model following a script retries the scenario once and says so on stderr.

## Running the suites against SGLang

The suites speak to `LINCH_TEST_VLLM_URL` as provider kind `vllm`. To drive an SGLang server instead, point
`LINCH_TEST_VLLM_URL` at it and set `LINCH_LIVE_KIND=sglang`; every suite then sends `{"id": "sglang", ...}` as
its provider, and the collected cases, ids and skip rules are the same (`pytest --collect-only -m live`
reports the same count with and without the variable). Unset, nothing changes. The server must be started
with the flags the scenarios need (a reasoning parser, a tool-call parser and `--enable-cache-report`).
Assertions that read the server's raw usage use `wire_usage.reasoning_tokens`, which accepts SGLang's
`usage.reasoning_tokens` and vLLM's `usage.completion_tokens_details.reasoning_tokens`. The `ts_*` toolsec
suites use a scripted local model, not a server, and keep the `vllm` kind.

## The vLLM proxy

A vLLM host is usually shared. The suites were run through a small local proxy in front of it that
serialises requests (one at a time), caps `max_tokens` (injects 1500 when absent, 400 above), and logs every
request with its prompt, completion and cached token counts. Point `LINCH_TEST_VLLM_URL` at such a proxy
rather than at the shared host; the proxy log is also how the tests compare a wire request with a
reported usage. Keep outputs small (`max_tokens` of 400 or less unless a test needs more).

## DeepSeek rule

The key never goes into a file, a test, an output or a commit: export `DEEPSEEK_API_KEY` in the calling
shell only. The scripts in `e2e_deepseek/` forward through a counting gateway that refuses a request without
`max_tokens`. Check the account balance before and after a run and stop when it is under 0.50 USD.
`e2e_deepseek/run.sh --mock` rehearses every scenario against canned replies with no network and no key.

## Layout and what each area expects

Each area's expectations were written before its first run (`expectations/<area>.md`: feature, the exact
observable, the threshold or count, and the documented claim it comes from). The measured results are in
`measurements/<area>.md`: one table of checks (expected, observed, verdict) and one of numbers.

| area | files | collected cases | expectations | measurements |
|---|---|---|---|---|
| core (first pass) | `test_live_core_*.py`, `live_core_support.py`: basics, host tools, Bash, permissions, hooks, loop guard, steering, structured output, subagents and skills, stdio MCP, sandbox | 99 (each runs on vLLM and, with a key, on DeepSeek) | one line each below | |
| core (checked against the docs) | `test_live_e2e_core_*.py`, `e2e_core_support.py`: usage and cost, tool loop, structured output, `tool_choice`, effort, temperature, budgets, loop guard, gates, limiter, hooks, skills, subagents, overhead and memory numbers | 53 | `expectations/core.md` | `measurements/core.md` |
| schedules, jobs, workers, resume | `test_live_sched_*.py`, `live_sched_support.py`: compaction and recall, jobs, observability, retry and fallback, resume after kill, schedules, tenancy, the model's own schedule tools, workers | 42 (vLLM and DeepSeek variants) | the claims of the docs, one line each in the test docstrings | |
| durable | `test_live_durable_liveness.py`, `durable_e2e/` (scenario scripts: crash points, lease takeover and fencing, approvals, workflows, schedules across kills, liveness, group-commit counts, resume latency) | 2 cases, 9 scripts | `expectations/durable.md` | `measurements/durable.md` |
| long | `test_live_long_*.py`, `e2e_long_support.py`: steering, compaction, context budget and sections, task ledger, file and long-term memory, background polling, heartbeat and cron | 27 | `expectations/long.md` | `measurements/long.md` |
| toolsec | `test_ts_*.py`, `ts_*.py`: Bash, sandbox, remote execution through `SubprocessHost`, stdio MCP, tenants and admission, provider scrubbing, tool cache, tracing, overhead | 41 | `expectations/toolsec.md` | `measurements/toolsec.md` |
| DeepSeek | `e2e_deepseek/` (scripts, not collected by pytest): final tool and forced choice, prompt cache, cost arithmetic, compaction cache sharing, cost caps, effort, provider concurrency, headers | 9 scenarios, run by script | `expectations/deepseek.md` | `measurements/deepseek.md` |

First-pass core in one line each: a text turn streams and accounts usage; thinking on streams deltas and off
does not; host tools run (reads together, writes one at a time); Bash runs, fails, runs in the background and
is polled or killed; a write waits for approval and a denial is read by the model; hooks block, rewrite and
inject; a loop guard ends a repeated call; cancel and steering behave; structured output and `tool_choice`
work; subagents and skills run; a stdio MCP server dies with its agent; the sandbox confines or refuses.

## Measurements

Measured on 2026-11-17 and 2026-11-18 on a shared 64-core host with a load average of 2 to 3, against the
stack at `f69d97b2` (before the two fix PRs). Medians unless a row says otherwise; sample sizes are in
`measurements/<area>.md` and most rows are 3 to 30 samples. These are one host's numbers, not benchmarks.
The vLLM numbers are those of one `qwen3.6-35b-a3b` behind a serialising proxy.

### core

| measurement | value |
|---|---|
| linch overhead per turn, a 1-turn text run (release) | median 22.5 ms (16.8 to 28.7 ms) |
| `agent.open` | p50 28.6 ms |
| events per second, 200-event run, memory store / Postgres (stream deltas) | 130,638 / 8,759 |
| events per second, tool loop on Postgres | 430 |
| server RSS idle, after 20 model runs | 17.2 MB, 18.9 MB |
| two read tools / write tools | read starts 2.3 ms apart; write spans do not overlap (gap 8.7 ms) |
| provider limiter, cap 1 | the second request reaches the provider 7.3 ms after the first ended |

### durable

| measurement | value |
|---|---|
| resume latency, server start to first resumed event (lease already over) | p50 59 ms (48 to 64 ms), 5 samples |
| takeover after `kill -9`, lease 3 s / 30 s | 2.34 to 3.02 s (mean 2.70 s) / 28.6 s |
| run throughput on Postgres, 1 / 4 / 16 concurrent | 56 / 180 / 258 runs per second (memory stores: 4568 / 6713 / 7516) |
| Postgres write cost per tool turn | about 14.9 ms (about 7 commits, about 1.6 ms each) |
| crash points (6 points x 3 reps) | 0 duplicated model calls beyond the in-flight one, 0 repeated tool runs, strictly increasing event sequence |
| a silent WebSocket peer dropped | 2.06 s at a 2 s pong timeout; 60 s at the default |

### long

| measurement | value |
|---|---|
| steer call to answer / answer to alignment event | 3.003 to 3.008 s (at the end of a 4 s tool batch) / about 0 ms |
| 17th steer refused / cancel with 16 steers waiting | 2 ms / 4 ms |
| compaction time at a 2000 / 6000 / 14000 token trigger | 3.33 s / 3.53 s / 3.10 s (flat: a cached prefix) |
| compaction shrink ratio (tokens after / before) | 0.77 to 0.89 by default |
| memory recall added to a request / `MemorySearch` call | 10.5 ms p50 / 6.0 ms p50 |
| heartbeat and cron start lag | 0.45 to 0.76 s (worker poll interval 1 s) |

### toolsec

| measurement | value |
|---|---|
| host no-op tool, gap per call | p50 0.75 ms, p95 0.90 ms |
| local Bash `true` / sandbox Bash `true` | p50 1.47 ms / 21.2 ms |
| tool cache miss (200 ms tool) / hit | 202.7 ms / 0.117 ms |
| `agent.open`, tenant versus stdio | 0.748 ms versus 0.762 ms |
| remote 256 MiB output flood to `output_limit` (release) | 5.16 s foreground, 4.37 s background |
| server `SIGKILL` to the remote command gone | 43 ms (remote), 51 ms (MCP child) |
| server RSS over 100 runs | 24.2 MB at 25 runs to 32.4 MB at 100 (a slow linear rise; 12 open/close rounds flatten near 38 MB) |

### DeepSeek (28 requests, about 0.015 USD)

| measurement | value |
|---|---|
| prompt cache hit on the second request | 2688 of 2854 prompt tokens (0.94) |
| compaction summary request shares the primary prefix | 8 of 8 messages, cache hit 0.8356 / 0.8356 / 0.8354 (3 runs) |
| cost per call against the arithmetic | difference under 2e-19 USD |

## Run against the fixed stack

Run on 2026-11-18 against the final debug binary (the merge of the two fix PRs, `8144e331`), vLLM
through the serialising proxy, Postgres, one file at a time, no DeepSeek (its cases deselect). Load average 3 to 5.

| area | passed | other |
|---|---|---|
| durable (`test_live_durable_liveness`, `test_live_sched_resume`) | 6 | |
| toolsec (`test_ts_*`, 11 files) | 41 | |
| long (`test_live_long_*`, 9 files) | 26 | 1 skipped: the model wrote its memory index at the root instead of under `/memories` (model behaviour, the test skips with that reason) |
| core, first pass (`test_live_core_*`) | 52 | |
| core, checked against the docs (`test_live_e2e_core_*`) | 52 | 1 xfail (reasoning length against effort is not monotonic on this model; model behaviour) |
| schedules, jobs, workers (`test_live_sched_*`) | 18 | 1 skipped (DeepSeek-only case) |

What the fixes changed in these suites, all green against the final binary:

- keepalive times are whole seconds: `keepalive_idle_secs` / `keepalive_interval_secs` start the server and set
  keepalive without a warning; the millisecond names and `0` are refused at startup with the field named;
- `RunLeased.owner` is an opaque 8-hex-character server id, not the constant `linch-rpc`;
- a background command's final status (`output_limit`) is in the `tool_call_end` metadata;
- a command started through `SubprocessHost` ends when its client dies by `SIGKILL`;
- a test that prints the same failure after each of ten different patches no longer trips the loop guard;
- every accepted compaction shrinks the view (`tokens_after < tokens_before`) in the long compaction suite.

Two harness points were fixed on the way: the keepalive test now reads past the server id line printed
before `listening`, and the long suites skip without a vLLM URL. The long file-memory test failed in two
consecutive runs (the model wrote its index at `/MEMORY.md`) and passed in the three runs after; it now skips
on that model behaviour instead of failing.

## Not covered

Docker and the root-user sandbox path (no daemon or root on the test host); a server-side OTLP exporter
(none exists: spans are built by the Python client); the kernel's TCP keepalive against a peer that vanished
(needs packet drop, so root); a model that refuses to summarise (the compaction rejection hold is covered by
unit tests, not live); the `-32046` unpriced-mid-call path; the worker queue's `enqueue_approve` after a prune
(not reachable over RPC); the DeepSeek scenarios in this branch's final run.
