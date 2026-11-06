# Python Linch vs linch-rust bench

One harness, one table: the same work from the same fixture files on three
sides.

| Side | What runs | Driver |
|---|---|---|
| `py` | Python Linch from `master` (the native extension built from `crates/linch-python`) | `py_side.py` |
| `rs-client` | the `linch_rust` client (`python/src`) over the `linch-server` sidecar, the user-facing path | `rs_client_side.py` |
| `rs-kernel` | Rust direct through the facade crate `linch`, the floor | `crates/linch/examples/bench_driver.rs` |

Each (case, side) cell runs in its own process, so peak RSS and startup are
per cell. A cell a side cannot run is shown as `n/a (reason)`, never left out.

The numbers in a results table come from the coordinator's full run on a
quiet machine. A `--quick` run only checks that the harness works; its
numbers are not results.

## Setup

```bash
# Python side: master's Linch in its own venv, outside the repo.
git fetch origin && git worktree add --detach ../linch-agent-scratch/bench-master origin/master
cd ../linch-agent-scratch
uv venv bench-venv
VIRTUAL_ENV=$PWD/bench-venv uv pip install ./bench-master/linch-rust/crates/linch-python
VIRTUAL_ENV=$PWD/bench-venv uv pip install -e './bench-master[postgres,worker,mcp,anthropic,gemini]'

# Rust sides (from linch-rust/).
cargo build --release -p linch-server
cargo build --release -p linch --example bench_driver --example mcp_stdio_server
```

The `anthropic` and `gemini` extras are needed only by case 6 (Python's
Anthropic and Gemini adapters use the vendor SDKs).

## Run

```bash
cd linch-rust
PY=../../linch-agent-scratch/bench-venv/bin/python

# Smoke: 2 timed iterations per cell, small streams and queues.
$PY bench/run_all.py --out /tmp/bench-smoke --quick --pg-dsn "$DSN"

# Full run (20 timed iterations per cell by default; --runs N to change).
$PY bench/run_all.py --out /tmp/bench-full --pg-dsn "$DSN"

# Live DeepSeek, case 13 only, with DEEPSEEK_API_KEY in the environment.
$PY bench/run_all.py --out /tmp/bench-live --cases 13 --live
```

`--cases` takes case numbers or names (`1,3,decode`), `--sides` a subset of
`py,rs-client,rs-kernel`. Without `--pg-dsn`, the Postgres cells are n/a.
Each Postgres cell gets its own schema (through the DSN's `search_path`),
dropped afterwards. Case 12 is computed from case 1, so select both.

The output directory gets `results.md` (the table), `raw/<side>.json`
(everything each side printed, plus provenance), `jobs/` (the job each
process ran), `logs/` (each process's stderr) and `sse/` (the case 6
streams with their sizes and hashes).

Every side process gets the environment without provider keys. Case 6
sets dummy keys (the replay server ignores them); only `--live` passes
`DEEPSEEK_API_KEY`, to case 13, and nothing prints it.

## Cases

| # | Case | One iteration | Per op |
|---|---|---|---|
| 1 | Loop shapes, memory stores | one run of `loop_<N>x<k>.json`: N turns of k Echo calls, then a text turn (10x1, 10x3, 50x1, and 1x8 run in parallel) | ms per provider turn |
| 2 | Compaction | one run of 15 Echo turns of 4000-character values that compacts once, the summary scripted | ms per turn |
| 3 | Loop shapes, Postgres | case 1 on Postgres run and session stores | ms per turn |
| 4 | Workspace tools | one run calling one tool once per turn: Read, Write, Edit (each after a Read), Grep, Glob, Bash on a 10-file workspace, and Grep over a 2000-file tree | ms per call |
| 5 | MCP | one run calling `echo` on one stdio MCP server 10 times (the `mcp_stdio_server` example binary for every side) | ms per call |
| 6 | Provider stream decoding | one text-only run whose stream is the vendor's SSE stream, replayed on loopback by `sse_server.py` | MB/s and events/s |
| 7 | Worker queue | enqueue N one-turn runs (100; 5 with `--quick`), a worker in the same process drains them | jobs/s |
| 8 | Workflows | one workflow of N journaled steps (100; 10 with `--quick`), journal in memory and Postgres | ms per step |
| 9 | Evals | a 10-case suite (an Echo call, then the answer) scored by "text contains" | ms per case |
| 10 | Startup | one cold process: import, agent open, a first one-turn run, exit | ms per process |
| 11 | Concurrency | 16 agents in one process, one 10x1 session each, run at once | runs/s |
| 12 | RPC overhead | rs-client minus rs-kernel on the case 1 rows (derived, not measured) | ms per turn |
| 13 | Live DeepSeek | the W5 prompts on `deepseek-flash`, thinking off, one `get_weather` tool | ms per provider turn |

Columns: wall is the median per iteration; CPU is the mean per iteration
from the process CPU-time clock (rs-client adds the sidecar's, read through
its process CPU clock); peak RSS is the process peak (rs-client: client plus
sidecar). Store writes: on memory stores, the write calls of one extra,
untimed run (Python's store methods; Rust's `MemoryRunStore` and
`MemorySessionStore` counters; the client does not expose them, `n/c`); on
Postgres, inserted, updated and deleted tuples of the cell's schema
(`pg_stat_user_tables`) per run, or per job and per workflow.

## What is not the same on every side

- Python always renders its environment block in the system prompt; it runs
  with `SystemPromptConfig(replace_defaults=True)` and the same text as the
  Rust `system_prompt`. Its workspace registry is cut to the six tools Rust
  has.
- Glob's input schema differs (Python `glob_pattern`/`target_directory`,
  Rust `pattern`/`path`), so Python has its own Glob fixture; the search is
  the same. Python's default loop guard stops a run after three identical
  calls (Rust has none), so tool fixtures vary each call's input without
  changing the work.
- Tool and MCP latencies include one loop turn per call; subtract the case 1
  50x1 per-turn figure for the tool alone. Echo on rs-client is a host tool,
  so each call also crosses the RPC.
- Compaction rules differ: Rust compacts at `threshold_tokens` of its
  estimate, Python at 80% of the context window counting the message JSON
  plus a reserve. The settings make both compact once, before the same call,
  and each side reports compactions per run in the table.
- Decoding goes through each adapter over loopback HTTP, so the HTTP client
  is part of the figure. Python's adapters use the vendor SDKs (`openai`,
  `anthropic`, `google-genai`); its Gemini adapter has no base URL option,
  so google-genai's `GOOGLE_GEMINI_BASE_URL` points it at the server.
- Worker: Python's `Worker` (PgQueuer) builds a fresh agent per job through
  its factory; Rust's serves one agent. Both sides poll completion every 2 ms.
- Workflows journal into the run store in Python and into a `JournalStore`
  in Rust, so their Postgres writes are different tables.
- Memory write counts are calls of different store APIs, comparable only
  within a side; Postgres tuples are comparable.
- `rs-kernel` runs on a multi-thread runtime like `linch-server`; Python is
  one event loop. Its case 1 agent is built with `AgentBuilder` (as
  `AgentConfig::open` builds it) to keep the store counters.
- Startup: Rust has no import phase; rs-client's process includes the
  Python interpreter and the sidecar's spawn and exit.
- Live: provider time is not split out (the agents are opened from config,
  so the provider is not wrapped); wall and CPU per provider turn are shown.
  `crates/linch-providers/examples/e2e_bench.rs` splits kernel time for Rust.
- Worker, workflow and eval cells are n/a on rs-client: neither
  `linch-server` nor the client exposes them.

## Files

`make_fixtures.py` regenerates `fixtures/` (`--check` fails on a stale
file); `loop_10x1.json` and `loop_10x3.json` are the W5 fixtures, byte for
byte the same as `crates/linch-core/benches/fixtures/`. `common.py` holds the
measurement helpers of the two Python sides. Standard library only, plus
what each side already depends on.
